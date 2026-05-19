#!/usr/bin/env python
"""检查 min5 数据完整度：2024-01-01 至今，每只股票每天的5分钟线覆盖情况"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta
from pathlib import Path
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

ROOT = Path(__file__).parent.parent
MIN5_DIR = ROOT / "data" / "parquet" / "min5"
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
START = date(2024, 1, 1)
END = date.today()

# ── 获取交易日列表（从日线数据） ──
print("[1/3] Loading trading calendar from daily data...")
td_set = set()
daily_files = list(DAILY_DIR.glob("*.parquet"))
for f in daily_files:
    if not f.stem.isdigit() or len(f.stem) != 6:
        continue
    try:
        df = pd.read_parquet(str(f), columns=['date'])
        for c in df.columns:
            if c.lower() in ('trade_date','datetime','date'):
                dates = pd.to_datetime(df[c]).dt.date
                td_set.update(d for d in dates if START <= d <= END)
                break
    except:
        continue
trading_dates = sorted(td_set)
print(f"  交易日: {len(trading_dates)} ({trading_dates[0]} ~ {trading_dates[-1]})")

# ── 获取有日线数据的股票集合 ──
print("[2/3] Counting stocks with daily data...")
daily_stocks = set()
for f in daily_files:
    if not f.stem.isdigit() or len(f.stem) != 6:
        continue
    try:
        df = pd.read_parquet(str(f), columns=['date'])
        for c in df.columns:
            if c.lower() in ('trade_date','datetime','date'):
                dlist = pd.to_datetime(df[c]).dt.date
                if any(START <= d <= END for d in dlist):
                    daily_stocks.add(f.stem)
                break
    except:
        continue
print(f"  日线覆盖股票: {len(daily_stocks)}")

# ── 逐个 min5 文件统计覆盖 ──
print("[3/3] Scanning min5 files...")
min5_files = list(MIN5_DIR.glob("*.parquet"))
total = len(min5_files)

# stock → set of dates with min5 data
min5_coverage = {}
# 每日覆盖股票数
day_count = defaultdict(int)
# 每只股票覆盖天数
stock_days = defaultdict(int)
# 每只股票平均每天bar数
stock_bar_avg = {}

n = 0
for f in min5_files:
    stem = f.stem
    code = stem[:6] if len(stem) >= 6 and stem[:6].isdigit() else stem
    if not code.isdigit() or len(code) != 6:
        continue
    try:
        df = pd.read_parquet(str(f), columns=['datetime'])
        df['date'] = pd.to_datetime(df['datetime']).dt.date
        dates_in_range = [d for d in df['date'].unique() if START <= d <= END]
        if dates_in_range:
            min5_coverage[code] = set(dates_in_range)
            stock_days[code] = len(dates_in_range)
            for d in dates_in_range:
                day_count[d] += 1
    except Exception:
        continue
    n += 1
    if n % 2000 == 0:
        print(f"  {n}/{total}...")

print(f"\n  min5文件总数: {total}")
print(f"  有效min5覆盖: {len(min5_coverage)} stocks")

# ── 报告 ──
print(f"\n{'='*70}")
print(f"  5分钟线数据完整度报告：{START} ~ {END}")
print(f"{'='*70}")

# 日线覆盖 vs min5覆盖
only_daily = daily_stocks - set(min5_coverage.keys())
both = daily_stocks & set(min5_coverage.keys())
print(f"\n  日线有数据: {len(daily_stocks):,} stocks")
print(f"  min5有数据: {len(min5_coverage):,} stocks")
print(f"  两者都有: {len(both):,} ({len(both)/len(daily_stocks)*100:.1f}%)")
print(f"  仅日线无min5: {len(only_daily):,}")

# 按日统计
full_days = sum(1 for d in trading_dates if day_count.get(d, 0) >= len(both) * 0.8)
partial_days = sum(1 for d in trading_dates if 0 < day_count.get(d, 0) < len(both) * 0.8)
empty_days = sum(1 for d in trading_dates if day_count.get(d, 0) == 0)
print(f"\n  交易日覆盖:")
print(f"    完整日(>=80%股票): {full_days} / {len(trading_dates)}")
print(f"    部分日(<80%股票):  {partial_days}")
print(f"    无数据日:          {empty_days}")

# 日均覆盖
avg_cov = np.mean([day_count.get(d, 0) for d in trading_dates])
med_cov = np.median([day_count.get(d, 0) for d in trading_dates])
min_cov = min(day_count.get(d, 0) for d in trading_dates)
max_cov = max(day_count.get(d, 0) for d in trading_dates)
print(f"\n  每日min5覆盖股票数:")
print(f"    均值: {avg_cov:,.0f}  |  中位: {med_cov:,.0f}  |  最小: {min_cov:,}  |  最大: {max_cov:,}")

# 股票覆盖天数分布
stock_days_vals = list(stock_days.values())
print(f"\n  每只股票min5覆盖天数:")
print(f"    均值: {np.mean(stock_days_vals):.0f}  |  中位: {np.median(stock_days_vals):.0f}")
print(f"    最小: {min(stock_days_vals)}  |  最大: {max(stock_days_vals)}")
pcts = [50, 80, 90, 95, 99]
print(f"    分位数: " + " | ".join(f"P{p}={int(np.percentile(stock_days_vals, p))}" for p in pcts))

# 按年统计
print(f"\n  [按年统计]")
for yr in range(2024, 2027):
    yr_dates = [d for d in trading_dates if d.year == yr]
    if not yr_dates:
        continue
    yr_cov = [day_count.get(d, 0) for d in yr_dates]
    print(f"  {yr}: {len(yr_dates)}天 | 日均覆盖 {np.mean(yr_cov):,.0f} 只 | "
          f"最少 {min(yr_cov):,} / 最多 {max(yr_cov):,}")

# 每月覆盖
print(f"\n  [每月覆盖]")
monthly = defaultdict(list)
for d, cnt in day_count.items():
    monthly[d.strftime('%Y-%m')].append(cnt)
for m in sorted(monthly.keys()):
    vals = monthly[m]
    print(f"  {m}: 日均 {np.mean(vals):,.0f} 只 | 最少 {min(vals):,} | 最多 {max(vals):,} | 天数 {len(vals)}")

# 覆盖最差的10天
print(f"\n  [覆盖最差的10天]")
worst = sorted(day_count.items(), key=lambda x: x[1])[:10]
for d, cnt in worst:
    dow = ['一','二','三','四','五','六','日'][d.weekday()]
    print(f"  {d} (周{dow}): {cnt} 只有数据")

# 没有min5数据的日线股票（前20）
if only_daily:
    print(f"\n  [无min5数据的日线股票（前20）]: {', '.join(sorted(list(only_daily))[:20])}")
