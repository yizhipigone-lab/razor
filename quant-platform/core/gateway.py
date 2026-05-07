from core.logger import get_logger
from core.settings import settings
import threading

log = get_logger("GatewayManager")

_gateway = None
_gateway_lock = threading.Lock()

def get_gateway():
    """根据配置获取并初始化对应交易网关（单例，线程安全）"""
    global _gateway
    if _gateway is not None:
        return _gateway

    with _gateway_lock:
        # 双重检查锁定
        if _gateway is not None:
            return _gateway
        gw_name = settings.active_gateway
        log.info(f"正在初始化网关: {gw_name}")

        if gw_name == "easytrader":
            from app.trader.gateways.ths import ths_gateway
            _gateway = ths_gateway
        elif gw_name == "qmt":
            from app.trader.gateways.qmt import qmt_gateway
            _gateway = qmt_gateway
        else:
            log.warning(f"未知网关配置: {gw_name}，使用 THS 默认")
            from app.trader.gateways.ths import ths_gateway
            _gateway = ths_gateway

        # 尝试连接
        if hasattr(_gateway, "connect"):
            connected = _gateway.connect()
            if not connected:
                log.warning(f"网关 {gw_name} 连接失败，将以模拟模式运行")

    return _gateway
