from app.screener.engine import ScreenerEngine
from datetime import date

def test():
    engine = ScreenerEngine()
    print("🚀 启动引擎测试...")
    
    def mock_callback(curr, total, msg):
        print(f"📡 [WS模拟回调] {curr}/{total} - {msg}")

    # 给一个必然触发的代码清单
    res = engine.run_scan(
        strategy_name="RPS-VCP动量突破",
        freq="daily",
        progress_callback=mock_callback
    )
    print(f"✅ 测试完成，选出 {len(res)} 只股票")

if __name__ == "__main__":
    test()
