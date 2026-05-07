import requests
import pandas as pd
from database.duckdb_manager import db
from core.logger import get_logger
import time

log = get_logger("PopulateStocks")

def populate():
    log.info("正在多页拉取全量 A 股股票列表 (源: 新浪 API)...")
    all_dfs = []
    page = 1
    num_per_page = 100
    
    while page <= 60: # 约 6000 只股票
        url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num={num_per_page}&node=hs_a"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                log.error(f"Page {page} 请求失败")
                break
                
            data = resp.json()
            if not data or len(data) == 0:
                log.info(f"Page {page} 返回为空，拉取结束。")
                break
                
            df = pd.DataFrame(data)
            all_dfs.append(df)
            log.info(f"Page {page}: 获取到 {len(df)} 条数据")
            page += 1
            time.sleep(0.1) # 礼貌访问
        except Exception as e:
            log.error(f"Page {page} 发生错误: {e}")
            break
            
    if not all_dfs:
        log.error("未获取到任何数据")
        return
        
    full_df = pd.concat(all_dfs)
    
    # 清洗代码
    def _clean_code(symbol):
        return symbol.replace("sh", "").replace("sz", "").replace("bj", "")
    full_df["code"] = full_df["symbol"].apply(_clean_code)
    
    # 判断交易所
    def _infer_exchange(code: str) -> str:
        if code.startswith("6") or code.startswith("5"): return "SSE"
        if code.startswith("0") or code.startswith("3"): return "SZSE"
        if code.startswith("8") or code.startswith("4"): return "BSE"
        return "UNKNOWN"
    full_df["exchange"] = full_df["code"].apply(_infer_exchange)
    
    # 补齐字段
    full_df["sector"] = ""
    full_df["concepts"] = ""
    full_df["list_date"] = None
    full_df["status"] = "active"
    
    # 匹配 DB Schema
    res = full_df[["code", "name", "exchange", "sector", "concepts", "list_date", "status"]]
    
    log.info(f"合并完成，共 {len(res)} 只股票，正在写入数据库...")
    db.upsert_stocks(res)
    log.info("数据库写入完成。")

if __name__ == "__main__":
    populate()
