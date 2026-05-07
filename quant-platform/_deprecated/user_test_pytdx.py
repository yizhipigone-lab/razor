# -*- coding: utf-8 -*-
import time
from pytdx2.hq import TdxHq_API

# 通达信官方最快服务器列表
TDX_SERVERS = [
    ("119.147.212.81", 7709),
    ("114.80.80.122", 7709),
    ("123.125.108.23", 7709),
    ("202.108.253.131", 7709),
]

def test_pytdx_speed():
    print("=" * 60)
    print("        pytdx 通达信行情速度测试（修复版）")
    print("=" * 60)

    for host, port in TDX_SERVERS:
        api = TdxHq_API()
        try:
            # 关键修复：使用 time_out 而非 timeout
            # 尝试连接
            success = api.connect(host, port, time_out=2)

            if not success:
                print(f"❌ {host}:{port} 连接失败")
                continue

            start = time.time()
            # 获取 80 根 K 线
            data = api.get_security_bars(9, 0, "000001", 0, 80)
            cost = round((time.time() - start) * 1000, 2)

            if data:
                print(f"✅ {host}:{port} 成功 | 获取速度：{cost} ms")
            else:
                print(f"⚠️ {host}:{port} 成功连接但无数据返回")
            api.disconnect()

        except Exception as e:
            print(f"⚠️ {host} 异常：{str(e)[:50]}")
            continue

    print("\n🎉 测试完成！能用就是正常！")

if __name__ == "__main__":
    test_pytdx_speed()
