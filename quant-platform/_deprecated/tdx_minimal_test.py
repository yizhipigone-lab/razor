# -*- coding: utf-8 -*-
from pytdx2.hq import TdxHq_API

def minimal_test():
    api = TdxHq_API()
    host = "119.147.212.81"
    port = 7709
    
    print(f"🚀 尝试极简连接 [{host}:{port}]...")
    
    try:
        # 只传本尊参数，不加多余
        connected = api.connect(host, port)
        
        if connected:
            print("✅ 连接成功！！！")
            # 试拉 10 根 5 分钟线 (000001.SZ)
            data = api.get_security_bars(0, 0, "000001", 0, 10)
            if data:
                print(f"✅ K线数据条数：{len(data)}")
                print("🎉 通达信数据正常！极速协议已打通！")
            else:
                print("⚠️ 连接成功但无数据。")
            api.disconnect()
        else:
            print("❌ 极简连接依然被服务器拒绝。")
            
    except Exception as e:
        print(f"❌ 运行报错: {e}")

if __name__ == '__main__':
    minimal_test()
