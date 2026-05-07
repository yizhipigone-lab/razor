#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度 — 市场环境过滤（大盘 MA120）对 PF 的影响
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
BUFFER = 400  # 多给点缓冲，MA120 需要至少 120+ 数据
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

# ── 加载沪深 300 指数数据 ─────────────────────────────────
print("加载沪深 300 指数数据计算 MA120 ...")
index_bars = db.load_bars("000300.SH", freq="daily")
if index_bars is None or index_bars.empty:
    # 兜底直接读取 parquet
    print("  db.load_bars 返回空，直接读 parquet ...")
    index_bars = pd.read_parquet(os.path.join(os.path.dirname(__file__),
        "..", "data", "parquet", "daily", "index_000300.SH.parquet"))
index_bars = index_bars.copy()
index_bars["date"] = pd.to_datetime(index_bars["date"]).dt.date
index_bars = index_bars.sort_values("date")
index_bars["ma120"] = index_bars["close"].rolling(120).mean()

# 构建 date -> (close, ma120) 查询字典
index_map = {}
for _, r in index_bars.iterrows():
    index_map[r["date"]] = {"close": r["close"], "ma120": r["ma120"]}
print(f"  沪深 300 数据: {len(index_bars)} 行, MA120 可用日期: {sum(1 for v in index_map.values() if not pd.isna(v['ma120'])):,}\n")

# ── 加载成分股 + 概念股 ──────────────────────────────────
print("加载成分股、概念股 ...")
index_codes = db.conn.execute(
    "SELECT DISTINCT stock_code FROM index_members"
).df()["stock_code"].tolist()
concept_codes = db.conn.execute(
    "SELECT DISTINCT stock_code FROM concept_stocks"
).df()["stock_code"].tolist()
both_set = set(index_codes) & set(concept_codes)
rps_high_set = set()

# ── RPS 计算 ──────────────────────────────────────────────
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
print(f"  RPS>80 股票: {len(rps_high_set):,} 只\n")

# ── 生成原始策略信号 ──────────────────────────────────────
print("生成原始策略信号 ...")
sig = generate_signals(bars, version="original")
sig = sig[(sig["date"] >= START) & (sig["date"] <= END)].copy()
sig["date"] = pd.to_datetime(sig["date"]).dt.date
sig["buy_price"] = sig["close"].astype(float)
print(f"  总信号数: {len(sig):,}\n")

# ── 市场环境过滤函数 ──────────────────────────────────────
def market_filter_ma120(row):
    """检查信号日沪深 300 是否在 MA120 上方"""
    d = row["date"]
    if d not in index_map:
        return True  # 没有指数数据，不过滤
    v = index_map[d]
    if pd.isna(v["ma120"]):
        return True
    return v["close"] > v["ma120"]

def apply_market_filter(df, name):
    before = len(df)
    mask = df.apply(market_filter_ma120, axis=1)
    df_filtered = df[mask].copy()
    after = len(df_filtered)
    print(f"  {name}: {before} -> {after} 信号 (过滤掉 {before - after}, {((before-after)/before*100):.1f}%)")
    return df_filtered

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

def calc_metrics(signals, trades, name):
    n_sig = len(signals)
    n_trd = len(trades)
    if n_trd == 0:
        return None
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
    return {"name": name, "signals": n_sig, "trades": n_trd, "win_rate": wr,
            "avg_return": avg, "med_return": med, "max_return": mx, "min_return": mn,
            "profit_factor": pf, "avg_hold_days": avg_hold, "total_return": total_ret}

# ── 4 个组合对比 ─────────────────────────────────────────
combos = [
    ("组合8 RPS>80",           sig[sig["code"].isin(rps_high_set)].copy(), False),
    ("组合8 + 大盘MA120",      sig[sig["code"].isin(rps_high_set)].copy(), True),
    ("组合5 成分+概念+RPS",    sig[sig["code"].isin(both_set) & sig["code"].isin(rps_high_set)].copy(), False),
    ("组合5 + 大盘MA120",      sig[sig["code"].isin(both_set) & sig["code"].isin(rps_high_set)].copy(), True),
]

results = []
print("模拟交易 ...")
for name, signals, use_market_filter in combos:
    if use_market_filter:
        signals = apply_market_filter(signals, name)
    trades = simulate_trades(signals, bars, hold_days=10)
    m = calc_metrics(signals, trades, name)
    if m:
        results.append(m)
        print(f"  {name}: {m['trades']} 交易, WR={m['win_rate']:.1f}%, avg={m['avg_return']:.2f}%, PF={m['profit_factor']:.2f}")
    else:
        print(f"  {name}: 0 交易, 跳过")

# ── 并排输出 ──────────────────────────────────────────────
print("\n" + "=" * 120)
print(f"{'指标':<16} {'组合8':>16} {'组合8+MA120':>18} {'变化':>10} {'组合5':>16} {'组合5+MA120':>18} {'变化':>10}")
print("=" * 120)

def get(name):
    for r in results:
        if r["name"] == name:
            return r
    return None

keys = ["signals", "trades", "win_rate", "avg_return", "med_return",
        "max_return", "min_return", "avg_hold_days", "profit_factor", "total_return"]
labels = {
    "signals": "信号数", "trades": "交易数", "win_rate": "胜率",
    "avg_return": "平均收益", "med_return": "中位收益",
    "max_return": "最大收益", "min_return": "最小收益",
    "avg_hold_days": "持有天数", "profit_factor": "PF", "total_return": "总收益",
}
fmts = {
    "signals": "{:>10,}", "trades": "{:>10,}", "win_rate": "{:>9.1f}%",
    "avg_return": "{:>+9.2f}%", "med_return": "{:>+9.2f}%",
    "max_return": "{:>+9.2f}%", "min_return": "{:>+9.2f}%",
    "avg_hold_days": "{:>10.1f}", "profit_factor": "{:>9.2f}", "total_return": "{:>+10.2f}%",
}

for k in keys:
    r8 = get("组合8 RPS>80")
    r8m = get("组合8 + 大盘MA120")
    r5 = get("组合5 成分+概念+RPS")
    r5m = get("组合5 + 大盘MA120")

    v8 = fmts[k].format(r8[k]) if r8 else "N/A"
    v8m = fmts[k].format(r8m[k]) if r8m else "N/A"
    v5 = fmts[k].format(r5[k]) if r5 else "N/A"
    v5m = fmts[k].format(r5m[k]) if r5m else "N/A"

    # 变化
    if r8 and r8m:
        if k in ("win_rate", "avg_return", "med_return", "profit_factor", "total_return"):
            chg8 = r8m[k] - r8[k]
            v_chg8 = f"{chg8:>+9.2f}" if k != "win_rate" else f"{chg8:>+8.1f}pp"
        else:
            v_chg8 = f"{(r8m[k]/r8[k]-1)*100:>+9.0f}%" if r8[k] else "   N/A"
    else:
        v_chg8 = "   N/A"

    if r5 and r5m:
        if k in ("win_rate", "avg_return", "med_return", "profit_factor", "total_return"):
            chg5 = r5m[k] - r5[k]
            v_chg5 = f"{chg5:>+9.2f}" if k != "win_rate" else f"{chg5:>+8.1f}pp"
        else:
            v_chg5 = f"{(r5m[k]/r5[k]-1)*100:>+9.0f}%" if r5[k] else "   N/A"
    else:
        v_chg5 = "   N/A"

    print(f"{labels[k]:<16} {v8:>16} {v8m:>18} {v_chg8:>10} {v5:>16} {v5m:>18} {v_chg5:>10}")

print("=" * 120)
