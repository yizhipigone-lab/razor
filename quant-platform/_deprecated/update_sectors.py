import akshare as ak
import pandas as pd
from database.duckdb_manager import db
from core.logger import get_logger
from tqdm import tqdm

log = get_logger("UpdateSectors")

def update_sectors():
    log.info("正在获取新浪行业板块清单...")
    try:
        sector_df = ak.stock_sector_spot()
        if sector_df is None or sector_df.empty:
            log.error("无法获取板块清单")
            return
            
        log.info(f"找到 {len(sector_df)} 个行业板块，开始拉取成分股...")
        
        updated_stocks = 0
        for _, row in tqdm(sector_df.iterrows(), total=len(sector_df), desc="拉取并更新"):
            label = row["label"]
            sector_name = row["板块"]
            
            try:
                stocks = ak.stock_sector_detail(sector=label)
                if stocks is not None and not stocks.empty:
                    # 使用正确的列名 'code' 或 'symbol'
                    codes = stocks["code"].tolist()
                    
                    if codes:
                        placeholders = ",".join(["?"] * len(codes))
                        db.conn.execute(
                            f"UPDATE stocks SET sector = ? WHERE code IN ({placeholders})",
                            [sector_name] + codes
                        )
                        db.conn.commit()
                        updated_stocks += len(codes)
            except Exception as e:
                log.warning(f"板块 {sector_name} 更新失败: {e}")
                continue
                
        log.info(f"同步完成。共更新了 {updated_stocks} 只股票的板块信息。")
        
    except Exception as e:
        log.error(f"同步异常: {e}")

if __name__ == "__main__":
    update_sectors()
