# -*- coding: utf-8 -*-
import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from pytdx2.hq import TdxHq_API

def test_intensive_sync():
    server = ("180.153.18.170", 7709)
    stocks = ["000001", "600000", "000002", "600016", "300059", "601318", "000725", "601988", "002594", "600519"] * 20 # 200只
    
    print("=" * 60)
    print(f"🚀 [极限内测] 服务器: {server[0]} | 股票总数: {len(stocks)} | 线程数: 20")
    print("=" * 60)
    
    success_d, success_m = 0, 0
    t_start = time.time()
    
    def _fetch(code):
        api = TdxHq_API()
        try:
            if api.connect(*server, time_out=3):
                market = 1 if code.startswith('6') else 0
                # 抓取日线
                d = api.get_security_bars(9, market, code, 0, 100)
                # 抓取5分钟线
                m = api.get_security_bars(0, market, code, 0, 100)
                api.disconnect()
                return (True if d else False, True if m else False)
        except: pass
        return (False, False)

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(_fetch, c) for c in stocks]
        for f in as_completed(futures):
            d_ok, m_ok = f.result()
            if d_ok: success_d += 1
            if m_ok: success_m += 1

    t_end = time.time()
    total_cost = t_end - t_start
    avg_per_stock = (total_cost / len(stocks)) * 1000 # 毫秒
    
    print("\n" + "=" * 60)
    print(f"📊 [内测结果简报]")
    print(f"⏱️ 总耗时: {round(total_cost, 2)} 秒")
    print(f"⚡ 单股平均耗时: {round(avg_per_stock, 2)} 毫秒 (含双频拉取)")
    print(f"✅ 日线成功率: {success_d}/{len(stocks)} ({round(success_d/len(stocks)*100, 1)}%)")
    print(f"✅ 5分线成功率: {success_m}/{len(stocks)} ({round(success_m/len(stocks)*100, 1)}%)")
    print("=" * 60)
    
    if success_d > 190:
        print("🎉 优异！服务器在 20 线程压榨下依然保持极佳稳定性，正式环境可大胆起飞。")
    else:
        print("⚠️ 警告：检测到少量丢包，建议将正式环境线程数下调至 10-15。")

if __name__ == '__main__':
    test_intensive_sync()
