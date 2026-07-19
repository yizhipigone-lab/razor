# 临时性能研究脚本 — TDX 回测缓存命中路径 cProfile
# 用法: python scripts/_perf_profile_tdx.py [cache_parquet]
# 目的: 分段计时 + cProfile 定位热点, 供性能优化研究报告用
import cProfile
import pstats
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tqsdk import result_cache
from app.backtest import tdx_runner


def build_params():
    """复刻 run_tdx_backtest 的参数准备逻辑 (daily 路径)"""
    params = {
        "start_date": date(2025, 5, 1),
        "end_date": date(2026, 7, 15),
        "intraday_freq": "daily",
        "strategy_name": "QUANTQQ",
    }
    from app.config.risk_params import load_risk_params, load_position_params, load_streak_params
    _rp = load_risk_params()
    _pp = load_position_params()
    _sp = load_streak_params()
    params.setdefault("initial_capital", _pp.initial_capital)
    params.setdefault("position_size",    _pp.position_size)
    params.setdefault("min_buy_amt",      _pp.min_buy_amt)
    params.setdefault("hard_stop",        _rp.hard_stop)
    params.setdefault("take_profit_tiers", _rp.take_profit_tiers)
    params.setdefault("trail_activate",   _rp.trail_activate)
    params.setdefault("trail_dd",         _rp.trail_dd)
    params.setdefault("time_exit_days",   _rp.time_exit_days)
    params.setdefault("time_exit_profit", _rp.time_exit_profit)
    params.setdefault("time_force_days",  _rp.time_force_days)
    params.setdefault("loss_streak_pause", _sp.loss_streak_pause)
    params.setdefault("pause_days",       _sp.pause_days)
    params.setdefault("loss_streak_halve", _sp.loss_streak_halve)
    params.setdefault("same_stock_cooldown", _sp.same_stock_cooldown)
    params["position_ratio"] = params["position_size"] / params["initial_capital"]
    return params


def main():
    cache_file = sys.argv[1] if len(sys.argv) > 1 else None
    if not cache_file:
        # 默认用最新的一份缓存
        cands = sorted((ROOT / "output" / "tdx_cache").glob("*.parquet"),
                       key=lambda p: p.stat().st_mtime)
        cache_file = str(cands[-1])
    print(f"cache: {cache_file}")

    # ── 段 1: 读 parquet ──
    t0 = time.perf_counter()
    df = pd.read_parquet(cache_file)
    t1 = time.perf_counter()
    print(f"[seg1] read_parquet: {t1-t0:.2f}s  rows={len(df)}")

    # ── 段 2: df → signals/prices dict (字符串化) ──
    t0 = time.perf_counter()
    signals, prices = result_cache.df_to_signals_prices(df)
    t1 = time.perf_counter()
    print(f"[seg2] df_to_signals_prices: {t1-t0:.2f}s  signals={len(signals)} prices={len(prices)}")

    del df  # 释放, 模拟真实路径内存

    sig_result = {"status": "ok", "signals": signals, "prices": prices}
    params = build_params()
    start = params["start_date"]
    end = params["end_date"]

    # ── 段 3: 日线回放回测 (cProfile) ──
    pr = cProfile.Profile()
    t0 = time.perf_counter()
    pr.enable()
    result = tdx_runner._run_daily_backtest(
        sig_result, params, start, end, None, None, {})
    pr.disable()
    t1 = time.perf_counter()
    print(f"[seg3] _run_daily_backtest: {t1-t0:.2f}s  trades={result['summary']['trades']} "
          f"total_return={result['summary']['total_return']}%")

    stats = pstats.Stats(pr)
    stats.sort_stats("cumulative")
    print("\n===== TOP 30 by cumulative =====")
    stats.print_stats(30)
    stats.sort_stats("tottime")
    print("\n===== TOP 30 by tottime =====")
    stats.print_stats(30)


if __name__ == "__main__":
    main()
