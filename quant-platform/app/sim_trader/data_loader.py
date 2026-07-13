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

    候选⑥:实时缝合逻辑委托给 LiveBarStitcher(避免重复 quote 拉取 +
    snapshot 构造 + DataFrame merge 样板)。
    """
    from app.data_manager.live_bar_stitcher import LiveBarStitcher
    stitcher = LiveBarStitcher()
    new_bars, snapshot = stitcher.stitch_bars(bars, today)

    if snapshot and bars is not new_bars:
        # 成功注入实时行情 → 走原 log 提示
        try:
            from core.logger import get_logger
            log = get_logger("SimLoader")
            log.info(f"实时行情已注入: {len(snapshot)} 只股票")
        except Exception:
            pass
        return new_bars, snapshot

    if not snapshot:
        # 回退到历史:LiveBarStitcher 返回 history snapshot(无缝等价)
        try:
            from core.logger import get_logger
            log = get_logger("SimLoader")
            log.warning("所有实时行情通道均失败，回退到历史数据")
        except Exception:
            pass
    return new_bars, snapshot


def _finalize(bars, today, new_rows, snapshot):
    """合并实时行情行到 bars(候选⑥后,该函数保留以防外部调用,但 augment_bars_with_realtime 不再走它)"""
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
