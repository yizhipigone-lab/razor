"""指数日线数据更新器（akshare）"""
import pandas as pd
from pathlib import Path
from core.logger import get_logger

log = get_logger("IndexUpdater")
ROOT = Path(__file__).resolve().parent.parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"

INDICES = {
    'index_000001': 'sh000001',
    'index_399001': 'sz399001',
    'index_000300': 'sh000300',
    'index_000905': 'sh000905',
    'index_000852': 'sh000852',
    'index_000510': 'sh000510',
    'index_399006': 'sz399006',
    'index_399005': 'sz399005',
    'index_000688': 'sh000688',
}


def update_all_indices() -> dict:
    """更新所有指数 parquet 文件，返回 {fname: added_count}"""
    import akshare as ak
    result = {}
    for fname, symbol in INDICES.items():
        fp = DAILY_DIR / f"{fname}.parquet"
        if not fp.exists():
            log.warning(f"IndexUpdater | {fname} 文件不存在")
            continue
        try:
            existing = pd.read_parquet(str(fp))
            date_col = 'trade_date' if 'trade_date' in existing.columns else 'date'
            existing[date_col] = pd.to_datetime(existing[date_col]).dt.date
            last_date = existing[date_col].max()

            new_df = ak.stock_zh_index_daily(symbol=symbol)
            new_df['date'] = pd.to_datetime(new_df['date']).dt.date
            new_data = new_df[new_df['date'] > last_date]

            if len(new_data) == 0:
                continue

            new_data = new_data.rename(columns={
                'open': 'open', 'high': 'high', 'low': 'low',
                'close': 'close', 'volume': 'volume'
            })
            combined = pd.concat([existing, new_data], ignore_index=True)
            combined[date_col] = pd.to_datetime(combined[date_col])
            combined = combined.sort_values(date_col)
            combined = combined.drop_duplicates(subset=[date_col])
            combined.to_parquet(str(fp), index=False)

            result[fname] = len(new_data)
            log.info(f"IndexUpdater | {fname}: +{len(new_data)} 条")
        except Exception as e:
            log.error(f"IndexUpdater | {fname} 失败: {e}")

    return result
