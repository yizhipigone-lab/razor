import sys
import os
from pathlib import Path

# 添加项目根目录到路径
ROOT_DIR = Path(__file__).parent
sys.path.append(str(ROOT_DIR))

try:
    from core.settings import settings
    from core.gateway import get_gateway
    from core.logger import get_logger
except ImportError as e:
    print(f"导入失败，请确保在项目根目录下运行: {e}")
    sys.exit(1)

log = get_logger("QMTVerify")

def verify_qmt():
    print("=== QMT (XtQuant) 连接测试 ===")
    
    # 强制设置网关为 qmt 进行测试
    settings.set("gateway", "active_gateway", "qmt")
    
    qmt_path = settings.get("gateway", "qmt_path")
    account_id = settings.get("gateway", "account_id")
    
    print(f"配置信息:")
    print(f"  QMT 路径: {qmt_path}")
    print(f"  账户 ID: {account_id}")
    
    if "BokerName" in qmt_path or account_id == "12345678":
        print("\n[警告] 检测到默认占位符配置，请在 config/app_setting.json 中填入真实的 QMT 路径和账号。")
        return

    print("\n正在尝试连接 QMT 客户端...")
    try:
        gw = get_gateway()
        if gw and hasattr(gw, "_connected") and gw._connected:
            print("[成功] QMT 已连接！")
            
            print("\n正在获取余额信息...")
            balance = gw.get_balance()
            if balance:
                print(f"  可用资金: {balance.get('cash')}")
                print(f"  总资产: {balance.get('total_asset')}")
            else:
                print("  [失败] 无法获取余额信息。")
                
            print("\n正在获取持仓信息...")
            positions = gw.get_position()
            if positions is not None:
                print(f"  持仓品种数: {len(positions)}")
                for p in positions:
                    print(f"    - {p['code']}: {p['volume']}股, 市值 {p['market_value']}")
            else:
                print("  [失败] 无法获取持仓信息。")
                
        else:
            print("[失败] QMT 连接失败，请检查 MiniQMT 客户端是否已启动且路径正确。")
    except Exception as e:
        print(f"[异常] 测试过程发生错误: {e}")

if __name__ == "__main__":
    verify_qmt()
