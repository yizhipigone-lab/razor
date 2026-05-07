#!/usr/bin/env python
"""
MA5 角度策略 — 原版 VS 改进版 信号对比回测
运行: cd quant-platform && python scripts/compare_ma5_angle.py
"""
import sys, os
# Fix GBK encoding for Windows console
sys.stdout.reconfigure(encoding='utf-8', errors='replace') if hasattr(sys.stdout, 'reconfigure') else None
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import numpy as np
from datetime import date, timedelta, datetime
from database.duckdb_manager import db
from app.screener.strategies.ma5_angle import generate_signals

# ── 配置 ──────────────────────────────────────────────────
END   = date(2026, 4, 29)
START = date(2025, 11, 1)   # 回测区间 ~6 个月
BUFFER = 365                  # MA60/斜率计算需要历史缓冲
LOAD_START = START - timedelta(days=BUFFER)

# ── 加载数据 ──────────────────────────────────────────────
print(f"加载 K 线数据 {LOAD_START} ~ {END} ...")
bars = db.load_all_bars(freq="daily", start=LOAD_START, end=END)
bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
for c in ["open", "high", "low", "close", "volume"]:
    if c in bars: bars[c] = pd.to_numeric(bars[c], errors='coerce')
bars = bars.dropna(subset=["close"])
bars["date"] = pd.to_datetime(bars["date"]).dt.date
bars = bars.sort_values(["code", "date"])
print(f"  共 {bars['code'].nunique()} 只股票, {len(bars)} 行\n")

# ── 运行两个版本 ──────────────────────────────────────────
results = {}
for version in ["original", "improved"]:
    print(f"▶ 运行 {version} 版本 ...")
    sig = generate_signals(bars, version=version)
    # 裁剪到回测区间
    sig = sig[(sig["date"] >= START) & (sig["date"] <= END)].copy()
    results[version] = sig
    print(f"  信号数: {len(sig)}")

# ── 模拟交易：买入后持有 N 天 ──────────────────────────────
def simulate_trades(signals, bars_df, hold_days=[5, 10, 20]):
    """简单模拟：信号日收盘买入，持有 N 天后收盘卖出"""
    bars_lookup = bars_df.set_index(["code", "date"]).sort_index()
    trades = []
    for _, row in signals.iterrows():
        code = row["code"]
        buy_date = row["date"]
        buy_price = float(row["close"])
        if buy_price <= 0:
            continue
        for h in hold_days:
            # 计算 N 个交易日后的日期
            stock_bars = bars_df[bars_df["code"] == code].sort_values("date")
            idx = stock_bars[stock_bars["date"] == buy_date].index
            if len(idx) == 0:
                continue
            pos = stock_bars.index.get_loc(idx[0])
            sell_idx = pos + h
            if sell_idx >= len(stock_bars):
                continue
            sell_row = stock_bars.iloc[sell_idx]
            sell_price = float(sell_row["close"])
            ret = (sell_price / buy_price - 1) * 100
            trades.append({
                "code": code, "buy_date": str(buy_date),
                "sell_date": str(sell_row["date"]),
                "hold_days": h, "return_pct": ret, "win": ret > 0,
            })
    return pd.DataFrame(trades)

print("\n模拟交易...")
for v in ["original", "improved"]:
    trades = simulate_trades(results[v], bars)
    results[f"{v}_trades"] = trades

# ── 输出对比 ──────────────────────────────────────────────
print("\n" + "=" * 70)
print(f"{'指标':<30} {'原版':>15} {'改进版':>15}")
print("=" * 70)

for v_label, v_key in [("原版", "original"), ("改进版", "improved")]:
    sig = results[v_key]
    trades = results[f"{v_key}_trades"]
    n_sig = len(sig)
    avg_pct_5 = trades[trades["hold_days"] == 5]["return_pct"].mean() if not trades.empty else 0
    avg_pct_10 = trades[trades["hold_days"] == 10]["return_pct"].mean() if not trades.empty else 0
    avg_pct_20 = trades[trades["hold_days"] == 20]["return_pct"].mean() if not trades.empty else 0
    wr_5 = trades[trades["hold_days"] == 5]["win"].mean() * 100 if not trades.empty else 0
    wr_10 = trades[trades["hold_days"] == 10]["win"].mean() * 100 if not trades.empty else 0
    wr_20 = trades[trades["hold_days"] == 20]["win"].mean() * 100 if not trades.empty else 0

    if v_label == "原版":
        orig = [n_sig, avg_pct_5, avg_pct_10, avg_pct_20, wr_5, wr_10, wr_20]
    else:
        impr = [n_sig, avg_pct_5, avg_pct_10, avg_pct_20, wr_5, wr_10, wr_20]

rows = [
    ("信号数量", f"{orig[0]:>15}", f"{impr[0]:>15}"),
    ("持有5天 平均收益%", f"{orig[1]:>15.2f}", f"{impr[1]:>15.2f}"),
    ("持有10天 平均收益%", f"{orig[2]:>15.2f}", f"{impr[2]:>15.2f}"),
    ("持有20天 平均收益%", f"{orig[3]:>15.2f}", f"{impr[3]:>15.2f}"),
    ("持有5天 胜率%", f"{orig[4]:>15.1f}", f"{impr[4]:>15.1f}"),
    ("持有10天 胜率%", f"{orig[5]:>15.1f}", f"{impr[5]:>15.1f}"),
    ("持有20天 胜率%", f"{orig[6]:>15.1f}", f"{impr[6]:>15.1f}"),
]
for label, o, i in rows:
    print(f"{label:<30} {o} {i}")
print("=" * 70)
