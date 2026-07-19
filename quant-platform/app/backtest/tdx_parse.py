"""
TDX 缓存 parquet 的向量化解析

替代旧路径 "parquet → df_to_signals_prices(字符串化 dict) → tdx_runner 逐行 float() 解析"
的来回翻译(实测占缓存命中路径 ~97% 耗时)。直接从缓存 parquet 长表一次成型
回测引擎需要的 sig_by_code / prices_by_date 结构。

零差异口径(与 tdx_runner 旧解析循环逐字段等价,除两处有意声明的差异):
- 差异 1(修复): 信号判定。旧 _is_signal_value 对 "nan" 串返回 True(float("nan")!=0
  为 True), 会把 TDX 历史不足 bar 的 NaN 信号误判为买入信号。本模块按修复口径:
  NaN → 非信号 (sv.notna() & (sv != 0))。其余输入("0"/"0.0"/""/None/非法串/
  "0.5"/"100"/带空格/"1e3"/"inf")与旧实现逐项等价。用户已拍板(2026-07-18)。
- 差异 2(声明): close 为 NaN/<=0 的行不进 prices_by_date。这与旧**缓存命中**路径
  一致(df_to_signals_prices 的 sub = g[close.notna()] 本就剔除);但旧**冷路径**
  (worker dict 直传, 不走缓存)会把这些行归一成 close=0.0 纳入。即旧代码自身
  冷/热路径就不一致, 本模块统一采用热路径(剔除)口径, 行为更合理。
- OHLC 高/低/开无效(NaN/<=0): 日线逐行 fallback close;日内复刻旧"整股翻转"
  (首行无效后该股后续全部 fallback close), 与旧路径逐位一致。
- low 的 parquet fallback: 旧实现因 'YYYY-MM-DD' vs 'YYYYMMDD' 格式不匹配恒失败
  (low=close), 本模块直接 low=close, 与旧行为逐位一致(该 fallback 失效是已登记
  的独立遗留问题, 不在此修)。
- 日期: 缓存 date 列为 8 位定长 YYYYMMDD 字符串, 字典序=日期序;非法日期串
  旧循环 date(int(...)) 抛异常跳过, 本模块 to_datetime(coerce) 丢 NaT, 等价。
"""
import numpy as np
import pandas as pd

from core.logger import get_logger

log = get_logger("TdxParse")


def load_cache_df(parquet_path: str, start, end) -> pd.DataFrame:
    """读缓存 parquet, 谓词下推只取 [start, end] 区间(区间外 82.5% 的行不读)。"""
    s = start.strftime("%Y%m%d")
    e = end.strftime("%Y%m%d")
    return pd.read_parquet(parquet_path, filters=[("date", ">=", s), ("date", "<=", e)])


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    """公共预处理: code 去后缀、date 转 ISO、丢非法日期行。"""
    out = df.copy()
    out["code_num"] = out["code"].str.split(".").str[0]
    d = out["date"].astype(str)
    out["date_iso"] = d.str[:4] + "-" + d.str[4:6] + "-" + d.str[6:8]
    valid_date = pd.to_datetime(out["date_iso"], errors="coerce").notna()
    return out[valid_date]


def _signal_mask(df: pd.DataFrame) -> pd.Series:
    """向量化信号判定(修复口径: NaN → 非信号)。"""
    sv = pd.to_numeric(df["signal_value"], errors="coerce")
    return sv.notna() & (sv != 0)


def parse_daily(df: pd.DataFrame):
    """日线路径语义: OHLC 逐行判断, 无效行(任一字段 NaN/<=0) → high/low/open=close。

    返回 (sig_by_code, prices_by_date):
      sig_by_code:    {code_num: {date_iso: zp_str}}   只含信号日期
      prices_by_date: {date_iso: {code_num: {"close","high","low","open"}}}
    """
    if df is None or df.empty:
        return {}, {}
    df = _prep(df)

    # ── 信号: 只存"确为信号"的日期(与旧日线路径 7-16 后行为一致) ──
    sig_df = df[_signal_mask(df)]
    sig_by_code = {}
    for code, g in sig_df.groupby("code_num", sort=False):
        sig_by_code[code] = dict(zip(g["date_iso"], g["signal_value"]))

    # ── 价格: close 非空行(等价旧 df_to_signals_prices 的 sub 选择) ──
    prices_by_date = _build_prices(df, flip_per_code=False)
    return sig_by_code, prices_by_date


def parse_intraday(df: pd.DataFrame):
    """日内路径语义: has_ohlc 整股翻转 — 首行脏数据后, 该股后续所有行
    high/low/open 全部 fallback 为 close(逐位复刻旧日内循环的永久翻转转行为,
    保证零差异;该行为是否合理解待单独立项, 与 C7 的读盘风暴修复不冲突)。

    返回结构同 parse_daily。sig_by_code 只含信号日期(旧日内路径存全部日期但
    下游只用信号日期做 pending_buys 过滤, 等价)。
    """
    if df is None or df.empty:
        return {}, {}
    df = _prep(df)

    sig_df = df[_signal_mask(df)]
    sig_by_code = {}
    for code, g in sig_df.groupby("code_num", sort=False):
        sig_by_code[code] = dict(zip(g["date_iso"], g["signal_value"]))

    prices_by_date = _build_prices(df, flip_per_code=True)
    return sig_by_code, prices_by_date


def _build_prices(df: pd.DataFrame, flip_per_code: bool) -> dict:
    """构造 prices_by_date。close 必须非 NaN(旧路径 close 缺失的行不进 prices)。

    flip_per_code=False (日线): 逐行判断, 无效行 h/l/o=close。
    flip_per_code=True  (日内): 同一只股首次出现无效行后, 该股后续所有行 h/l/o=close。
    """
    px = df[df["close"].notna()]
    if px.empty:
        return {}
    close = px["close"]
    valid = (px["high"] > 0) & (px["low"] > 0) & (px["open"] > 0)  # NaN 比较 → False
    if flip_per_code:
        # 无效行 cumax 翻转: 首次无效后该股永久 fallback(复刻旧日内 has_ohlc 翻转)
        flipped = (~valid).groupby(px["code_num"], sort=False).cummax()
        use_real = valid & ~flipped
    else:
        use_real = valid
    high = px["high"].where(use_real, close)
    low = px["low"].where(use_real, close)
    open_ = px["open"].where(use_real, close)

    # tolist() → Python 原生 float(与旧解析 float() 产物类型一致,防 np.float64 泄漏进 JSON)
    codes_l = px["code_num"].tolist()
    dates_l = px["date_iso"].tolist()
    closes_l = close.tolist()
    highs_l = high.tolist()
    lows_l = low.tolist()
    opens_l = open_.tolist()

    prices_by_date = {}
    for cn, dt, c, h, l, o in zip(codes_l, dates_l, closes_l, highs_l, lows_l, opens_l):
        sub = prices_by_date.get(dt)
        if sub is None:
            sub = prices_by_date[dt] = {}
        sub[cn] = {"close": c, "high": h, "low": l, "open": o}
    return prices_by_date
