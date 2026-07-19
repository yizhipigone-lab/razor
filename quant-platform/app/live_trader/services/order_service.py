"""下单核心服务(阶段3, 2026-07-19 从 main.py 抽离)。

place_order_service 是下单核心入口,供 /live/order(WEB)和 /live/buy-signal(TDX)共用。
委托给 OrderExecutor(从 _state 取实例)。

历史:原 main.py:88-105。函数内相对 import 前缀 .→..(审计 C1/A,搬到 services/ 深一级)。
"""
from core.logger import get_logger

from .._state import state as _state

logger = get_logger("live_trader.main")  # 审计 R6:沿用 main 名


def place_order_service(intent, source: str = "WEB", lock_wait_sec: int = 30) -> dict:
    """下单核心逻辑 — 委托给 OrderExecutor(候选③)。

    供 /live/order(WEB)和 /live/buy-signal(TDX)共用。

    Args:
        intent: OrderIntent 下单意图
        source: 下单来源 "WEB"/"TDX",决定价格策略和 terminal 标记
        lock_wait_sec: 清仓锁等待秒数(手动30s, buy-signal 5s)

    Returns:
        dict 下单结果 {"ok", "order_id", "client_order_id", "status", "reason", ...}
    """
    from ..order_executor import OrderExecutor  # 审计 C1:双点(原 main 为 from .order_executor)
    executor: OrderExecutor = _state.get("executor")
    if not executor:
        return {"ok": False, "status": "error", "reason": "OrderExecutor 未初始化"}
    return executor.execute(intent, source=source, lock_wait_sec=lock_wait_sec)
