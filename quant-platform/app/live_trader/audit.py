"""审计日志(v5.4 §11)

每笔决策可回放:信号→闸门→下单→回调→成交。
5 分钟内可回放任意订单的完整链路。
"""
import json
from datetime import datetime
from typing import Any, Dict, Optional

from core.logger import get_logger

logger = get_logger("live_trader.audit")


class AuditLogger:
    """审计日志(写 live_audit 表)"""

    def __init__(self, store=None):
        self.store = store

    def log(self, action: str, code: str = "", order_id: Optional[int] = None,
            gate_result: str = "", reason: str = "", snapshot: Optional[Dict] = None) -> None:
        """记录审计

        Args:
            action: 动作(signal/gate_pass/gate_reject/order_placed/order_filled/...)
            code: 股票代码
            order_id: 订单ID
            gate_result: 闸门结果(pass/reject/8闸门详情)
            reason: 原因
            snapshot: 快照(订单/持仓/行情)
        """
        if not self.store:
            logger.info(f"[AUDIT] {action} code={code} oid={order_id} gate={gate_result} reason={reason}")
            return
        try:
            snapshot_str = json.dumps(snapshot, default=str, ensure_ascii=False) if snapshot else ""
            self.store.insert_audit(action, code, order_id, gate_result, reason, snapshot_str)
        except Exception as e:
            logger.error(f"审计写入失败: {e}")

    def replay(self, order_id: int) -> Dict[str, Any]:
        """回放订单完整链路(5分钟内可查)"""
        if not self.store:
            return {}
        try:
            assert self.store._conn is not None
            rows = self.store._conn.execute(
                "SELECT * FROM live_audit WHERE order_id = ? ORDER BY id",
                [order_id]
            ).fetchall()
            if not rows:
                return {"order_id": order_id, "events": [], "found": False}
            cols = [d[0] for d in self.store._conn.description]
            events = [dict(zip(cols, r)) for r in rows]
            return {"order_id": order_id, "events": events, "found": True}
        except Exception as e:
            logger.error(f"回放失败: {e}")
            return {"error": str(e)}

    def gate_pass(self, code: str, gates: str, snapshot: Optional[Dict] = None) -> None:
        self.log("gate_pass", code=code, gate_result=gates, snapshot=snapshot)

    def gate_reject(self, code: str, gate: str, reason: str, category: str = "risk") -> None:
        self.log("gate_reject", code=code, gate_result=f"{gate}:{category}", reason=reason)

    def order_placed(self, code: str, order_id: int, mode: str, snapshot: Optional[Dict] = None) -> None:
        self.log("order_placed", code=code, order_id=order_id, gate_result=mode, snapshot=snapshot)
