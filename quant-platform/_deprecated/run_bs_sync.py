import baostock as bs
import pandas as pd
from database.duckdb_manager import db
from datetime import datetime, timedelta
import time

def bao_sync():
    print("🚀 [BaoStock Robust Sync] 老黄牛同步引擎启动...")
    
    lg = bs.login()
    if lg.error_code != '0':
        print(f"❌ BaoStock 登录失败: {lg.error_msg}")
        return
        
    print("✅ BaoStock 登录成功！正在准备清单...")
    
    # 1. 第一步：获取股票清单
    try:
        # 使用最近的一个确定的交易日获取列表
        rs = bs.query_all_stock(day="2024-12-31")
        data_list = []
        while rs.next():
            data_list.append(rs.get_row_data())
        
        df_raw = pd.DataFrame(data_list, columns=rs.fields)
        print(f"📡 原始清单总计: {len(df_raw)} 条")
        # 排除指数和北交所代码
        # BaoStock 代码格式: sh.600000, sz.000001
        df_filtered = df_raw[df_raw['code'].str.contains(r'sh\.6|sz\.0|sz\.3', regex=True)]
        print(f"🧹 过滤后主板股票计: {len(df_filtered)} 条")
        
        # 格式化存库，补齐数据库字段以兼容 upsert_stocks SQL
        upsert_df = pd.DataFrame({
            'code': df_filtered['code'].str.replace('sh.', '', regex=False).str.replace('sz.', '', regex=False),
            'name': df_filtered['code_name'],
            'exchange': df_filtered['code'].apply(lambda x: 'SSE' if x.startswith('sh.') else 'SZSE'),
            'sector': '',
            'concepts': '',
            'list_date': None,
            'status': 'active'
        })
        db.upsert_stocks(upsert_df)
        print(f"📊 股票库更新完成: 已同步 {len(upsert_df)} 只沪深主要标的。")
    except Exception as e:
        print(f"❌ 清单获取失败: {e}")
        return

    # 2. 第二步：分步下载历史日线数据
    codes = upsert_df['code'].tolist()
    total = len(codes)
    success = 0
    start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    
    print("\n📦 开始全串行数据落地测试 [预计 1.5s/只]...")
    for i, code in enumerate(codes):
        try:
            bs_code = f"sh.{code}" if code.startswith('6') else f"sz.{code}"
            rs = bs.query_history_k_data_plus(
                bs_code, "date,open,high,low,close,volume,amount",
                start_date=start_date, frequency="d", adjustflag="2"
            )
            
            k_data = []
            while rs.next():
                k_data.append(rs.get_row_data())
            
            if k_data:
                df_k = pd.DataFrame(k_data, columns=rs.fields)
                # 转换类型
                df_k['date'] = pd.to_datetime(df_k['date'])
                for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                    df_k[col] = pd.to_numeric(df_k[col], errors='coerce')
                
                # 保存数据
                db.save_bars(code, df_k, freq="daily")
                success += 1
            
            if (i+1) % 20 == 0:
                 print(f"✅ 稳步推进: {i+1}/{total} | 成功: {success}")
                 
        except Exception as e:
            print(f"⚠️ {code} 异常: {e}")
            continue

    bs.logout()
    print(f"\n✨ 同步跑通！共计入库 {success} 只。")

if __name__ == '__main__':
    bao_sync()
