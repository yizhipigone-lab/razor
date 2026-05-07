import requests
import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

def get_eastmoney_kline(code):
    market = "1" if code.startswith('6') or code.startswith('5') else "0"
    url = f"http://push2his.eastmoney.com/api/qt/stock/kline/get?secid={market}.{code}&ut=fa5fd1943c7b386f172d6893dbfba10b&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&fqt=1&end=20500101&lmt=120"
    
    start_t = time.time()
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        klines = data.get("data", {}).get("klines", [])
        elapsed = time.time() - start_t
        return {"code": code, "count": len(klines), "time": elapsed, "status": "OK"}
    except Exception as e:
        elapsed = time.time() - start_t
        return {"code": code, "count": 0, "time": elapsed, "error": str(e), "status": "FAIL"}

def run_stress_test():
    test_codes = ["600000", "000001", "300059", "600519", "000725", "601318", "002415", "600036", "000002", "600887"]
    print(f"🚀 启动东财 API 并发测试 (并发数: 10)...\n")
    
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_eastmoney_kline, code): code for code in test_codes}
        for future in as_completed(futures):
            results.append(future.result())
    
    df = pd.DataFrame(results)
    print(df.to_string(index=False))
    print(f"\n✅ 测试完成。平均耗时: {df['time'].mean():.3f}s")

if __name__ == '__main__':
    run_stress_test()
