import pandas as pd
import akshare as ak
from database.duckdb_manager import db
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

def sync_task(code):
    try:
        # 下载日线数据
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date="20240101", adjust="qfq")
        if not df.empty:
            df = df.rename(columns={
                '日期': 'date', '开盘': 'open', '最高': 'high', '最低': 'low', '收盘': 'close', '成交量': 'volume', '成交额': 'amount'
            })
            df['date'] = pd.to_datetime(df['date'])
            db.save_bars(code, df, freq="daily")
            return True
    except Exception as e:
        print(f"❌ {code} 失败: {e}")
    return False

def main():
    print("🚀 启动 [5 线程并发] 极速同步测试...")
    df_list = ak.stock_zh_a_spot_em()
    codes = df_list['代码'].tolist()[:100]
    
    start_time = time.time()
    success = 0
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(sync_task, c): c for c in codes}
        for future in as_completed(futures):
            if future.result():
                success += 1
            if success % 20 == 0 and success > 0:
                print(f"✅ 已完成: {success}/100 只...")

    elapsed = time.time() - start_time
    print(f"\n✨ 并发测试圆满结束！耗时: {elapsed:.2f}秒")
    print(f"平均速度: {elapsed/success:.2f}秒/只 (5 线程并行下)")

if __name__ == '__main__':
    main()
