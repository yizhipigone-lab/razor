#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
组合8 (RPS>80) — 动态止盈 vs 固定持有对比
动态止盈: 触发 +5% 后启动移动止盈，从最高点回撤 3% 卖出
最長持有 20 天
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
print(f"  {bars['code'].nunique():,} 只股票, {len(bars):,} 行\n")

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
print(f"  RPS>80: {len(rps_high_set):,} 只\n")

# ── 生成信号 ──────────────────────────────────────────────
print("生成原始策略信号 ...")
sig = generate_signals(bars, version="original")
sig = sig[(sig["date"] >= START) & (sig["date"] <= END)].copy()
sig["date"] = pd.to_datetime(sig["date"]).dt.date
sig["buy_price"] = sig["close"].astype(float)
sig = sig[sig["code"].isin(rps_high_set)].copy()
print(f"  组合8 信号数: {len(sig)}\n")

# ── 模拟交易（支持动态止盈） ──────────────────────────────
def simulate_trades(signals, bars_df, mode="fixed", hold_days=10,
                    trail_activate=5.0, trail_drawdown=3.0, max_hold=20):
    """
    mode: 'fixed' = 固定持有, 'trailing' = 动态止盈
    trail_activate: 盈利多少 % 后启动移动止盈
    trail_drawdown: 从最高点回撤多少 % 卖出
    """
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

        exit_price = None
        exit_date = None
        max_pnl = 0.0
        trailing_active = False

        for i in range(pos + 1, min(pos + 1 + max_hold * 2, len(stock_bars))):
            row_i = stock_bars.iloc[i]
            price = float(row_i["close"])
            pnl = (price / entry - 1) * 100

            if mode == "fixed":
                # 持有到期
                if (i - pos) >= hold_days:
                    exit_price = price
                    exit_date = row_i["date"]
                    break
            elif mode == "trailing":
                # 更新最高点
                if pnl > max_pnl:
                    max_pnl = pnl

                # 是否激活移动止盈
                if pnl >= trail_activate:
                    trailing_active = True

                # 激活后回撤达标 → 卖出
                if trailing_active and (max_pnl - pnl) >= trail_drawdown:
                    exit_price = price
                    exit_date = row_i["date"]
                    break

                # 最长持有到期
                if (i - pos) >= max_hold:
                    exit_price = price
                    exit_date = row_i["date"]
                    break

        if exit_price is None:
            continue

        ret = (exit_price / entry - 1) * 100
        hold = (pd.Timestamp(exit_date) - pd.Timestamp(buy_date)).days
        trades.append({
            "code": code, "buy_price": entry,
            "buy_date": str(buy_date), "exit_date": str(exit_date),
            "hold_days": hold, "return_pct": ret, "win": ret > 0,
            "max_pnl": max_pnl,
        })
    return pd.DataFrame(trades)

# ── 测试 4 种模式 ─────────────────────────────────────────
configs = [
    ("固定持有 10 天",       "fixed",    10,   5.0,  3.0,  20),
    ("移动止盈 +5%/回撤3%",  "trailing", 10,   5.0,  3.0,  20),
    ("移动止盈 +3%/回撤2%",  "trailing", 10,   3.0,  2.0,  20),
    ("移动止盈 +8%/回撤4%",  "trailing", 10,   8.0,  4.0,  20),
]

def calc(trades):
    if len(trades) == 0:
        return {"trades": 0}
    wr = trades["win"].mean() * 100
    avg = trades["return_pct"].mean()
    med = trades["return_pct"].median()
    mx = trades["return_pct"].max()
    mn = trades["return_pct"].min()
    gains = trades[trades["win"]]["return_pct"].sum()
    losses = trades[~trades["win"]]["return_pct"].sum()
    pf = abs(gains / losses) if losses != 0 else float('inf')
    avg_hold = trades["hold_days"].mean()
    total_ret = trades["return_pct"].sum()
    return {"trades": len(trades), "win_rate": wr, "avg_return": avg,
            "med_return": med, "max_return": mx, "min_return": mn,
            "profit_factor": pf, "avg_hold_days": avg_hold, "total_return": total_ret}

all_results = {}
for name, mode, hd, ta, td, mh in configs:
    trades = simulate_trades(sig, bars, mode=mode, hold_days=hd,
                             trail_activate=ta, trail_drawdown=td, max_hold=mh)
    m = calc(trades)
    all_results[name] = (trades, m)
    print(f"  {name}: {m['trades']} 交易, WR={m['win_rate']:.1f}%, "
          f"avg={m['avg_return']:.2f}%, med={m['med_return']:.2f}%, PF={m['profit_factor']:.2f}")

# ── 对比输出 ──────────────────────────────────────────────
print("\n" + "=" * 120)
header = f"{'指标':<16}"
for name, _, _, _, _, _ in configs:
    header += f" {name:>22}"
print(header)
print("=" * 120)

keys = ["trades", "win_rate", "avg_return", "med_return", "profit_factor", "avg_hold_days", "total_return"]
labels = {"trades": "交易数", "win_rate": "胜率", "avg_return": "平均收益",
          "med_return": "中位收益", "profit_factor": "PF", "avg_hold_days": "平均持有",
          "total_return": "总收益"}
fmts = {"trades": "{:>8,}", "win_rate": "{:>8.1f}%", "avg_return": "{:>+8.2f}%",
        "med_return": "{:>+8.2f}%", "profit_factor": "{:>8.2f}", "avg_hold_days": "{:>8.1f}",
        "total_return": "{:>+10.2f}%"}

for k in keys:
    line = f"{labels[k]:<16}"
    for name, _, _, _, _, _ in configs:
        _, m = all_results[name]
        if m["trades"] == 0:
            line += f" {'N/A':>22}"
        else:
            line += f" {fmts[k].format(m[k]):>22}"
    print(line)
print("=" * 120)

# ── 额外：卖出原因分布 ────────────────────────────────────
print("\n\n动态止盈 (+5%/回撤3%) 卖出原因:")
trades_trail, _ = all_results["移动止盈 +5%/回撤3%"]
if len(trades_trail) > 0:
    # 回看: 触发止盈后卖出 vs 持有到期
    triggered = trades_trail[trades_trail["max_pnl"] >= 5.0]
    expired  = trades_trail[trades_trail["max_pnl"] < 5.0]
    print(f"  触发止盈后卖出: {len(triggered)} ({len(triggered)/len(trades_trail)*100:.1f}%), "
          f"WR={triggered['win'].mean()*100:.1f}%, avg={triggered['return_pct'].mean():.2f}%")
    print(f"  持有到期: {len(expired)} ({len(expired)/len(trades_trail)*100:.1f}%), "
          f"WR={expired['win'].mean()*100:.1f}%, avg={expired['return_pct'].mean():.2f}%")
