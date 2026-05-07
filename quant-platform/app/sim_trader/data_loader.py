"""
模拟盘交易 — 数据加载器
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pandas as pd
from datetime import date
from pathlib import Path
from database.duckdb_manager import db, PARQUET_DAILY_DIR
from app.screener.engine import load_strategy
from app.sim_trader.config import LOAD_START, SIM_END, SIGNAL_PARAMS, STRATEGY_NAME


def load_all_bars(start: date = None, end: date = None):
    """加载全市场日线"""
    s = start or LOAD_START
    e = end or SIM_END
    bars = db.load_all_bars(freq="daily", start=s, end=e)
    # DuckDB .df() 已经返回 pandas DataFrame；仅在非 DataFrame 时转换
    if not isinstance(bars, pd.DataFrame):
        bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
    if bars.empty or "code" not in bars.columns:
        return bars if isinstance(bars, pd.DataFrame) else pd.DataFrame()
    for c in ["open", "high", "low", "close", "volume"]:
        if c in bars:
            bars[c] = pd.to_numeric(bars[c], errors='coerce')
    if "close" in bars.columns:
        bars = bars.dropna(subset=["close"])
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    return bars.sort_values(["code", "date"])


def augment_bars_with_realtime(bars: pd.DataFrame, today: date):
    """
    用实时行情补充今日 bar 数据（14:52 盘中执行时需要）。
    Parquet 文件中没有当日未收盘的 bar，必须从 QMT/TDX/腾讯 获取。
    返回: (augmented_bars, snapshot_dict)
    """
    if today != date.today():
        return bars, get_daily_snapshot(bars, today)

    from core.logger import get_logger
    log = get_logger("SimLoader")

    if 'code' not in bars.columns:
        log.warning("bars 缺少 'code' 列，回退到历史数据")
        return bars, get_daily_snapshot(bars, today)

    codes = bars['code'].unique().tolist()

    snapshot = {}
    new_rows = []
    grouped = bars.groupby("code")

    def _build_result(quotes_dict):
        """将行情字典转换为 snapshot 和 bars 行"""
        nonlocal snapshot, new_rows
        for code, q in quotes_dict.items():
            price = float(q.get('price', 0) or q.get('lastPrice', 0))
            if price <= 0:
                continue
            group = grouped.get_group(code) if code in grouped.groups else None
            if group is None or group.empty:
                continue
            last_row = group.iloc[-1].to_dict()
            snapshot[code] = {
                'open': float(q.get('open', price)),
                'high': float(q.get('high', price)),
                'low': float(q.get('low', price)),
                'close': price,
            }
            last_row['date'] = today
            last_row['open'] = snapshot[code]['open']
            last_row['high'] = snapshot[code]['high']
            last_row['low'] = snapshot[code]['low']
            last_row['close'] = snapshot[code]['close']
            last_row['volume'] = float(q.get('volume', 0) or q.get('vol', 0))
            new_rows.append(last_row)

    # 通道 1: QMT（最快，本地代理）
    try:
        from app.trader.gateways.qmt import qmt_gateway
        qmt_quotes = qmt_gateway.get_realtime_quotes(codes)
        if qmt_quotes:
            # 规范化 QMT 字段名
            normalized = {}
            for code, q in qmt_quotes.items():
                normalized[code] = {
                    'price': q.get('lastPrice', 0),
                    'open': q.get('open', q.get('lastPrice', 0)),
                    'high': q.get('high', q.get('lastPrice', 0)),
                    'low': q.get('low', q.get('lastPrice', 0)),
                    'volume': q.get('volume', 0),
                }
            _build_result(normalized)
            if snapshot:
                log.info(f"QMT 实时行情已注入: {len(snapshot)} 只股票")
                return _finalize(bars, today, new_rows, snapshot)
    except Exception as e:
        log.debug(f"QMT 行情获取失败: {e}")

    # 通道 2: 腾讯 HTTP（批量、快速、无需 QMT）
    try:
        import requests
        # 构建 code 查找表
        code_set = {str(c).split('.')[0]: str(c) for c in codes}

        tencent_codes = []
        for c in codes:
            clean = str(c).split('.')[0]
            prefix = "sh" if clean.startswith(('6', '000')) else "sz"
            tencent_codes.append(f"s_{prefix}{clean}")

        tencent_quotes = {}
        batch_size = 300  # 腾讯 API 单次 URL 长度安全上限
        for i in range(0, len(tencent_codes), batch_size):
            batch = tencent_codes[i:i + batch_size]
            url = f"http://qt.gtimg.cn/q={','.join(batch)}"
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                continue
            raw = resp.text
            for line in raw.split(';'):
                if '~' not in line or '=' not in line:
                    continue
                try:
                    seg = line.split('=')[1].replace('"', '').strip()
                    parts = seg.split('~')
                    if len(parts) < 7:
                        continue
                    code_raw = parts[2]
                    price = float(parts[3])
                    if price <= 0:
                        continue
                    chg = float(parts[4])
                    orig = code_set.get(code_raw, code_raw)
                    tencent_quotes[orig] = {
                        'price': price,
                        'open': price,
                        'high': price,
                        'low': price,
                        'volume': float(parts[6]) if len(parts) > 6 else 0,
                        'last_close': price - chg,
                    }
                except (ValueError, IndexError):
                    continue

        if tencent_quotes:
            _build_result(tencent_quotes)
            log.info(f"腾讯行情已注入: {len(snapshot)} 只股票")
            return _finalize(bars, today, new_rows, snapshot)
    except Exception as e:
        log.warning(f"腾讯行情获取失败: {e}")

    log.warning("所有实时行情通道均失败，回退到历史数据")
    return bars, get_daily_snapshot(bars, today)


def _finalize(bars, today, new_rows, snapshot):
    """将实时行情行合并到 bars 中"""
    if new_rows:
        bars = bars[bars['date'] != today]
        ndf = pd.DataFrame(new_rows)
        bars = pd.concat([bars, ndf], ignore_index=True)
        bars = bars.sort_values(['code', 'date'])
    return bars, snapshot


def generate_today_signals(bars: pd.DataFrame, today: date, strategy_name: str = None):
    """生成当天买入信号，返回 [(code, close_price), ...]"""
    name = strategy_name or STRATEGY_NAME
    strategy = load_strategy(name)
    try:
        sig = strategy.generate_signals(bars)
    except TypeError:
        sig = strategy.generate_signals(bars, **SIGNAL_PARAMS)
    if sig is None or sig.empty:
        return []
    sig = sig[sig["date"] == today].copy()
    if sig.empty:
        return []
    return [(r['code'], float(r['close'])) for _, r in sig.iterrows()]


def load_sh_index():
    """加载上证指数日线"""
    sh_path = PARQUET_DAILY_DIR / "index_000001.parquet"
    if not sh_path.exists():
        return pd.DataFrame()
    sh = pd.read_parquet(str(sh_path))
    sh['date'] = pd.to_datetime(sh['date']).dt.date
    sh = sh.sort_values('date')
    sh['ma20'] = sh['close'].rolling(20).mean()
    return sh


def is_bull_market(sh_index: pd.DataFrame, d: date) -> bool:
    """上证收盘在MA20之上"""
    if sh_index.empty:
        return True
    row = sh_index[sh_index['date'] == d]
    if row.empty:
        return True
    r = row.iloc[0]
    if pd.isna(r['ma20']):
        return True
    return float(r['close']) >= float(r['ma20'])


def get_daily_snapshot(bars: pd.DataFrame, today: date) -> dict:
    """返回当天每只股票的 OHLC 快照"""
    day_bars = bars[bars["date"] == today]
    return {
        r['code']: {
            'open': float(r['open']), 'high': float(r['high']),
            'low': float(r['low']), 'close': float(r['close']),
        }
        for _, r in day_bars.iterrows()
    }
