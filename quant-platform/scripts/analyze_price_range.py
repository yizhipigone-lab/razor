#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
组合8 (RPS>80) 信号 — 价格区间盈利性分析
时间段: 2025-01-01 ~ 2026-04-29
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

# ── 加载 K 线 ─────────────────────────────────────────────
print("加载 K 线数据 ...")
bars = db.load_all_bars(freq="daily", start=LOAD_START, end=END)
bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
for c in ["open","high","low","close","volume"]:
    if c in bars: bars[c] = pd.to_numeric(bars[c], errors='coerce')
bars = bars.dropna(subset=["close"])
bars["date"] = pd.to_datetime(bars["date"]).dt.date
bars = bars.sort_values(["code","date"])
print(f"  共 {bars['code'].nunique():,} 只股票, {len(bars):,} 行\n")

# ── RPS ────────────────────────────────────────────────────
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
print(f"  RPS>80 股票: {len(rps_high_set):,} 只\n")

# ── 生成信号 ──────────────────────────────────────────────
print("生成原始策略信号 ...")
sig = generate_signals(bars, version="original")
sig = sig[(sig["date"] >= START) & (sig["date"] <= END)].copy()
sig["date"] = pd.to_datetime(sig["date"]).dt.date
sig["buy_price"] = sig["close"].astype(float)

# 组合8: RPS>80 过滤
sig = sig[sig["code"].isin(rps_high_set)].copy()
print(f"  组合8 信号数: {len(sig)}\n")

# ── 模拟交易 ──────────────────────────────────────────────
def simulate_trades(signals, bars_df, hold_days=10):
    trades = []
    for _, row in signals.iterrows():
        code = row["code"]
        buy_date = row["date"]
        entry = float(row["buy_price"])
        if entry <= 0:
            continue
        stock_bars = bars_df[bars_df["code"] == code].sort_values("date")
        idx = stock_bars[stock_bars["date"] == buy_date].index
        if len(idx) == 0:
            continue
        pos = stock_bars.index.get_loc(idx[0])
        exit_idx = pos + hold_days
        if exit_idx >= len(stock_bars):
            continue
        exit_price = float(stock_bars.iloc[exit_idx]["close"])
        ret = (exit_price / entry - 1) * 100
        hold = (pd.Timestamp(stock_bars.iloc[exit_idx]["date"]) - pd.Timestamp(buy_date)).days
        trades.append({
            "code": code, "buy_date": str(buy_date), "buy_price": entry,
            "exit_date": str(stock_bars.iloc[exit_idx]["date"]),
            "hold_days": hold, "return_pct": ret, "win": ret > 0,
        })
    return pd.DataFrame(trades)

print("模拟交易 ...")
trades = simulate_trades(sig, bars, hold_days=10)
print(f"  {len(trades)} 笔交易\n")

# ── 价格区间分桶 ──────────────────────────────────────────
bins = [0, 5, 10, 15, 20, 30, 50, 100, float('inf')]
labels = ['0~5', '5~10', '10~15', '15~20', '20~30', '30~50', '50~100', '100+']
trades["price_bucket"] = pd.cut(trades["buy_price"], bins=bins, labels=labels, right=False)

print("=" * 95)
print(f"{'价格区间':>10} {'信号数':>8} {'交易数':>8} {'胜率':>8} {'平均收益':>10} {'中位收益':>10} {'PF':>8} {'占比':>8}")
print("=" * 95)

total_trades = len(trades)
for label in labels:
    t = trades[trades["price_bucket"] == label]
    n_trd = len(t)
    if n_trd == 0:
        print(f"{label:>10} {0:>8} {0:>8} {'N/A':>8} {'N/A':>10} {'N/A':>10} {'N/A':>8} {'0%':>8}")
        continue
    wr = t["win"].mean() * 100
    avg = t["return_pct"].mean()
    med = t["return_pct"].median()
    gains = t[t["win"]]["return_pct"].sum()
    losses = t[~t["win"]]["return_pct"].sum()
    pf = abs(gains / losses) if losses != 0 else float('inf')
    pct = n_trd / total_trades * 100
    print(f"{label:>10} {n_trd:>8} {n_trd:>8} {wr:>7.1f}% {avg:>+9.2f}% {med:>+9.2f}% {pf:>7.2f} {pct:>7.1f}%")

print("=" * 95)
print(f"{'总计':>10} {len(sig):>8} {total_trades:>8} {trades['win'].mean()*100:>7.1f}% {trades['return_pct'].mean():>+9.2f}% {trades['return_pct'].median():>+9.2f}% ", end="")
gains = trades[trades["win"]]["return_pct"].sum()
losses = trades[~trades["win"]]["return_pct"].sum()
pf_total = abs(gains / losses) if losses != 0 else float('inf')
print(f"{pf_total:>7.2f} 100.0%")
print("=" * 95)

# ── 额外：按信号数量排名前 50 的股票 ────────────────────
print("\n\n信号最多的 Top 20 股票:")
top_stocks = sig["code"].value_counts().head(20)
for code, count in top_stocks.items():
    t = trades[trades["code"] == code]
    if len(t) == 0:
        continue
    wr = t["win"].mean() * 100
    avg = t["return_pct"].mean()
    print(f"  {code}: {count} 次信号, {len(t)} 笔交易, WR={wr:.1f}%, avg={avg:+.2f}%")
