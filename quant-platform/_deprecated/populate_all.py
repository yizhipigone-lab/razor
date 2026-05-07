import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time
import requests
import json
import re
from io import StringIO
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from database.duckdb_manager import db
from app.data_manager.engine import get_all_stock_list, download_daily_bars, download_min5_bars, log, HEADERS

def update_sectors():
    log.info("开始拉取行业板块数据(Sina源)...")
    url = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        # Parse the JSON-like JS object
        # var S_Finance_bankuai_sinaindu = { "new_blhy":"玻璃行业,31,...", ...}
        match = re.search(r'S_Finance_bankuai_sinaindu\s*=\s*(\{.*?\})\s*;', r.text, re.DOTALL)
        if not match:
            log.error("无法解析新浪板块数据结构")
            return
            
        # The JS object needs keys to be quoted to be valid JSON
        json_str = match.group(1)
        json_str = re.sub(r'([{,]\s*)([a-zA-Z0-9_]+)\s*:', r'\1"\2":', json_str)
        data = json.loads(json_str)
        
        updated = 0
        nodes = list(data.keys())
        for node in tqdm(nodes, desc="同步各行业成分股"):
            v = data[node]
            parts = v.split(',')
            if len(parts) >= 2:
                sector_name = parts[0]
                
                page = 1
                codes = []
                while True:
                    api = f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=1&node={node}&symbol=&_s_r_a=init"
                    try:
                        res = requests.get(api, headers=HEADERS, timeout=10)
                        if not res.text or 'null' in res.text:
                            break
                        items = json.loads(res.text)
                        if not items: break
                        for item in items:
                            code = item['symbol']
                            code = code.replace("sh","").replace("sz","").replace("bj","")
                            codes.append(code)
                        if len(items) < 80:
                            break
                        page += 1
                        time.sleep(0.5)
                    except Exception:
                        time.sleep(2)
                        break
                
                if codes:
                    placeholders = ",".join(["?"] * len(codes))
                    db.conn.execute(
                        f"UPDATE stocks SET sector = ? WHERE code IN ({placeholders})",
                        [sector_name] + codes
                    )
                    db.conn.commit()
                    updated += len(codes)
                    
        log.info(f"成功同步 {updated} 只股票的行业板块信息。")
    except Exception as e:
        log.error(f"板块同步异常: {e}")

def batch_download_fast(freq="daily"):
    stocks = db.get_all_stocks()
    if stocks.empty:
        log.warning("无股票列表！")
        return
        
    codes = stocks["code"].tolist()
    log.info(f"开始使用多线程加速下载 {len(codes)} 只股票的 {freq} 数据...")
    
    success = 0
    failed = 0
    
    def fetch_and_save(code):
        try:
            if freq == "daily":
                df = download_daily_bars(code, 1)
            else:
                df = download_min5_bars(code)
                
            if df is not None and not df.empty:
                db.save_bars(code, df, freq=freq)
                time.sleep(0.2)
                return True
        except Exception:
            pass
        return False
        
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fetch_and_save, code): code for code in codes}
        for future in tqdm(as_completed(futures), total=len(codes), desc=f"下载{freq}"):
            if future.result():
                success += 1
            else:
                failed += 1
                
    log.info(f"【{freq} 批量下载完成】成功: {success}, 失败: {failed}")

if __name__ == "__main__":
    log.info("=== 全量数据初始化开始 ===")
    
    # 1. 股票列表
    stocks = get_all_stock_list()
    if not stocks.empty:
        db.upsert_stocks(stocks)
        
    # 2. 板块
    update_sectors()
    
    # 3. 日线
    batch_download_fast("daily")
    
    # 4. 5分钟
    batch_download_fast("5m")
    
    log.info("=== 所有的股票行业/K线数据全部初始化完成！ ===")
