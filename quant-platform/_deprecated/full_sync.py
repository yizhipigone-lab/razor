import os
import sys
# 增加项目根目录到路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.data_manager.engine import get_all_stock_list, download_daily_bars, download_min5_bars
from database.duckdb_manager import db
import baostock as bs
import time

def run_sync(freq="all", years=1):
    print(f"[FullSync] 启动全量同步任务...", flush=True)
    
    stocks = db.get_all_stocks()
    if stocks.empty:
        from app.data_manager.engine import get_all_stock_list
        stocks = get_all_stock_list()
        if stocks.empty: return
    
    codes = stocks["code"].tolist()
    codes.sort(key=lambda x: 0 if (x.startswith('6') or x.startswith('0')) else 1)
    total = len(codes)
    
    bs.login()
    try:
        # 第一阶段：日线
        print(f"PROGRESS:0:100:开始同步日线数据...", flush=True)
        success, failed = 0, 0
        for i, code in enumerate(codes):
            try:
                df = download_daily_bars(code, years)
                if df is not None and not df.empty:
                    db.save_bars(code, df, freq="daily")
                    success += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            if (i + 1) % 50 == 0 or (i + 1) == total:
                print(f"PROGRESS:{int((i+1)/total*50)}:100:日线处理中 {i+1}/{total} (成功:{success},失败:{failed})", flush=True)

        # 第二阶段：5分钟线
        print(f"PROGRESS:50:100:日线完成，开始同步5分钟线...", flush=True)
        success, failed = 0, 0
        for i, code in enumerate(codes):
            try:
                # 5分钟线通常只取最近 20 天，多年数据量太大
                df = download_min5_bars(code)
                if df is not None and not df.empty:
                    db.save_bars(code, df, freq="min5")
                    success += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            if (i + 1) % 50 == 0 or (i + 1) == total:
                print(f"PROGRESS:{int(50 + (i+1)/total*50)}:100:5分线处理中 {i+1}/{total} (成功:{success},失败:{failed})", flush=True)

    finally:
        bs.logout()
        print(f"COMPLETED:同步全部完成！", flush=True)

if __name__ == '__main__':
    # 参数处理
    f = sys.argv[1] if len(sys.argv) > 1 else "daily"
    y = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    run_sync(f, y)
