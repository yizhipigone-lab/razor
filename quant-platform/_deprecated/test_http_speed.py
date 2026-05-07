import requests
import time

def test_tencent_http():
    print("🌐 正在测试 [腾讯行情 HTTP 接口]...")
    # 浦发银行 (sh600000)
    url = "https://qt.gtimg.cn/q=sh600000"
    
    try:
        start = time.time()
        resp = requests.get(url, timeout=5)
        cost = round((time.time() - start) * 1000, 2)
        
        if resp.status_code == 200:
            print(f"✅ 腾讯行情 成功拿回数据 | 耗时: {cost} ms")
            print(f"实时快照: {resp.text[:80]}...")
        else:
            print(f"❌ 腾讯接口 响应异常: {resp.status_code}")
    except Exception as e:
        print(f"❌ 腾讯专线 也无法通过: {e}")

if __name__ == '__main__':
    test_tencent_http()
