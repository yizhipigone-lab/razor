from pytdx.hq import TdxHq_API
import pandas as pd
import time

def test_pytdx():
    api = TdxHq_API()
    
    # 通达信常用高速主站
    servers = [
        ('119.147.212.81', 7709), # 深圳广发
        ('61.152.168.225', 7709), # 上海电信
        ('124.74.236.94', 7709)   # 上海联通
    ]
    
    print("📡 正在探测 [通达信极速协议] 通道...")
    
    connected_server = None
    for ip, port in servers:
        try:
            if api.connect(ip, port, time_out=2):
                print(f"✅ 握手成功: {ip}:{port}")
                connected_server = (ip, port)
                break
        except Exception as e:
            print(f"⚠️ {ip} 连接超市或失败: {e}")
            
    if not connected_server:
        print("❌ 未能发现可用的通达信服务器，请检查防火墙 7709 端口是否开放。")
        return

    # 测试拉取 80 根 5 分钟 K 线 (000001 平安银行)
    # category 为 K 线种类: 0 5分钟, 4 1分钟, 9 日线, 11 周线
    try:
        start_time = time.time()
        # get_security_bars 参数: category, market(0:SZ, 1:SH), code, start, count
        data = api.get_security_bars(0, 0, '000001', 0, 100)
        elapsed = time.time() - start_time
        
        if data:
            df = api.to_df(data)
            print(f"\n🚀 极速下载测试通过！耗时: {elapsed*1000:.2f} 毫秒")
            print("数据样本 (5 分钟线):")
            print(df.tail(3))
        else:
            print("⚠️ 握手成功但无报价数据下传。")
            
    except Exception as e:
        print(f"❌ 数据拉取失败: {e}")
    finally:
        api.disconnect()

if __name__ == '__main__':
    test_pytdx()
