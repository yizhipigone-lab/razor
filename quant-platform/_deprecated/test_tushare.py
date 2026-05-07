import tushare as ts
import pandas as pd
from database.duckdb_manager import db
from datetime import datetime

# ======= Tushare Token 已配置 =======
TUSHARE_TOKEN = '5051cf6cf52ca062ca348ab11c615ecb6b7909085d33cc11bc6f7ece'
# ============================================

def sync_via_tushare():
    if TUSHARE_TOKEN == '您的_TUSHARE_TOKEN':
        print("❌ 错误: 请先在脚本中填入您的 Tushare Token")
        return

    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()
    
    print("🚀 启动 Tushare 数据同步...")
    
    # 1. 获取股票列表
    try:
        df_list = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,area,industry,list_date')
        print(f"成功拉取清单: {len(df_list)} 只股票")
    except Exception as e:
        print(f"❌ Tushare 访问失败: {e}")
        return

    # 2. 遍历下载（Tushare 建议根据积分控制并发频率）
    success = 0
    for i, row in df_list.iterrows():
        ts_code = row['ts_code']
        symbol = row['symbol']
        
        try:
            # 下载最近一年的日线
            df = ts.pro_bar(ts_code=ts_code, adj='qfq', start_date='20240101')
            if df is not None and not df.empty:
                # 转换为项目标准列名
                df = df.rename(columns={
                    'trade_date': 'date',
                    'vol': 'volume'
                })
                df['date'] = pd.to_datetime(df['date'])
                
                # 写入数据库（现在已支持线程安全）
                db.save_bars(symbol, df, freq="daily")
                success += 1
            
            if (i+1) % 50 == 0:
                print(f"进度: {i+1}/{len(df_list)} | 成功累计: {success}")
                
        except Exception as e:
            print(f"跳过 {symbol}: {e}")
            continue

    print(f"✅ Tushare 同步任务结束。成功导入: {success}")

if __name__ == '__main__':
    sync_via_tushare()
