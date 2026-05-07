import tushare as ts
import pandas as pd
from datetime import datetime

TUSHARE_TOKEN = '5051cf6cf52ca062ca348ab11c615ecb6b7909085d33cc11bc6f7ece'

def test_power():
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()
    print(f"🚀 正在测试 Tushare Token 效能...")
    
    # 测试平安银行 (000001.SZ) 的日线数据
    try:
        df = pro.daily(ts_code='000001.SZ', start_date='20240301', end_date='20240325')
        if not df.empty:
            print("✅ Token 权限充足！日线拉取成功。")
            print(df.head(2))
        else:
            print("⚠️ Token 响应为空，可能没有该合约权限。")
    except Exception as e:
        print(f"❌ 权限受阻: {e}")

if __name__ == '__main__':
    test_power()
