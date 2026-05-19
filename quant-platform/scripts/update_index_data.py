"""用 akshare 更新指数日线数据到 parquet"""
import akshare as ak
import pandas as pd
from pathlib import Path
from datetime import date

DAILY_DIR = Path(__file__).resolve().parent.parent / "data" / "parquet" / "daily"

# 指数列表: {parquet文件名: akshare symbol}
INDICES = {
    'index_000001': 'sh000001',   # 上证指数
    'index_000300': 'sh000300',   # 沪深300
    'index_000905': 'sh000905',   # 中证500
    'index_000852': 'sh000852',   # 中证1000
    'index_000510': 'sh000510',   # 中证A500
    'index_399006': 'sz399006',   # 创业板指
}

for fname, symbol in INDICES.items():
    fp = DAILY_DIR / f"{fname}.parquet"
    if not fp.exists():
        print(f"{fname}: file not found, skip")
        continue

    # 读已有数据
    existing = pd.read_parquet(str(fp))
    date_col = 'trade_date' if 'trade_date' in existing.columns else 'date'
    existing[date_col] = pd.to_datetime(existing[date_col]).dt.date
    last_date = existing[date_col].max()

    # 拉新数据
    try:
        new_df = ak.stock_zh_index_daily(symbol=symbol)
        new_df['date'] = pd.to_datetime(new_df['date']).dt.date
        new_data = new_df[new_df['date'] > last_date]

        if len(new_data) == 0:
            print(f"{fname}: already up to date ({last_date})")
            continue

        # 合并并保存
        combined = pd.concat([existing, new_data.rename(columns={
            'open': 'open', 'high': 'high', 'low': 'low',
            'close': 'close', 'volume': 'volume'
        })], ignore_index=True)

        # Fill any new columns from existing
        for c in existing.columns:
            if c not in combined.columns:
                combined[c] = None

        combined[date_col] = pd.to_datetime(combined[date_col])
        combined = combined.sort_values(date_col).drop_duplicates(subset=[date_col])
        combined.to_parquet(str(fp), index=False)
        print(f"{fname}: added {len(new_data)} rows ({new_data['date'].min()} ~ {new_data['date'].max()})")

    except Exception as e:
        print(f"{fname}: error - {e}")

print("Done")
