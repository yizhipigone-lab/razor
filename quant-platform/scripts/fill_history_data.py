"""
补全历史日线数据 — 通过 baostock 获取 2021-06-30 ~ 2022-01-03
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
import numpy as np
import baostock as bs
from pathlib import Path
from datetime import date, datetime, timedelta

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"

FILL_START = '2021-06-30'
FILL_END   = '2022-05-04'

print("=" * 60)
print(f"补全历史数据: {FILL_START} ~ {FILL_END}")
print("=" * 60)

# 1. 找出需要补数据的股票
print("\n[1/3] 扫描现有数据...")
files = sorted(DAILY_DIR.glob("*.parquet"))
targets = []
for f in files:
    code = f.stem
    if len(code) == 6 and code.isdigit() and code[0] in '603':
        df = pd.read_parquet(str(f), columns=['date'])
        df['date'] = pd.to_datetime(df['date'])
        earliest = df['date'].min()
        if earliest > pd.Timestamp(FILL_START):
            targets.append((code, earliest.date()))

print(f"  需补数据: {len(targets)} 只")

if len(targets) == 0:
    print("  无需补全")
    sys.exit(0)

# 2. 登录 baostock
print("\n[2/3] 下载历史数据...")
lg = bs.login()
print(f"  登录: {lg.error_msg}")

def baostock_code(code):
    """转为 baostock 代码格式"""
    if code.startswith('6'):
        return f'sh.{code}'
    return f'sz.{code}'

success, fail, skip = 0, 0, 0

for i, (code, earliest) in enumerate(targets):
    try:
        bs_code = baostock_code(code)
        rs = bs.query_history_k_data_plus(bs_code,
            'date,open,high,low,close,volume',
            start_date=FILL_START, end_date=FILL_END,
            frequency='d', adjustflag='2')  # 前复权

        rows = []
        while (rs.error_code == '0') and rs.next():
            rows.append(rs.get_row_data())

        if len(rows) == 0:
            skip += 1
            continue

        df_new = pd.DataFrame(rows, columns=['date','open','high','low','close','volume'])
        df_new['date'] = pd.to_datetime(df_new['date'])
        for c in ['open','high','low','close','volume']:
            df_new[c] = pd.to_numeric(df_new[c], errors='coerce')
        df_new = df_new.dropna(subset=['close'])
        if len(df_new) == 0:
            skip += 1
            continue

        # 读取并合并现有数据
        existing = pd.read_parquet(str(DAILY_DIR / f'{code}.parquet'))
        existing['date'] = pd.to_datetime(existing['date'])

        # 去重：neww 中跳过已存在的日期
        df_new = df_new[~df_new['date'].isin(existing['date'])]
        if len(df_new) == 0:
            skip += 1
            continue

        merged = pd.concat([df_new, existing], ignore_index=True)
        merged = merged.sort_values('date').drop_duplicates('date', keep='first')
        merged.to_parquet(str(DAILY_DIR / f'{code}.parquet'), index=False)
        success += 1

    except Exception as e:
        fail += 1
        if fail <= 3:
            print(f"  {code} 失败: {e}")

    if (i + 1) % 500 == 0:
        print(f"  进度: {i+1}/{len(targets)} (成功{success} 跳过{skip} 失败{fail})")

bs.logout()
print(f"  完成: 成功{success} 跳过{skip} 失败{fail}")

# 3. 验证
print(f"\n[3/3] 验证:")
for code in ['000001', '000002', '600519', '300750']:
    f = DAILY_DIR / f'{code}.parquet'
    if f.exists():
        df = pd.read_parquet(str(f), columns=['date'])
        df['date'] = pd.to_datetime(df['date'])
        print(f"  {code}: {df['date'].min().date()} ~ {df['date'].max().date()} ({len(df)}条)")

print("=" * 60)
