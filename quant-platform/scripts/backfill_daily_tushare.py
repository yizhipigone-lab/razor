"""
Tushare 补充日线至 2015-01-01
按股票逐个拉取全量历史，比逐日拉取快 10x
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv; load_dotenv()
import pandas as pd
import tushare as ts
from pathlib import Path
from datetime import datetime

ts.set_token(os.environ["TUSHARE_KEY"])
pro = ts.pro_api()
ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"

FILL_START = '20150101'
FILL_END   = '20210629'

print("=" * 60)
print(f"Tushare 补充历史数据 {FILL_START[:4]}-{FILL_END[:4]}")
print("=" * 60)

# 找出所有需要补数据的股票
files = sorted(DAILY_DIR.glob("*.parquet"))
codes = [f.stem for f in files if len(f.stem) == 6 and f.stem.isdigit()]
print(f"全市场 {len(codes)} 只")

def ts_code(c):
    return f"{c}.SH" if c.startswith('6') else f"{c}.SZ"

success, fail, skip, no_data = 0, 0, 0, 0
t0 = time.time()

for i, code in enumerate(codes):
    try:
        # 检查已有数据是否需要补充
        fp = DAILY_DIR / f'{code}.parquet'
        existing = pd.read_parquet(str(fp), columns=['date'])
        existing['date'] = pd.to_datetime(existing['date'])
        earliest = existing['date'].min()
        if earliest <= pd.Timestamp('2015-01-05'):
            skip += 1; continue

        # 拉取全量历史日线（前复权）
        df = pro.daily(ts_code=ts_code(code), start_date=FILL_START, end_date=FILL_END)
        if df is None or df.empty:
            no_data += 1; continue

        df = df.rename(columns={'trade_date': 'date', 'vol': 'volume'})
        df['date'] = pd.to_datetime(df['date'])
        for c in ['open','high','low','close','volume']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        # Tushare amount 字段单位是元(与 #7 同根因,L1 修复)
        df['amount'] = df['amount'].fillna(df['close'] * df['volume'] * 100)
        df = df[['date','open','high','low','close','volume','amount']].dropna(subset=['close'])

        if len(df) == 0:
            no_data += 1; continue

        # 合并去重
        df = df[~df['date'].isin(existing['date'])]
        if len(df) == 0:
            skip += 1; continue

        merged = pd.concat([df, existing], ignore_index=True)
        merged = merged.sort_values('date').drop_duplicates('date', keep='first')
        merged.to_parquet(str(fp), index=False)
        success += 1

    except Exception as e:
        fail += 1
        if fail <= 5: print(f"  [{code}] 失败: {e}")

    if (i + 1) % 200 == 0:
        elapsed = time.time() - t0
        rate = (i + 1) / elapsed
        eta = (len(codes) - i - 1) / rate
        print(f"  进度: {i+1}/{len(codes)} 成功{success} 跳过{skip} 无数据{no_data} 失败{fail} | {elapsed:.0f}s ETA:{eta:.0f}s")

    time.sleep(0.15)  # Tushare 免费版限速

elapsed = time.time() - t0
print(f"\n完成: 成功{success} 跳过{skip} 无数据{no_data} 失败{fail} | {elapsed:.0f}s")

# 验证
print("\n验证:")
for c in ['000001','000002','600519','300750','601318']:
    f = DAILY_DIR / f'{c}.parquet'
    if f.exists():
        df = pd.read_parquet(str(f), columns=['date'])
        df['date'] = pd.to_datetime(df['date'])
        print(f"  {c}: {df['date'].min().date()} ~ {df['date'].max().date()} ({len(df)}条)")
