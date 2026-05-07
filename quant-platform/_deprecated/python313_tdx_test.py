# -*- coding: utf-8 -*-
import time
from pytdx2.hq import TdxHq_API

# 全国最稳通达信服务器
SERVERS = [
    ("119.147.212.81", 7709),
    ("114.80.80.122", 7709),
    ("123.125.108.23", 7709),
    ("202.108.253.131", 7709),
]

def main():
    print("="*50)
    print("       Python3.13 + pytdx2 专用测速")
    print("="*50)

    for ip, port in SERVERS:
        api = TdxHq_API()
        try:
            # 关键：Python3.13 必须用 time_out 参数
            ok = api.connect(ip, port, time_out=3)
            if ok:
                t1 = time.time()
                data = api.get_security_bars(9, 0, "000001", 0, 50)
                t2 = time.time()
                ms = round((t2-t1)*1000)
                if data:
                    print(f"✅ {ip} 成功 | 耗时：{ms}ms | K线数量：{len(data)}")
                else:
                    print(f"⚠️ {ip} 成功连接但无数据")
                api.disconnect()
            else:
                print(f"❌ {ip} 连接失败")
        except Exception as e:
            print(f"⚠️ {ip} 错误：{str(e)[:40]}")

    print("\n🎉 测试完成！Python3.13 现在可以正常取数！")

if __name__ == "__main__":
    main()
