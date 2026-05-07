#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度原版策略 — 多维度增强对比
验证：成分股、概念股、RPS、止损 对胜率和收益的影响
"""
import sys, os
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import numpy as np
from datetime import date, timedelta, datetime
from database.duckdb_manager import db
from app.screener.strategies.ma5_angle import generate_signals

# ── 配置 ──────────────────────────────────────────────────
END   = date(2026, 4, 29)
START = date(2025, 11, 1)
BUFFER = 365
LOAD_START = START - timedelta(days=BUFFER)

# ── 加载全部 K 线 ─────────────────────────────────────────
print("加载 K 线数据 ...")
bars = db.load_all_bars(freq="daily", start=LOAD_START, end=END)
bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
for c in ["open","high","low","close","volume"]:
    if c in bars: bars[c] = pd.to_numeric(bars[c], errors='coerce')
bars = bars.dropna(subset=["close"])
bars["date"] = pd.to_datetime(bars["date"]).dt.date
bars = bars.sort_values(["code","date"])
print(f"  共 {bars['code'].nunique():,} 只股票, {len(bars):,} 行\n")

# ── 加载辅助数据 ──────────────────────────────────────────
print("加载成分股、概念股数据 ...")

# 成分股：出现在任意主流指数中的股票
index_codes = db.conn.execute("""
    SELECT DISTINCT stock_code FROM index_members
""").df()["stock_code"].tolist()
index_set = set(index_codes)
print(f"  成分股: {len(index_set):,} 只")

# 概念股：至少有一个概念的股票
concept_codes = db.conn.execute("""
    SELECT DISTINCT stock_code FROM concept_stocks
""").df()["stock_code"].tolist()
concept_set = set(concept_codes)
print(f"  有概念股票: {len(concept_set):,} 只")

# 交集: 既是成分股又有概念的
both_set = index_set & concept_set
print(f"  成分+概念: {len(both_set):,} 只\n")

# ── 生成原始策略信号 ──────────────────────────────────────
print("生成原版策略信号 ...")
sig = generate_signals(bars, version="original")
sig = sig[(sig["date"] >= START) & (sig["date"] <= END)].copy()
sig["date"] = pd.to_datetime(sig["date"]).dt.date
# 信号日收盘价
sig["buy_price"] = sig["close"].astype(float)
print(f"  总信号数: {len(sig)}\n")

# ── RPS 计算（120 日收益排名） ────────────────────────────
print("计算 RPS ...")
# 取每个股票最新和 120 交易日前收盘价
codes_all = bars["code"].unique()
rps_values = {}
for code in codes_all:
    cb = bars[bars["code"] == code].sort_values("date")
    if len(cb) < 120:
        continue
    latest = cb.iloc[-1]["close"]
    base = cb.iloc[-120]["close"]
    ret = latest / base - 1 if base > 0 else -1
    rps_values[code] = ret
# 排序赋分
if rps_values:
    rps_series = pd.Series(rps_values)
    rps_rank = rps_series.rank(pct=True) * 100
    rps_high_set = set(rps_rank[rps_rank > 80].index.tolist())
else:
    rps_high_set = set()
print(f"  RPS>80 股票: {len(rps_high_set):,} 只\n")


# ── 信号过滤函数 ──────────────────────────────────────────
def filter_signals(signals, code_set, name):
    """按股票代码集过滤信号"""
    before = len(signals)
    filtered = signals[signals["code"].isin(code_set)].copy()
    print(f"  {name}: {before} -> {len(filtered)} 信号 ({len(filtered)/before*100:.1f}%)" if before else "")
    return filtered

def simulate_trades(signals, bars_df, hold_days=10, stop_loss=None, take_profit=None):
    """
    模拟交易
    stop_loss: float, e.g. -5.0 = -5%
    take_profit: float, e.g. 10.0 = +10%
    返回交易记录DataFrame
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

        # 从信号日之后遍历
        exit_price = None
        exit_date = None
        max_pct = 0.0

        for i in range(pos + 1, min(pos + 1 + hold_days * 2, len(stock_bars))):
            row_i = stock_bars.iloc[i]
            price = float(row_i["close"])
            pnl_pct = (price / entry - 1) * 100
            max_pct = max(max_pct, pnl_pct)

            # 止损
            if stop_loss is not None and pnl_pct <= stop_loss:
                exit_price = price
                exit_date = row_i["date"]
                break
            # 止盈
            if take_profit is not None and pnl_pct >= take_profit:
                exit_price = price
                exit_date = row_i["date"]
                break
            # 持有到期
            if (i - pos) >= hold_days:
                exit_price = price
                exit_date = row_i["date"]
                break

        if exit_price is None:
            continue

        ret_pct = (exit_price / entry - 1) * 100
        hold = (pd.Timestamp(exit_date) - pd.Timestamp(buy_date)).days
        trades.append({
            "code": code, "buy_date": str(buy_date), "exit_date": str(exit_date),
            "hold_days": hold, "return_pct": ret_pct, "win": ret_pct > 0,
        })
    return pd.DataFrame(trades)


# ── 定义测试组合 ──────────────────────────────────────────
combos = [
    # (name, code_filter, stop_loss, take_profit, rps_filter)
    ("1. 原始策略（无过滤）",       None,               None,  None,  False),
    ("2. + 成分股",                index_set,           None,  None,  False),
    ("3. + 概念股",                concept_set,         None,  None,  False),
    ("4. + 成分+概念",             both_set,            None,  None,  False),
    ("5. + 成分+概念+RPS>80",      both_set,            None,  None,  True),
    ("6. + 成分+概念+RPS+止损",    both_set,            -5.0,  10.0,  True),
    ("7. + 成分+概念+止损",        both_set,            -5.0,  10.0,  False),
    ("8. + RPS>80",                None,                None,  None,  True),
    ("9. + 止损(仅-5%/+10%)",      None,                -5.0,  10.0,  False),
]

results_list = []

for name, code_filter, sl, tp, rps_only in combos:
    s = sig.copy()
    if code_filter is not None:
        s = s[s["code"].isin(code_filter)]
    if rps_only:
        s = s[s["code"].isin(rps_high_set)]
    if len(s) == 0:
        print(f"{name}: 0 信号，跳过")
        continue

    trades = simulate_trades(s, bars, hold_days=10, stop_loss=sl, take_profit=tp)

    n_sig = len(s)
    n_trades = len(trades)
    if n_trades == 0:
        print(f"{name}: {n_sig} 信号, 0 交易")
        continue

    win_rate = trades["win"].mean() * 100
    avg_ret = trades["return_pct"].mean()
    med_ret = trades["return_pct"].median()
    avg_hold = trades["hold_days"].mean()
    max_ret = trades["return_pct"].max()
    min_ret = trades["return_pct"].min()
    profit_factor = abs(trades[trades["win"]]["return_pct"].sum() /
                        trades[~trades["win"]]["return_pct"].sum()) if (~trades["win"]).sum() > 0 else float('inf')

    results_list.append({
        "name": name,
        "signals": n_sig,
        "trades": n_trades,
        "win_rate": win_rate,
        "avg_return": avg_ret,
        "med_return": med_ret,
        "avg_hold_days": avg_hold,
        "max_return": max_ret,
        "min_return": min_ret,
        "profit_factor": profit_factor,
    })
    print(f"  {name}: {n_trades} trades, WR={win_rate:.1f}%, avg={avg_ret:.2f}%, PF={profit_factor:.2f}")


# ── 输出表格 ──────────────────────────────────────────────
print("\n" + "=" * 100)
header = f"{'组合':<30} {'信号':>6} {'交易':>6} {'胜率':>7} {'平均收益':>8} {'中位收益':>8} {'持有时':>6} {'PF':>6}"
print(header)
print("=" * 100)
for r in results_list:
    print(f"{r['name']:<30} {r['signals']:>6} {r['trades']:>6} "
          f"{r['win_rate']:>6.1f}% {r['avg_return']:>+7.2f}% {r['med_return']:>+7.2f}% "
          f"{r['avg_hold_days']:>5.1f}d {r['profit_factor']:>5.2f}")
print("=" * 100)

# ── 最佳三条总结 ──────────────────────────────────────────
best = sorted(results_list, key=lambda x: x["win_rate"], reverse=True)
print("\n胜率排名:")
for i, r in enumerate(best[:3], 1):
    print(f"  {i}. {r['name']}: 胜率={r['win_rate']:.1f}%  平均收益={r['avg_return']:+.2f}%")
