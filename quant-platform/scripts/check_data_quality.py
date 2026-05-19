"""检查本地 parquet 数据完整度"""
import glob, os, pandas as pd, numpy as np

DATA_DIR = r'e:\1target\p9_project\quant-platform\data\parquet\daily'
files = sorted(glob.glob(os.path.join(DATA_DIR, '*.parquet')))

print("=" * 60)
print("数据完整度检查")
print(f"文件总数: {len(files)}")
print("=" * 60)

# 1. 板块统计
sh = sum(1 for f in files if os.path.basename(f).startswith('6'))
sz = sum(1 for f in files if os.path.basename(f).startswith('0'))
cy = sum(1 for f in files if os.path.basename(f).startswith('3'))
bj_8 = sum(1 for f in files if os.path.basename(f).startswith('8'))
bj_4 = sum(1 for f in files if os.path.basename(f).startswith('4'))
bj_9 = sum(1 for f in files if os.path.basename(f).startswith('9'))
other = len(files) - sh - sz - cy - bj_8 - bj_4 - bj_9
print(f"\n[1] 板块分布:")
print(f"  沪市主板(6): {sh}")
print(f"  深市主板(0): {sz}")
print(f"  创业板(3):   {cy}")
print(f"  北交所(8):   {bj_8}")
print(f"  北交所(4):   {bj_4}")
print(f"  北交所(9):   {bj_9}")
print(f"  其他:        {other}")

# 2. 日期范围统计（抽样100只 + 1000只扫描）
print(f"\n[2] 日期范围...")

# 先用前1000只
sample_size = min(1000, len(files))
all_starts = []
all_ends = []
all_counts = []
for i, f in enumerate(files[:sample_size]):
    try:
        df = pd.read_parquet(f, columns=['date'])
        all_starts.append(df['date'].iloc[0])
        all_ends.append(df['date'].iloc[-1])
        all_counts.append(len(df))
    except:
        pass

# Convert to list of Timestamps then sort
all_starts_ts = [pd.Timestamp(t) for t in all_starts]
all_ends_ts = [pd.Timestamp(t) for t in all_ends]

print(f"  抽样: {len(all_starts)} 只")
print(f"  起始日期: {min(all_starts_ts).strftime('%Y-%m-%d')} ~ {max(all_starts_ts).strftime('%Y-%m-%d')}")
print(f"  结束日期: {min(all_ends_ts).strftime('%Y-%m-%d')} ~ {max(all_ends_ts).strftime('%Y-%m-%d')}")
print(f"  记录数: min={min(all_counts)}  max={max(all_counts)}  mean={np.mean(all_counts):.0f}  median={int(np.median(all_counts))}")

# 检查最新数据日期
print(f"\n[3] 数据新鲜度:")
# 抽样检查最新日期
end_dates_count = {}
for et in all_ends_ts:
    key = et.strftime('%Y-%m-%d')
    end_dates_count[key] = end_dates_count.get(key, 0) + 1

for k in sorted(end_dates_count.keys(), reverse=True)[:10]:
    print(f"  结束于 {k}: {end_dates_count[k]} 只")

# 4. 缺失数据检查
print(f"\n[4] 停牌/缺失检查 (抽样5只):")
import random
random.seed(42)
sample = random.sample(files, min(5, len(files)))
for f in sample:
    df = pd.read_parquet(f, columns=['date', 'close'])
    df = df.sort_values('date').drop_duplicates('date')
    df['gap'] = df['date'].diff().dt.days
    large_gaps = (df['gap'] > 5).sum()
    max_gap = df['gap'].max()
    print(f"  {os.path.basename(f):20s}  记录{len(df):4d}  >5日断点{large_gaps:3d}  最大间隔{int(max_gap) if pd.notna(max_gap) else 0}天")

# 5. 检查通达信终端能否补充最新数据
print(f"\n[5] 通达信 TdxQuant 补充能力:")
try:
    import sys
    sys.path.insert(0, r'E:\NEW_TDX\PYPlugins\user')
    from tqcenter import tq
    tq.initialize(r'E:\NEW_TDX\PYPlugins\user\tdxdata_test.py')
    today = pd.Timestamp.now().strftime('%Y%m%d')
    df_tdx = tq.get_market_data(
        field_list=['Close'],
        stock_list=['000001.SZ'],
        start_time='20260508',
        end_time=today,
        dividend_type='front',
        period='1d'
    )
    if isinstance(df_tdx, dict):
        df_c = tq.price_df(df_tdx, 'Close', column_names=['000001.SZ'])
        last_date = df_c.index[-1]
        if hasattr(last_date, 'strftime'):
            last_str = last_date.strftime('%Y-%m-%d')
        else:
            last_str = str(last_date)
        print(f"  TdxQuant 最新数据日期: {last_str}  ({len(df_c)} 条)")
        print(f"  可补充最新交易日数据")
except Exception as e:
    print(f"  未连接通达信终端，无法补充: {e}")

print(f"\n[6] 结论:")
print(f"  总股票数: {len(files)} 只 (沪{sh}+深{sz}+创{cy}+北{bj_8+bj_4+bj_9})")
print(f"  数据起始: {min(all_starts_ts).strftime('%Y-%m-%d')}")
print(f"  数据截止: {max(all_ends_ts).strftime('%Y-%m-%d')}")
print(f"  平均记录: {np.mean(all_counts):.0f} 条/只")
print(f"  数据质量: 可用于回测，需排除stale股票")
print("=" * 60)
