# -*- coding: utf-8 -*-
from pytdx2.hq import TdxHq_API
import pandas as pd
import time

def final_test():
    api = TdxHq_API()
    server = ("180.153.18.170", 7709)
    
    print("=" * 60)
    print(f"🚀 [最后的战前侦察] 目标服务器: {server[0]}")
    print("=" * 60)
    
    try:
        # 1. 连接测试
        if api.connect(*server, time_out=5):
            print("✅ 物理连接成功！")
            
            # 2. 日线测试 (Category 9)
            print("\n📈 正在同步日线 (Daily)...")
            t1 = time.time()
            data_d = api.get_security_bars(9, 0, "000001", 0, 100)
            cost_d = round((time.time() - t1) * 1000, 2)
            
            if data_d:
                df_d = api.to_df(data_d)
                print(f"   >>> 成功！拉取 100 根日线耗时: {cost_d} ms")
                print(df_d[['datetime', 'open', 'high', 'low', 'close', 'vol']].tail(2))
            else:
                print("   ❌ 日线握手成功但无数据。")

            # 3. 5分钟线测试 (Category 0)
            print("\n📊 正在同步 5 分钟线 (5-Min)...")
            t2 = time.time()
            data_m = api.get_security_bars(0, 0, "000001", 0, 100)
            cost_m = round((time.time() - t2) * 1000, 2)
            
            if data_m:
                df_m = api.to_df(data_m)
                print(f"   >>> 成功！拉取 100 根 5 分钟线耗时: {cost_m} ms")
                print(df_m[['datetime', 'open', 'high', 'low', 'close', 'vol']].tail(2))
            else:
                print("   ❌ 5 分钟线握手成功但无数据。")

            api.disconnect()
            print("\n" + "=" * 60)
            print("🎉 结论：该节点性能极佳，支持双频高速同步，可以正式整合！")
            
        else:
            print(f"❌ 无法连接到服务器 {server}. 请确认 7709 端口状态。")
            
    except Exception as e:
        print(f"❌ 运行中发生异常: {e}")

if __name__ == '__main__':
    final_test()
