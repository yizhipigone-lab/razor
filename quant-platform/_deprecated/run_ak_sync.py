import akshare as ak
import pandas as pd
from database.duckdb_manager import db
import time
from datetime import datetime

def master_sync():
    print("🚀 [Master Sync] 全量 AKSHARE 驱动同步引擎启动...")
    
    # 1. 第一步：获取最新股票池
    try:
        print("🔍 阶段 1: 正在拉取全市场实时清单 [Akshare]...")
        df_raw = ak.stock_zh_a_spot_em()
        print(f"📡 原始接口返回数据量: {len(df_raw)} 条")
        
        # 排除北交所 (8和4开头的股票)
        df_filtered = df_raw[~df_raw['代码'].str.startswith(('8', '4'))]
        print(f"🧹 已通过主板过滤: 剩余 {len(df_filtered)} 只目标股")
        
        # 映射数据库字段
        upsert_df = pd.DataFrame({
            'code': df_filtered['代码'],
            'name': df_filtered['名称'],
            'exchange': df_filtered['代码'].apply(lambda x: 'SSE' if x.startswith('6') else 'SZSE'),
            'status': 'active'
        })
        
        # 写入数据库并确认
        print("💾 正在向数据库增量更新清单...")
        db.upsert_stocks(upsert_df)
        print("✅ 阶段 1 完成！股票清单已同步。")
    except Exception as e:
        print(f"❌ 阶段 1 崩溃: {e}")
        return

    # 2. 第二步：同步历史历史行情 (前 200 只压力测试)
    print("\n📦 阶段 2: 正在执行历史日线数据落地...")
    codes = upsert_df['code'].tolist()
    success, fail = 0, 0
    total = len(codes)
    
    # 我们先全力跑前 200 只，步长设为 10，随时汇报
    for i, code in enumerate(codes):
        try:
            # 下载最近一年日线
            df_hist = ak.stock_zh_a_hist(symbol=code, period="daily", start_date="20240101", adjust="qfq")
            if not df_hist.empty:
                # 重命名
                df_hist = df_hist.rename(columns={
                    '日期': 'date', '开盘': 'open', '最高': 'high', '最低': 'low',
                    '收盘': 'close', '成交量': 'volume', '成交额': 'amount'
                })
                # 转换时间
                df_hist['date'] = pd.to_datetime(df_hist['date'])
                
                # 写入数据库 (Parquet 模式)
                db.save_bars(code, df_hist, freq="daily")
                success += 1
            else:
                fail += 1
                
            if (i+1) % 10 == 0:
                print(f"✅ 同步已完成: {i+1}/{total} (成功:{success}, 失败:{fail})")
            
            # T 级别的轻微停顿，防止触发反爬虫
            time.sleep(0.05)
            
        except Exception as e:
            print(f"⚠️ [{code}] 失败: {e}")
            fail += 1
            # 如果遭遇限流，多等几秒
            if "Remote end closed" in str(e) or "Too Many Requests" in str(e):
                time.sleep(5)
            continue

    print(f"\n✨ 同步跑通测试完成！")
    print(f"本次运行处理了 {total} 只股票中的行情同步，其中成功 {success} 只。")

if __name__ == '__main__':
    master_sync()
