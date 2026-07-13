"""
TDX 选股结果缓存层

数据源仍是 TDX —— 本模块只是把 TDX worker 返回的 signals/prices 在本地存一份
parquet 副本，下次同样请求直接读副本，跳过 subprocess + 通达信公式重算。

缓存 key = sha1(公式名 + 区间 + K线数 + 返回数 + 股票池样本)
同一公式同一区间的选股结果是确定函数，可安全缓存。
换公式 / 换区间 / 换 K线数 → key 变化 → 自动失效重跑。

parquet 长表 schema:
    code (str)          股票代码（数字部分）
    date (str)          YYYYMMDD
    signal_var (str)    信号变量名（ZP/ZT/中文/任意）
    signal_value (str)  信号值原样字符串（"1"/"100"/"0"/"0.5"）
    open/high/low/close (float)  OHLC，缺失用 NaN
"""
import hashlib
import shutil
from pathlib import Path
from typing import Optional

import pandas as pd

from core.logger import get_logger

log = get_logger("TdxCache")

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = ROOT / "output" / "tdx_cache"

# 缓存格式版本：schema 变化时递增，旧缓存自动失效
_CACHE_VERSION = 2

# 通达信公式库文件 —— 用户在通达信里改任何公式都会更新这些文件的 mtime
# 用于检测公式内容变更，避免改公式后命中旧缓存
_FORMULA_LIB_FILES = ("PriCS.dat", "PriGS.dat", "PriLoc.dat")


def formula_fingerprint(tdx_root: Path) -> str:
    """读取通达信公式库文件的 mtime+size 指纹。

    用户在通达信公式管理器里改公式内容（即使不改公式名）→ 公式库 .dat 文件
    mtime 更新 → 指纹变化 → 缓存 key 变化 → 旧缓存自动失效。
    包含 PriCS(条件选股)/PriGS(技术指标)/PriLoc(本地) 三类，覆盖所有公式类型。
    返回空串表示读不到公式库（调用方应禁用缓存以保证正确性）。
    """
    t0002 = tdx_root / "T0002"
    parts = []
    for name in _FORMULA_LIB_FILES:
        f = t0002 / name
        try:
            st = f.stat()
            parts.append(f"{name}:{int(st.st_mtime)}:{st.st_size}")
        except Exception:
            pass
    return "|".join(parts)


def _cache_key(formula_name: str, start_time: str, end_time: str,
               kline_count: int, return_count: int,
               stock_list_override: Optional[list],
               formula_fp: str = "") -> str:
    """构造缓存 key。股票池只取前 20 个样本 + 总数做摘要，避免全量序列化。

    formula_fp 是通达信公式库文件指纹 —— 改公式内容（不改名）也会让 key 变化，
    避免命中旧缓存返回错误结果。
    """
    if stock_list_override:
        sl = ",".join(sorted(stock_list_override)[:20]) + f"|n={len(stock_list_override)}"
    else:
        sl = "ALL"
    parts = [
        f"v{_CACHE_VERSION}",
        formula_name or "",
        start_time or "",
        end_time or "",
        str(kline_count),
        str(return_count),
        sl,
        f"fp={formula_fp}",
    ]
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.parquet"


def get_cache(formula_name: str, start_time: str, end_time: str,
              kline_count: int, return_count: int,
              stock_list_override: Optional[list],
              formula_fp: str = "") -> Optional[Path]:
    """命中缓存则返回 parquet 路径，否则 None。"""
    key = _cache_key(formula_name, start_time, end_time, kline_count,
                     return_count, stock_list_override, formula_fp)
    p = _cache_path(key)
    return p if p.exists() else None


def _ensure_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def save_cache_from_dict(signals: dict, prices: dict,
                         formula_name: str, start_time: str, end_time: str,
                         kline_count: int, return_count: int,
                         stock_list_override: Optional[list],
                         formula_fp: str = "") -> Path:
    """从 worker 返回的 signals/prices dict 构造 parquet 并写入缓存。"""
    rows = _signals_prices_to_rows(signals, prices)
    _ensure_dir()
    key = _cache_key(formula_name, start_time, end_time, kline_count,
                     return_count, stock_list_override, formula_fp)
    dst = _cache_path(key)
    df = pd.DataFrame(rows, columns=[
        "code", "date", "signal_var", "signal_value",
        "open", "high", "low", "close"])
    df.to_parquet(dst, index=False)
    log.info(f"缓存写入: {dst.name} ({len(df)}行, {df['code'].nunique()}只)")
    return dst


def save_cache_from_parquet(src: Path,
                            formula_name: str, start_time: str, end_time: str,
                            kline_count: int, return_count: int,
                            stock_list_override: Optional[list],
                            formula_fp: str = "") -> Path:
    """把 worker 写的 parquet 复制进缓存目录（B 路径用）。"""
    _ensure_dir()
    key = _cache_key(formula_name, start_time, end_time, kline_count,
                     return_count, stock_list_override, formula_fp)
    dst = _cache_path(key)
    shutil.copy2(src, dst)
    return dst


def clear_cache() -> int:
    """清空全部缓存（手动失效用）。返回删除文件数。"""
    if not CACHE_DIR.exists():
        return 0
    n = 0
    for f in CACHE_DIR.glob("*.parquet"):
        try:
            f.unlink()
            n += 1
        except Exception:
            pass
    log.info(f"已清空 {n} 个缓存文件")
    return n


# ── dict ↔ parquet 长表互转 ────────────────────────────────

def _safe_float(v) -> Optional[float]:
    """安全转 float；None/NaN/<=0/异常 → None（parquet 存 NaN）。"""
    if v is None:
        return None
    try:
        f = float(v)
        if f != f or f <= 0:  # NaN or 非正
            return None
        return f
    except (ValueError, TypeError):
        return None


def _signals_prices_to_rows(signals: dict, prices: dict) -> list:
    """把 worker 的 signals/prices dict 展平为长表行。

    signals: {code: {Date:[...], <var>:[...]}}  —— 全市场跑过公式的 code（key 可能带.SZ/.SH后缀）
    prices:  {code: {Date:[...], Close:[...], High/Low/Open:[...]}}  —— 仅 signal_codes（key 是数字部分）
    注意：worker 返回的 signals key 带后缀(000011.SZ)、prices key 是数字部分(000011)，
    匹配时必须统一去后缀，否则 OHLC 全部对不上。
    """
    # 建 (code_num, date) → OHLC 索引，code_num 统一去后缀
    price_lookup = {}
    for code, d in prices.items():
        code_num = code.split(".")[0] if "." in code else code
        dates = d.get("Date", [])
        closes = d.get("Close", [])
        highs = d.get("High")
        lows = d.get("Low")
        opens = d.get("Open")
        n = len(dates)
        for i, dt in enumerate(dates):
            price_lookup[(code_num, str(dt))] = (
                _safe_float(opens[i]) if opens and i < len(opens) else None,
                _safe_float(highs[i]) if highs and i < len(highs) else None,
                _safe_float(lows[i]) if lows and i < len(lows) else None,
                _safe_float(closes[i]) if i < len(closes) else None,
            )

    rows = []
    for code, d in signals.items():
        code_num = code.split(".")[0] if "." in code else code  # 去后缀匹配 prices
        dates = d.get("Date", [])
        var = next((k for k in d.keys() if k != "Date"), "ZP")
        vals = d.get(var, [])
        n = len(dates)
        for i in range(n):
            dt_str = str(dates[i])
            ohlc = price_lookup.get((code_num, dt_str), (None, None, None, None))
            rows.append({
                "code": code,  # 保留原 key（带后缀），与 worker 返回一致
                "date": dt_str,
                "signal_var": var,
                "signal_value": str(vals[i]) if i < len(vals) else "",
                "open": ohlc[0],
                "high": ohlc[1],
                "low": ohlc[2],
                "close": ohlc[3],
            })
    return rows


def df_to_signals(df: pd.DataFrame) -> dict:
    """长表 DataFrame → signals dict（兼容 API/旧接口，仅取信号变量）。

    返回 {code: {Date:[...], <var>:[...]}}，与 worker 原始格式一致。
    """
    signals = {}
    if df is None or df.empty:
        return signals
    for code, g in df.groupby("code", sort=False):
        dates = g["date"].astype(str).tolist()
        var = str(g["signal_var"].iloc[0])
        vals = g["signal_value"].astype(str).tolist()
        signals[code] = {"Date": dates, var: vals}
    return signals


def df_to_signals_prices(df: pd.DataFrame):
    """长表 DataFrame → (signals, prices) dict，与 worker 原始格式完全一致。

    signals: {code: {Date:[...], <var>:[...]}}  —— 所有 code
    prices:  {code: {Date:[...], Close:[...], High/Low/Open:[...]}}  —— 仅 close 非空的 code
    OHLC 转回字符串以匹配 worker _col_to_values 的输出（NaN → "0"）。
    """
    signals = {}
    prices = {}
    if df is None or df.empty:
        return signals, prices
    for code, g in df.groupby("code", sort=False):
        dates = g["date"].astype(str).tolist()
        var = str(g["signal_var"].iloc[0])
        vals = g["signal_value"].astype(str).tolist()
        signals[code] = {"Date": dates, var: vals}

        # prices：只取 close 非空的行（有 TDX 价格数据的 code）
        close_series = g["close"]
        if close_series.notna().any():
            sub = g[close_series.notna()]
            prices[code] = {
                "Date": sub["date"].astype(str).tolist(),
                "Close": [_float_to_str(v) for v in sub["close"].tolist()],
                "High": [_float_to_str(v) for v in sub["high"].tolist()],
                "Low": [_float_to_str(v) for v in sub["low"].tolist()],
                "Open": [_float_to_str(v) for v in sub["open"].tolist()],
            }
    return signals, prices


def _float_to_str(v) -> str:
    """float → 字符串，NaN/None → "0"（匹配 worker _col_to_values 行为）。"""
    if v is None:
        return "0"
    try:
        f = float(v)
        if f != f or f <= 0:  # NaN or 非正
            return "0"
        return str(f)
    except (ValueError, TypeError):
        return "0"
