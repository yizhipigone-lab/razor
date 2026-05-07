#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度策略 — 止盈止损参数优化（多进程版）
所有 db 相关导入在 main() 内部，避免 Windows spawn 子进程错误连接 DuckDB
"""
import sys, os
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.optimize_stop_core import init_worker, simulate_single
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, timedelta
import multiprocessing
import time


def main():
    from database.duckdb_manager import db
    from app.screener.strategies.ma5_angle import generate_signals
    import pandas as pd
    import numpy as np

    END   = date(2026, 4, 29)
    START = date(2025, 1, 1)
    LOAD_START = START - timedelta(days=365)
    N_CPUS = multiprocessing.cpu_count()
    print(f"检测到 {N_CPUS} 个 CPU 核心\n")

    print("加载 K 线数据 ...")
    bars = db.load_all_bars(freq="daily", start=LOAD_START, end=END)
    bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
    for c in ["open","high","low","close","volume"]:
        if c in bars: bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=["close"])
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    bars = bars.sort_values(["code","date"])
    print(f"  {bars['code'].nunique():,} 只股票, {len(bars):,} 行\n")

    print("计算 RPS ...")
    rps_values = {}
    for code in bars["code"].unique():
        cb = bars[bars["code"] == code].sort_values("date")
        if len(cb) < 120:
            continue
        ret = cb.iloc[-1]["close"] / cb.iloc[-120]["close"] - 1
        rps_values[code] = ret
    rps_series = pd.Series(rps_values)
    rps_rank = rps_series.rank(pct=True) * 100
    rps_high_set = set(rps_rank[rps_rank > 80].index.tolist())
    print(f"  RPS>80: {len(rps_high_set):,} 只\n")

    print("生成原始策略信号 ...")
    sig = generate_signals(bars, version="original")
    sig = sig[(sig["date"] >= START) & (sig["date"] <= END)].copy()
    sig["date"] = pd.to_datetime(sig["date"]).dt.date
    sig["buy_price"] = sig["close"].astype(float)
    sig = sig[sig["code"].isin(rps_high_set)].copy()
    print(f"  组合8 信号数: {len(sig)}\n")

    # ── 预建 numpy 索引 ──────────────────────────────────
    print("构建高性能数据索引 ...")
    code_price_arrays = {}
    code_date_pos = {}
    for code in bars["code"].unique():
        cb = bars[bars["code"] == code].sort_values("date")
        code_price_arrays[code] = cb["close"].values.astype(np.float64)
        code_date_pos[code] = {d: i for i, d in enumerate(cb["date"].values)}

    signal_tuples = []
    for _, row in sig.iterrows():
        d = row["date"]
        pos = code_date_pos.get(row["code"], {}).get(d)
        if pos is not None:
            signal_tuples.append((row["code"], float(row["buy_price"]), pos))

    print(f"  预建 {len(signal_tuples)} 个信号位置\n")

    # ═══════════════════════════════════════════════════════════
    # 第1轮：各组参数独立扫描
    # ═══════════════════════════════════════════════════════════
    print("=" * 70)
    print("第1轮：各组参数独立扫描")
    print("=" * 70)

    all_configs = []

    # A. 基线
    for h in [5, 10, 15, 20, 30]:
        all_configs.append((f"基线_hold={h}d", None, None, None, None, h, None))

    # B. 移动止盈
    for ts in [-2, -3, -4, -5, -7, -10]:
        for h in [10, 15, 20]:
            all_configs.append((f"移动止盈_{ts}%_hold={h}d", ts, None, None, None, h, None))

    # C. 回撤止盈
    for act in [3, 5, 8, 10, 15]:
        for dd in [2, 3, 4, 5, 8]:
            for h in [10, 15, 20]:
                all_configs.append((f"回撤_{act}a_{dd}d_hold={h}d", None, act, dd, None, h, None))

    # D. 分档止盈
    tier_cfgs = [
        [(5, 100)], [(8, 100)], [(10, 100)],
        [(5, 50), (10, 50)],
        [(10, 50), (20, 50)],
        [(5, 33), (10, 33), (15, 34)],
        [(10, 33), (20, 33), (30, 34)],
    ]
    for tiers in tier_cfgs:
        for h in [15, 20, 30]:
            name = "分档_" + "_".join(f"{t[0]}%({t[1]}%)" for t in tiers) + f"_hold={h}d"
            all_configs.append((name, None, None, None, tiers, h, None))

    # E. 硬止损
    for sl in [-3, -5, -7, -10]:
        for h in [10, 15, 20]:
            all_configs.append((f"硬止损_{sl}%_hold={h}d", None, None, None, None, h, sl))

    N_PARAMS = len(all_configs)
    print(f"共 {N_PARAMS} 组参数\n")

    # ── 多进程执行 ─────────────────────────────────────────
    t0 = time.time()
    results = []

    with ProcessPoolExecutor(max_workers=min(N_CPUS, 16),
                             initializer=init_worker,
                             initargs=(code_price_arrays, code_date_pos, signal_tuples)) as executor:
        futures = [executor.submit(simulate_single, p) for p in all_configs]
        for i, f in enumerate(as_completed(futures)):
            r = f.result()
            if r:
                results.append(r)
            if (i + 1) % 20 == 0 or (i + 1) == N_PARAMS:
                print(f"  进度: {i+1}/{N_PARAMS} ({time.time()-t0:.0f}s)")

    print(f"\n  完成! 耗时 {time.time()-t0:.0f}s\n")

    # ── 排名输出 ──────────────────────────────────────────
    def print_top(results, key, label, top_n=12):
        sorted_r = sorted(results, key=lambda x: x[key], reverse=True)
        print(f"\n{'─'*90}")
        print(f"按 {label} 排名 Top{top_n}")
        print(f"{'─'*90}")
        print(f"{'#':<3} {'策略':<40} {'交易':>6} {'胜率':>6} {'avg%':>8} {'med%':>8} {'PF':>7} {'总收益':>10}")
        print(f"{'─'*90}")
        for i, r in enumerate(sorted_r[:top_n]):
            print(f"{i+1:<3} {r['name']:<40} {r['trades']:>6} {r['win_rate']:>5.1f}% "
                  f"{r['avg_return']:>+7.2f}% {r['med_return']:>+7.2f}% "
                  f"{r['profit_factor']:>6.2f} {r['total_return']:>+9.0f}%")

    print_top(results, "profit_factor", "PF")
    print_top(results, "avg_return", "平均收益")
    print_top(results, "win_rate", "胜率")

    # ═══════════════════════════════════════════════════════════
    # 第2轮：最优组合
    # ═══════════════════════════════════════════════════════════
    print("\n\n" + "=" * 70)
    print("第2轮：最优参数组合 (移动止盈 + 回撤 + 分档)")
    print("=" * 70)

    best_combos = []
    for ts in [None, -3, -4, -5, -7]:
        for act in [None, 5, 8, 10]:
            for dd in [None, 3, 4, 5]:
                for tier in [None, [(8, 100)], [(10, 50), (20, 50)], [(5, 33), (10, 33), (15, 34)]]:
                    for h in [10, 15, 20]:
                        if ts is None and act is None and tier is None:
                            continue
                        if act is not None and dd is None:
                            continue
                        if dd is not None and act is None:
                            continue
                        parts = []
                        if ts: parts.append(f"移动{ts}%")
                        if act: parts.append(f"回撤{act}/{dd}")
                        if tier: parts.append(f"分档{len(tier)}档")
                        parts.append(f"hold={h}")
                        best_combos.append(("_".join(parts), ts, act, dd, tier, h, None))

    N_BEST = len(best_combos)
    print(f"共 {N_BEST} 组组合\n")

    t0 = time.time()
    best_results = []

    with ProcessPoolExecutor(max_workers=min(N_CPUS, 16),
                             initializer=init_worker,
                             initargs=(code_price_arrays, code_date_pos, signal_tuples)) as executor:
        futures = [executor.submit(simulate_single, p) for p in best_combos]
        for i, f in enumerate(as_completed(futures)):
            r = f.result()
            if r:
                best_results.append(r)
            if (i + 1) % 30 == 0 or (i + 1) == N_BEST:
                print(f"  进度: {i+1}/{N_BEST} ({time.time()-t0:.0f}s)")

    print(f"\n  完成! 耗时 {time.time()-t0:.0f}s\n")

    # ── 综合排名 ──────────────────────────────────────────
    sorted_best = sorted(best_results + results, key=lambda x: x["profit_factor"], reverse=True)

    print(f"{'='*100}")
    print(f"{'综合排名 Top 25 (按 PF)':^100}")
    print(f"{'='*100}")
    print(f"{'#':<3} {'策略':<50} {'交易':>6} {'胜率':>6} {'avg%':>7} {'med%':>7} {'PF':>7} {'总收益':>10}")
    print(f"{'─'*100}")
    for i, r in enumerate(sorted_best[:25]):
        print(f"{i+1:<3} {r['name']:<50} {r['trades']:>6} {r['win_rate']:>5.1f}% "
              f"{r['avg_return']:>+6.2f}% {r['med_return']:>+6.2f}% "
              f"{r['profit_factor']:>6.2f} {r['total_return']:>+9.0f}%")
    print("=" * 100)

    # ── 最终推荐 ──────────────────────────────────────────
    best_pf = sorted_best[0]
    best_avg = sorted(best_results + results, key=lambda x: x["avg_return"], reverse=True)[0]
    baseline = next((r for r in results if r['name'] == '基线_hold=10d'), None)

    print(f"\n{'='*50}")
    print("最终推荐")
    print(f"{'='*50}")
    print(f"\n🥇 最高 PF:  {best_pf['name']}")
    print(f"   交易={best_pf['trades']}, 胜率={best_pf['win_rate']:.1f}%, "
          f"avg={best_pf['avg_return']:.2f}%, PF={best_pf['profit_factor']:.2f}")
    print(f"\n🥇 最高平均收益: {best_avg['name']}")
    print(f"   交易={best_avg['trades']}, 胜率={best_avg['win_rate']:.1f}%, "
          f"avg={best_avg['avg_return']:.2f}%, PF={best_avg['profit_factor']:.2f}")
    if baseline:
        print(f"\n📊 基线 (固定10天): 交易={baseline['trades']}, "
              f"胜率={baseline['win_rate']:.1f}%, avg={baseline['avg_return']:.2f}%, "
              f"PF={baseline['profit_factor']:.2f}")
    print(f"\n{'='*50}")

if __name__ == '__main__':
    main()
