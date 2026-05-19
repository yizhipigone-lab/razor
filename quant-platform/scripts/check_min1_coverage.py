#!/usr/bin/env python
"""检查 min1 数据 2026-01-29 至今的完整度"""
import pandas as pd
from datetime import date, datetime
from pathlib import Path
from collections import defaultdict

MIN1_DIR = Path(r"e:\1target\p9_project\quant-platform\data\parquet\min1")
START = date(2026, 1, 29)
END   = date.today()

# 统计每只股票每天有没有数据
files = list(MIN1_DIR.glob("*.parquet"))
print(f"min1 文件总数: {len(files):,}")

# 扫描所有文件，提取 code -> {date set}
code_dates = defaultdict(set)
total_checked = 0
missing_dt = 0

for f in files:
    try:
        df = pd.read_parquet(str(f), columns=['datetime'] if 'datetime' in pd.read_parquet(str(f)).columns else None)
    except:
        missing_dt += 1
        continue

    # 只读 schema 判断有没有 datetime 列
    total_checked += 1

print(f"可读文件: {total_checked:,}")

# 换个方式：抽样检查几只股票，看日期覆盖
import random
sample_files = random.sample(files, min(200, len(files)))

date_coverage = defaultdict(int)  # date -> stock count
total_dates = set()

for f in sample_files:
    try:
        df = pd.read_parquet(str(f))
        if 'datetime' in df.columns:
            df['datetime'] = pd.to_datetime(df['datetime'])
            dates = set(df['datetime'].dt.date)
            for d in dates:
                if START <= d <= END:
                    date_coverage[d] += 1
            total_dates.update(dates)
        elif 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date']).dt.date
            for d in df['date'].unique():
                if START <= d <= END:
                    date_coverage[d] += 1
    except:
        pass

# 交易日列表（简单估算：周一到周五）
import pandas as pd
all_days = pd.date_range(START, END, freq='B')  # business days
trading_days = [d.date() for d in all_days]

print(f"\n2026-01-29 ~ {END} 交易日数: {len(trading_days)}")
print(f"抽样 {len(sample_files)} 只股票\n")

print(f"{'日期':<12} {'抽样覆盖(只)':>14} {'覆盖率%':>10}")
print("-" * 38)

zero_days = 0
for d in trading_days:
    cnt = date_coverage.get(d, 0)
    pct = cnt / len(sample_files) * 100
    bar = "█" * int(pct / 5)
    if cnt == 0:
        zero_days += 1
    print(f"  {d}  {cnt:>10}只  {pct:>6.1f}%  {bar}")

print(f"\n零覆盖交易日: {zero_days}/{len(trading_days)}")
