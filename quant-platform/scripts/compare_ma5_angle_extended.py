#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度 — 组合5 vs 组合8 延长回测
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

# ── 配置 ──────────────────────────────────────────────────
END   = date(2026, 4, 29)
START = date(2025, 1, 1)
BUFFER = 365
LOAD_START = START - timedelta(days=BUFFER)

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

# ── 加载成分股 + 概念股 ──────────────────────────────────
print("加载成分股、概念股 ...")
index_codes = db.conn.execute(
    "SELECT DISTINCT stock_code FROM index_members"
).df()["stock_code"].tolist()
index_set = set(index_codes)

concept_codes = db.conn.execute(
    "SELECT DISTINCT stock_code FROM concept_stocks"
).df()["stock_code"].tolist()
concept_set = set(concept_codes)

both_set = index_set & concept_set
print(f"  成分股: {len(index_set):,}  概念股: {len(concept_set):,}  交集: {len(both_set):,}\n")

# ── 生成原始策略信号 ──────────────────────────────────────
print("生成原始策略信号 ...")
sig = generate_signals(bars, version="original")
sig = sig[(sig["date"] >= START) & (sig["date"] <= END)].copy()
sig["date"] = pd.to_datetime(sig["date"]).dt.date
sig["buy_price"] = sig["close"].astype(float)
total_sigs = len(sig)
print(f"  总信号数: {total_sigs:,}\n")

# ── RPS 计算（120 日收益排名） ────────────────────────────
print("计算 RPS ...")
codes_all = bars["code"].unique()
rps_values = {}
for code in codes_all:
    cb = bars[bars["code"] == code].sort_values("date")
    if len(cb) < 120:
        continue
    ret = cb.iloc[-1]["close"] / cb.iloc[-120]["close"] - 1
    rps_values[code] = ret

if rps_values:
    rps_series = pd.Series(rps_values)
    rps_rank = rps_series.rank(pct=True) * 100
    rps_high_set = set(rps_rank[rps_rank > 80].index.tolist())
else:
    rps_high_set = set()
print(f"  RPS>80 股票: {len(rps_high_set):,} 只\n")

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
            "code": code, "buy_date": str(buy_date),
            "exit_date": str(stock_bars.iloc[exit_idx]["date"]),
            "hold_days": hold, "return_pct": ret, "win": ret > 0,
        })
    return pd.DataFrame(trades)

# ── 组合过滤 ──────────────────────────────────────────────
# 组合8: RPS>80 过滤
s8 = sig[sig["code"].isin(rps_high_set)].copy()
# 组合5: 成分+概念+RPS>80
s5 = sig[sig["code"].isin(both_set)].copy()
s5 = s5[s5["code"].isin(rps_high_set)].copy()

print("模拟交易 ...")
t8 = simulate_trades(s8, bars, hold_days=10)
t5 = simulate_trades(s5, bars, hold_days=10)

# ── 计算指标 ──────────────────────────────────────────────
def calc_metrics(signals, trades, name):
    n_sig = len(signals)
    n_trd = len(trades)
    if n_trd == 0:
        print(f"{name}: {n_sig} 信号, 0 交易")
        return {"signals": n_sig, "trades": 0, "win_rate": 0, "avg_return": 0,
                "med_return": 0, "max_return": 0, "min_return": 0, "profit_factor": 0}

    wr = trades["win"].mean() * 100
    avg = trades["return_pct"].mean()
    med = trades["return_pct"].median()
    mx = trades["return_pct"].max()
    mn = trades["return_pct"].min()
    gains = trades[trades["win"]]["return_pct"].sum()
    losses = trades[~trades["win"]]["return_pct"].sum()
    pf = abs(gains / losses) if losses != 0 else float('inf')
    avg_hold = trades["hold_days"].mean()

    return {"signals": n_sig, "trades": n_trd, "win_rate": wr, "avg_return": avg,
            "med_return": med, "max_return": mx, "min_return": mn, "profit_factor": pf,
            "avg_hold_days": avg_hold}

m8 = calc_metrics(s8, t8, "组合8 (RPS>80)")
m5 = calc_metrics(s5, t5, "组合5 (成分+概念+RPS>80)")

# ── 并排输出 ──────────────────────────────────────────────
print("\n" + "=" * 80)
print(f"{'指标':<20} {'组合8 (RPS>80)':>25} {'组合5 (成分+概念+RPS>80)':>30}")
print("=" * 80)

rows = [
    ("信号数",          f"{m8['signals']:>10,}",     f"{m5['signals']:>10,}"),
    ("交易数",          f"{m8['trades']:>10,}",      f"{m5['trades']:>10,}"),
    ("胜率",            f"{m8['win_rate']:>9.1f}%",  f"{m5['win_rate']:>9.1f}%"),
    ("平均收益",        f"{m8['avg_return']:>+9.2f}%", f"{m5['avg_return']:>+9.2f}%"),
    ("中位收益",        f"{m8['med_return']:>+9.2f}%", f"{m5['med_return']:>+9.2f}%"),
    ("最大收益",        f"{m8['max_return']:>+9.2f}%", f"{m5['max_return']:>+9.2f}%"),
    ("最小收益",        f"{m8['min_return']:>+9.2f}%", f"{m5['min_return']:>+9.2f}%"),
    ("平均持有(天)",    f"{m8['avg_hold_days']:>10.1f}", f"{m5['avg_hold_days']:>10.1f}"),
    ("Profit Factor",  f"{m8['profit_factor']:>10.2f}", f"{m5['profit_factor']:>10.2f}"),
]

for label, v8, v5 in rows:
    print(f"{label:<20} {v8:>25} {v5:>30}")
print("=" * 80)
