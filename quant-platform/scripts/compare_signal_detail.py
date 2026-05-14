"""
深入对比原版和改进版信号层面的差异
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
import numpy as np
from datetime import date, timedelta
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
START = date(2022, 1, 1)
END = date.today()

print("加载全市场日线数据...")
from app.backtest.simple_runner import load_daily_bars
bars = load_daily_bars(START - timedelta(days=180), END)
print(f"  加载完成: {len(bars)} 条, {bars['code'].nunique()} 只股票")

from app.screener.strategies.ma5_angle import generate_signals

print("\n生成原版信号...")
sig_o = generate_signals(bars, version="original")
sig_o = sig_o[(sig_o['date'] >= START) & (sig_o['date'] <= END)]
print(f"  原版信号: {len(sig_o)} 条")

print("生成改进版信号...")
sig_i = generate_signals(bars, version="improved")
sig_i = sig_i[(sig_i['date'] >= START) & (sig_i['date'] <= END)]
print(f"  改进版信号: {len(sig_i)} 条")

# 重合度分析
o_keys = set(zip(sig_o['code'], sig_o['date']))
i_keys = set(zip(sig_i['code'], sig_i['date']))
both = o_keys & i_keys
only_o = o_keys - i_keys
only_i = i_keys - o_keys

print(f"\n{'='*60}")
print(f"信号重合度分析")
print(f"{'='*60}")
print(f"  原版独有: {len(only_o)} ({len(only_o)/len(o_keys)*100:.1f}%)")
print(f"  改进版独有: {len(only_i)} ({len(only_i)/len(i_keys)*100:.1f}%)")
print(f"  两者共有: {len(both)} ({len(both)/len(o_keys)*100:.1f}% of 原版, {len(both)/len(i_keys)*100:.1f}% of 改进版)")

# 逐年统计
print(f"\n{'='*60}")
print(f"逐年信号数对比")
print(f"{'='*60}")
sig_o['year'] = pd.to_datetime(sig_o['date']).dt.year
sig_i['year'] = pd.to_datetime(sig_i['date']).dt.year
for yr in sorted(sig_o['year'].unique()):
    no = len(sig_o[sig_o['year'] == yr])
    ni = len(sig_i[sig_i['year'] == yr])
    print(f"  {yr}: 原版 {no:>5}  |  改进版 {ni:>5}  |  差异 {no-ni:+d}")

# 被原版选中但被改进版过滤的原因分析（采样分析）
print(f"\n{'='*60}")
print(f"改进版过滤原因分析（原版信号被改进版过滤的原因）")
print(f"{'='*60}")

# 对原版独有的信号，检查它们是卡在哪个条件上
only_o_df = sig_o[~sig_o.apply(lambda r: (r['code'], r['date']) in both, axis=1)].copy()

# 随机采样分析
sample = only_o_df.sample(min(1000, len(only_o_df)), random_state=42)

# 对采样信号，检查改进版的各个条件
# 重新对整批数据计算改进版中间指标
bars_sig = bars.copy()
g = bars_sig.groupby('code', group_keys=False)
bars_sig['ma5'] = g['close'].transform(lambda x: x.rolling(5).mean())
bars_sig['ma10'] = g['close'].transform(lambda x: x.rolling(10).mean())
bars_sig['ma20'] = g['close'].transform(lambda x: x.rolling(20).mean())
bars_sig['ma60'] = g['close'].transform(lambda x: x.rolling(60).mean())

# 改进版 x1/x2
bars_sig['x1_i'] = g['ma5'].transform(
    lambda x: (x - x.shift(5)) / x.shift(5) * 100
)
bars_sig['x2_i'] = g['x1_i'].transform(lambda x: x.rolling(5).mean())
bars_sig['cross_up_i'] = (bars_sig['x1_i'] > bars_sig['x2_i']) & (bars_sig['x1_i'].shift(1) <= bars_sig['x2_i'].shift(1))
bars_sig['cond_angle_i'] = bars_sig['cross_up_i'] & (bars_sig['x1_i'] > bars_sig['x1_i'].shift(1))

# 成交量条件
bars_sig['avg_vol_20'] = g['volume'].transform(lambda x: x.shift(1).rolling(20).mean())
bars_sig['cond_vol_i'] = bars_sig['volume'] > bars_sig['avg_vol_20'] * 1.5

# 收盘位置
bars_sig['close_pos'] = (bars_sig['close'] - bars_sig['low']) / (bars_sig['high'] - bars_sig['low'])
bars_sig['cond_close_strong_i'] = bars_sig['close_pos'] > 0.8

# 价格条件
bars_sig['range_high_20'] = g['high'].transform(lambda x: x.shift(1).rolling(20).max())
bars_sig['range_low_20'] = g['low'].transform(lambda x: x.shift(1).rolling(20).min())
bars_sig['range_mid_20'] = (bars_sig['range_high_20'] + bars_sig['range_low_20']) / 2
bars_sig['cond_price_i'] = (
    (bars_sig['close'] > bars_sig['ma20'])
    & (bars_sig['close'] > bars_sig['range_mid_20'])
    & (bars_sig['ma60'] >= bars_sig['ma60'].shift(10))
)

bars_sig['date_d'] = pd.to_datetime(bars_sig['date']).dt.date

# 原版条件
bars_sig['x1_o'] = g['ma5'].transform(
    lambda x: np.degrees(np.arctan((x / x.shift(1) - 1) * 100))
)
bars_sig['x2_o'] = g['x1_o'].transform(lambda x: x.rolling(5).mean())
bars_sig['cross_up_o'] = (bars_sig['x1_o'] > bars_sig['x2_o']) & (bars_sig['x1_o'].shift(1) <= bars_sig['x2_o'].shift(1))
bars_sig['cond_angle_o'] = (
    bars_sig['cross_up_o']
    & (bars_sig['x2_o'] < bars_sig['x2_o'].shift(5))
    & (bars_sig['x1_o'] > bars_sig['x1_o'].shift(5))
)
bars_sig['cond_price_o'] = (
    (bars_sig['close'] < 26)
    & (bars_sig['close'] / bars_sig['close'].shift(1) > 1.02)
    & (bars_sig['close'] > bars_sig['ma20'])
)

# 检查采样中被过滤的原因
reasons = Counter()
for _, r in sample.iterrows():
    mask = (bars_sig['code'] == r['code']) & (bars_sig['date_d'] == r['date'])
    row = bars_sig[mask]
    if row.empty:
        reasons['no_data'] += 1
        continue
    row = row.iloc[0]

    if not row['cond_angle_i']:
        reasons['angle_not_pass'] += 1
    elif not row['cond_price_i']:
        reasons['price_not_pass'] += 1
    elif not row['cond_vol_i']:
        reasons['volume_not_pass'] += 1
    elif not row['cond_close_strong_i']:
        reasons['close_pos_not_pass'] += 1
    else:
        # 可能被20天新鲜度或连续信号过滤
        reasons['other_filter'] += 1

print(f"\n  采样 {len(sample)} 个原版独有信号:")
total = len(sample)
for reason, count in reasons.most_common():
    print(f"    {reason}: {count} ({count/total*100:.1f}%)")

# 分析原版信号中哪些是因为 close<26 条件而选到的
print(f"\n{'='*60}")
print(f"价格分布分析")
print(f"{'='*60}")
# 看原版信号的价格分布 vs 改进版
print(f"\n  原版信号收盘价分布:")
for pct in [10, 25, 50, 75, 90]:
    val = sig_o['close'].quantile(pct/100)
    print(f"    P{pct}: {val:.2f}")
print(f"    均值: {sig_o['close'].mean():.2f}")
print(f"    最小值: {sig_o['close'].min():.2f}")
print(f"    最大值: {sig_o['close'].max():.2f}")
print(f"    <26 的比例: {(sig_o['close'] < 26).mean()*100:.1f}%")

print(f"\n  改进版信号收盘价分布:")
for pct in [10, 25, 50, 75, 90]:
    val = sig_i['close'].quantile(pct/100)
    print(f"    P{pct}: {val:.2f}")
print(f"    均值: {sig_i['close'].mean():.2f}")
print(f"    最小值: {sig_i['close'].min():.2f}")
print(f"    最大值: {sig_i['close'].max():.2f}")
print(f"    <26 的比例: {(sig_i['close'] < 26).mean()*100:.1f}%")

# 改进版独有的信号有什么特征
print(f"\n{'='*60}")
print(f"改进版独有信号特征（原版选不到但改进版能选到的）")
print(f"{'='*60}")
only_i_df = sig_i[~sig_i.apply(lambda r: (r['code'], r['date']) in both, axis=1)]
print(f"  共 {len(only_i_df)} 个信号")
if len(only_i_df) > 0:
    print(f"  收盘价分布: 均值 {only_i_df['close'].mean():.2f}, 中位数 {only_i_df['close'].median():.2f}")
    print(f"  close >= 26 的比例: {(only_i_df['close'] >= 26).mean()*100:.1f}%")
    # 这些是高价股信号，原版因为 close<26 条件而漏掉的
    print(f"  > 原版 close<26 条件直接过滤的: {len(only_i_df[only_i_df['close'] >= 26])} ({len(only_i_df[only_i_df['close'] >= 26])/len(only_i_df)*100:.1f}%)")

# 成交额门槛分析
print(f"\n{'='*60}")
print(f"成交量对比（改进版信号必须放量 >1.5x 均量）")
print(f"{'='*60}")
print(f"  原版信号平均量比: {sig_o['volume'].mean() / sig_o['volume'].mean():.2f}")
print(f"  改进版信号平均量比: {sig_i['volume'].mean() / sig_i['volume'].mean():.2f}")
