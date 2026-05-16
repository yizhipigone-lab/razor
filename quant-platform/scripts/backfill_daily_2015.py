"""
补全历史日线数据 — 通过 baostock 获取 2015-01-01 ~ 2021-06-29
免费，无需 Tushare Key。前复权。
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
import baostock as bs
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"

FILL_START = '2015-01-01'
FILL_END   = '2021-06-29'

print("=" * 60)
print(f"补全历史数据: {FILL_START} ~ {FILL_END}")
print("=" * 60)

# 找出所有A股
print("\n[1/3] 扫描现有股票...")
files = sorted(DAILY_DIR.glob("*.parquet"))
codes = []
for f in files:
    c = f.stem
    if len(c) == 6 and c.isdigit():
        codes.append(c)

print(f"  全市场 {len(codes)} 只")

# 登录 baostock
print("\n[2/3] 开始下载...")
lg = bs.login()
print(f"  baostock: {lg.error_msg}")

def baostock_code(code):
    if code.startswith('6'): return f'sh.{code}'
    return f'sz.{code}'

success, fail, skip, total = 0, 0, 0, 0
t0 = time.time()

for i, code in enumerate(codes):
    total += 1
    try:
        bs_code = baostock_code(code)
        rs = bs.query_history_k_data_plus(bs_code,
            'date,open,high,low,close,volume,amount',
            start_date=FILL_START, end_date=FILL_END,
            frequency='d', adjustflag='2')

        rows = []
        while (rs.error_code == '0') and rs.next():
            rows.append(rs.get_row_data())

        if len(rows) == 0:
            skip += 1; continue

        df_new = pd.DataFrame(rows, columns=['date','open','high','low','close','volume','amount'])
        df_new['date'] = pd.to_datetime(df_new['date'])
        for c in ['open','high','low','close','volume','amount']:
            df_new[c] = pd.to_numeric(df_new[c], errors='coerce')
        df_new = df_new.dropna(subset=['close'])
        if len(df_new) == 0:
            skip += 1; continue

        fp = DAILY_DIR / f'{code}.parquet'
        if fp.exists():
            existing = pd.read_parquet(str(fp))
            if 'date' in existing.columns:
                existing['date'] = pd.to_datetime(existing['date'])
            df_new = df_new[~df_new['date'].isin(existing['date'])]
            if len(df_new) == 0:
                skip += 1; continue
            merged = pd.concat([df_new, existing], ignore_index=True)
            merged = merged.sort_values('date').drop_duplicates('date', keep='first')
        else:
            merged = df_new

        merged.to_parquet(str(fp), index=False)
        success += 1

    except Exception as e:
        fail += 1
        if fail <= 5: print(f"  [{code}] 失败: {e}")

    if (i + 1) % 500 == 0:
        elapsed = time.time() - t0
        eta = elapsed / (i + 1) * (len(codes) - i - 1)
        print(f"  进度: {i+1}/{len(codes)} 成功{success} 跳过{skip} 失败{fail} | {elapsed:.0f}s ETA:{eta:.0f}s")

bs.logout()
elapsed = time.time() - t0
print(f"\n  完成: 成功{success} 跳过{skip} 失败{fail} | 总耗时 {elapsed:.0f}s")

# 验证
print(f"\n[3/3] 验证:")
for c in ['000001', '000002', '600519', '300750', '601318']:
    f = DAILY_DIR / f'{c}.parquet'
    if f.exists():
        df = pd.read_parquet(str(f), columns=['date'])
        df['date'] = pd.to_datetime(df['date'])
        print(f"  {c}: {df['date'].min().date()} ~ {df['date'].max().date()} ({len(df)}条)")
