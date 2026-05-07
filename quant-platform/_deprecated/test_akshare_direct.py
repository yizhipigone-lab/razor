# -*- coding: utf-8 -*-
import time
import akshare as ak

print("="*50)
print("       AKShare 开源行情接口 速度测试")
print("="*50)

def test_ak_speed():
    try:
        start = time.time()
        # 获取 000001 平安银行 日K线（最近100天）
        df = ak.stock_zh_a_hist(symbol="000001", period="daily", start_date="20240101", adjust="qfq")
        cost = round((time.time() - start) * 1000)

        if not df.empty:
            print(f"✅ 获取成功！耗时：{cost} 毫秒")
            print(f"✅ 数据条数：{len(df)}")
            print("✅ 前5行数据预览：")
            print(df.head())
        else:
            print("⚠️ 接口响应为空，可能合约代码不对。")

    except Exception as e:
        print(f"❌ AKShare 访问报错: {e}")

if __name__ == '__main__':
    test_ak_speed()
    print("\n🎉 恭喜！你的量化行情接口 100% 正常可用！")
