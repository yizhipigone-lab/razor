#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
组合8 (RPS>80) 信号 — 月度分布分析
"""
import sys, os
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import numpy as np
from datetime import date, timedelta
from database.duckdb_manager import db
from app.screener.strategies.ma5_angle import generate_signals

END   = date(2026, 4, 29)
START = date(2025, 1, 1)
LOAD_START = START - timedelta(days=365)

print("加载 K 线数据 ...")
bars = db.load_all_bars(freq="daily", start=LOAD_START, end=END)
bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
for c in ["open","high","low","close","volume"]:
    if c in bars: bars[c] = pd.to_numeric(bars[c], errors='coerce')
bars = bars.dropna(subset=["close"])
bars["date"] = pd.to_datetime(bars["date"]).dt.date
bars = bars.sort_values(["code","date"])

print("计算 RPS ...")
rps_values = {}
for code in bars["code"].unique():
    cb = bars[bars["code"] == code].sort_values("date")
    if len(cb) < 120: continue
    rps_values[code] = cb.iloc[-1]["close"] / cb.iloc[-120]["close"] - 1
rps_series = pd.Series(rps_values)
rps_rank = rps_series.rank(pct=True) * 100
rps_high_set = set(rps_rank[rps_rank > 80].index.tolist())

print("生成信号 ...")
sig = generate_signals(bars, version="original")
sig = sig[(sig["date"] >= START) & (sig["date"] <= END)].copy()
sig = sig[sig["code"].isin(rps_high_set)].copy()
# Keep dates as datetime.date for consistent lookups
print(f"  组合8 总信号数: {len(sig)}\n")

# ── 构建高性能索引 ──────────────────────────────────────
code_data = {}
for code in bars["code"].unique():
    cb = bars[bars["code"] == code].sort_values("date")
    code_data[code] = {
        "prices": cb["close"].values.astype(float),
        "date_pos": {d: i for i, d in enumerate(cb["date"].values)}
    }

# ── 月度统计（直接从信号看分布）───────────────────────────
month_counts = {}
for _, row in sig.iterrows():
    ym = row["date"].strftime("%Y-%m")
    month_counts[ym] = month_counts.get(ym, 0) + 1

sorted_months = sorted(month_counts.keys())
total_sig = len(sig)

print(f"\n{'='*55}")
print(f"信号月度分布（共 {total_sig} 个信号）")
print(f"{'='*55}")
print(f"{'月份':<10} {'信号数':>6} {'占比':>7}")
print(f"{'-'*55}")
for ym in sorted_months:
    n = month_counts[ym]
    pct = n / total_sig * 100
    bar_len = int(pct / 2)
    bar = "█" * bar_len
    print(f"{ym:<10} {n:>6} ({pct:>4.1f}%) {bar}")
print(f"{'='*55}")

values = np.array(list(month_counts.values()))
print(f"\n  月均: {values.mean():.1f} 个")
print(f"  最多: {values.max()} 个 ({sorted_months[values.argmax()]})")
print(f"  最少: {values.min()} 个 ({sorted_months[values.argmin()]})")
print(f"  标准差: {values.std():.1f}")
print(f"  变异系数: {values.std()/values.mean()*100:.1f}%")
print(f"  月均 < 50 个: {(values < 50).sum()}/{len(values)} 个月")

# ── 每月胜率模拟 ──────────────────────────────────────────
print(f"\n{'='*55}")
print("月度交易胜率（持有10天）")
print(f"{'='*55}")
print(f"{'月份':<10} {'交易数':>6} {'胜率':>7} {'avg%':>8}")
print(f"{'-'*55}")

for ym in sorted_months:
    month_trades = []
    for _, row in sig.iterrows():
        if row["date"].strftime("%Y-%m") != ym:
            continue
        code = row["code"]
        cd = code_data.get(code)
        if cd is None: continue
        pos = cd["date_pos"].get(row["date"])
        if pos is None or pos + 10 >= len(cd["prices"]): continue
        ret = (cd["prices"][pos + 10] / float(row["close"]) - 1) * 100
        month_trades.append(ret)

    if not month_trades:
        print(f"{ym:<10} {0:>6} {'N/A':>7} {'N/A':>8}")
        continue

    arr = np.array(month_trades)
    wr = (arr > 0).mean() * 100
    avg = arr.mean()
    print(f"{ym:<10} {len(arr):>6} {wr:>6.1f}% {avg:>+7.2f}%")
print(f"{'='*55}")
