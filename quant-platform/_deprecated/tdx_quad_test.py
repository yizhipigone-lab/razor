# -*- coding: utf-8 -*-
from pytdx2.hq import TdxHq_API
import time

SERVERS = [
    ("119.147.212.81", 7709),
    ("114.80.80.122", 7709),
    ("123.125.108.23", 7709),
    ("202.108.253.131", 7709),
]

def quad_test():
    print("🚀 [Quad Sync] 启动四路并测引擎...")
    
    for host, port in SERVERS:
        api = TdxHq_API()
        print(f"📡 正在探测 [{host}:{port}]...")
        try:
            # 极简连接
            if api.connect(host, port):
                print(f"✅ [{host}] 握手成功！！！正在试拉 K 线...")
                # 试拉平安银行 10 根 5 分钟线
                data = api.get_security_bars(0, 0, "000001", 0, 10)
                if data:
                    print(f"🎉 最终大获全胜！[{host}] 已跑通数据流！")
                    api.disconnect()
                    return True
                else:
                    print(f"⚠️ [{host}] 连接成功但数据包为空。")
                api.disconnect()
            else:
                print(f"❌ [{host}] 连接被拒绝。")
        except Exception as e:
            msg = str(e)[:50]
            print(f"⚠️ [{host}] 故障: {msg}")
        
        # 暂停 1 秒再试下一个，防风控
        time.sleep(1)
        
    return False

if __name__ == '__main__':
    if not quad_test():
        print("\n❌ 所有通达信节点均无法完成协议握手。建议回归 80 端口 HTTP 极速专线。")
