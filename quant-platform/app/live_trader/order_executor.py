"""OrderExecutor 深 module(候选③)

设计:小接口大实现 — 3 路下单统一到 OrderExecutor.execute()
+ 外部调用点仍只负责"信号→intent"+"策略 hook"

接口:
    executor.execute(intent, source, lock_wait_sec,
                     *,
                     cancel_inflight=False,
                     risk_positions_only=False,
                     persist_live_orders=True,
                     on_order_submitted=None)

调用方契约:
- WEB/TDX:默认行为(persist_live_orders=True, 无 cancel, 无 on_order_submitted)
- EXIT(exit_monitor):cancel_inflight=True / persist_live_orders=False /
                      on_order_submitted=tp_mark_fn

依赖注入(构造时):
  config / runtime_state / store / qmt / risk_gate /
  clearance_lock / kill_switch / callback_handler / audit / notifier
"""
from datetime import datetime
from typing import Callable, Optional

from app.utils.xtquant_compat import ORDER_TYPE_BUY, ORDER_TYPE_SELL, format_code
from core.logger import get_logger

from .schemas import OrderIntent

logger = get_logger("live_trader.order_executor")

SourceType = str  # "WEB" / "TDX" / "EXIT" / "SCHEDULER"


class OrderExecutor:
    """下单元语 — kill_switch → 幂等 → 定价 → 风控 → 锁 → 撤在途(EXIT)→ 提交 → 写 DB(可选)→ 审计 → 回调"""

    def __init__(self, *, config, runtime_state, store, qmt,
                 risk_gate, clearance_lock, kill_switch,
                 callback_handler, audit, notifier):
        self.config = config
        self.runtime_state = runtime_state
        self.store = store
        self.qmt = qmt
        self.risk_gate = risk_gate
        self.clearance_lock = clearance_lock
        self.kill_switch = kill_switch
        self.callback_handler = callback_handler
        self.audit = audit
        self.notifier = notifier

    def execute(
        self,
        intent: OrderIntent,
        source: SourceType = "WEB",
        lock_wait_sec: int = 30,
        *,
        cancel_inflight: bool = False,
        risk_positions_only: bool = False,
        persist_live_orders: bool = True,
        on_order_submitted: Optional[Callable[[int, OrderIntent], None]] = None,
    ) -> dict:
        """统一下单入口

        Args:
            intent: 下单意图(必须)
            source: "WEB" / "TDX" / "EXIT"(EXIT=exit_monitor 自动卖)
            lock_wait_sec: 清仓锁等待秒数(WEB=30, TDX=5, EXIT=0)
            cancel_inflight: EXIT 专用 — 提交前撤掉同 code 在途单
            risk_positions_only: EXIT 专用 — 风控只传 positions(asset/quote=None)
            persist_live_orders: 是否写 live_orders 表(EXIT=False 保留旧行为)
            on_order_submitted: 提交成功后回调(EXIT 用作 TP 档位标记)

        Returns:
            dict {"ok", "status", "order_id", "client_order_id", "code",
                  "reason", "mode", "source", "gates"(可选)}
        """
        if not self.config:
            return {"ok": False, "status": "error", "reason": "未初始化"}

        # 1. kill switch 二次检查
        if self.kill_switch and self.kill_switch.is_active():
            return {"ok": False, "status": "forbidden", "reason": "kill switch 已激活"}

        # 2. C3 幂等(按 client_order_id)
        if self.store and intent.client_order_id:
            existing = self.store.get_order_by_client_id(intent.client_order_id)
            if existing:
                return {
                    "ok": True, "order_id": existing.get("order_id"),
                    "client_order_id": intent.client_order_id,
                    "status": "duplicate", "reason": "幂等命中,不重复下单",
                }

        # 3. 格式化代码
        code_fmt = format_code(intent.code) if "." not in intent.code else intent.code

        # 4. TDX source:用 QMT 实时价覆盖信号源传的价格
        actual_price = intent.price
        if source == "TDX" and self.qmt and self.qmt.connected:
            try:
                quotes = self.qmt.get_realtime_quotes([code_fmt])
                qmt_price = quotes.get(code_fmt, {}).get("lastPrice", 0)
                if qmt_price and qmt_price > 0:
                    actual_price = float(qmt_price)
                    logger.info(f"TDX 信号价格覆盖: {intent.price} → QMT实时价 {actual_price}")
            except Exception as e:
                logger.warning(f"TDX 价格覆盖失败,用原始价格: {e}")

        # 5. 拉行情+持仓+资产(给 RiskGate;EXIT 模式跳过)
        asset = positions = quote = None
        if not risk_positions_only and self.qmt and self.qmt.connected:
            try:
                asset = self.qmt.query_asset()
            except Exception:
                pass
            if self.store:
                try:
                    positions = self.store.get_positions(managed_only=True)
                except Exception:
                    pass
            try:
                quotes = self.qmt.get_realtime_quotes([code_fmt])
                quote = quotes.get(code_fmt) if quotes else None
            except Exception:
                pass

        # 6. RiskGate 检查(EXIT 模式只传 positions)
        if self.risk_gate:
            passed, gates, reason = self.risk_gate.check(
                intent, asset=asset, positions=positions, quote=quote,
            )
            if not passed:
                if self.audit:
                    self.audit.gate_reject(code_fmt, intent.direction, reason)
                return {
                    "ok": False, "client_order_id": intent.client_order_id,
                    "code": code_fmt, "status": "risk_rejected", "reason": reason,
                    "gates": gates,
                }
            if self.audit:
                self.audit.gate_pass(code_fmt, intent.direction,
                                     str([g for g in gates if g.get("passed")]))

        # 7. 清仓锁(acquire_with_wait 支持 0=EXIT 不等待)
        lock_acquired = False
        if self.clearance_lock:
            lock_acquired = self.clearance_lock.acquire_with_wait(
                code_fmt, timeout_sec=lock_wait_sec,
            )
            if not lock_acquired:
                reason_msg = f"{code_fmt} 清仓锁冲突,等待{lock_wait_sec}s未获取"
                if source == "TDX":
                    logger.warning(f"信号跳过: {reason_msg}")
                    if self.notifier:
                        try:
                            self.notifier.send(f"⚠ 信号跳过: {reason_msg}")
                        except Exception:
                            pass
                return {
                    "ok": False, "client_order_id": intent.client_order_id,
                    "code": code_fmt, "status": "locked", "reason": reason_msg,
                }

        try:
            # 7.5 EXIT 专用:撤在途单(风控前置 + 风控 gate 已过)
            if cancel_inflight and self.qmt:
                try:
                    orders = self.qmt.query_orders(cancelable_only=True)
                    for o in (orders or []):
                        if o.get("code", "").split(".")[0] == code_fmt.split(".")[0]:
                            oid = o.get("order_id")
                            if oid:
                                self.qmt.cancel_order(oid)
                                logger.info(f"撤在途单 {code_fmt} oid={oid}")
                except Exception as e:
                    logger.warning(f"撤在途单失败 {code_fmt}: {e}")

            # 8. 提交订单(dry-run mock 或 live qmt)
            is_dry_run = self.runtime_state and self.runtime_state.is_dry_run()
            if is_dry_run:
                if not self.callback_handler:
                    return {"ok": False, "status": "error", "reason": "callback_handler 未初始化"}
                order_id = self.callback_handler.mock_order_async_response(
                    intent.client_order_id, code_fmt, intent.direction,
                    intent.volume, actual_price, intent.price_type,
                    intent.strategy_name, intent.reason,
                )
            else:
                if not self.qmt or not self.qmt.connected:
                    return {"ok": False, "status": "error", "reason": "QMT 未连接"}
                order_type = ORDER_TYPE_BUY if intent.direction == "buy" else ORDER_TYPE_SELL
                seq = self.qmt.order_stock_async(
                    code_fmt, order_type, intent.volume,
                    intent.price_type, actual_price,
                    intent.strategy_name, intent.reason,
                )
                order_id = seq
                # H3 防御:即便 qmt_wrapper 通常 raise(seq<=0),某些 mock/legacy 实现可能
                # 直接返回 0 不抛,显式拦截避免审计写入幽灵订单
                if seq is None or seq <= 0:
                    if self.clearance_lock and lock_acquired:
                        self.clearance_lock.release(code_fmt)
                    return {
                        "ok": False, "status": "qmt_rejected",
                        "code": code_fmt, "client_order_id": intent.client_order_id,
                        "reason": f"QMT 下单未接受 seq={seq}",
                    }
                # C1:买入成功后冻在途预扣
                if intent.direction == "buy" and self.risk_gate:
                    self.risk_gate.freeze_pending_buy(code_fmt, intent.volume)

            # 9. 写 live_orders(WEB/TDX;EXIT 默认不写,保留旧行为防破坏交易查询页)
            if persist_live_orders and self.store:
                now = datetime.now()
                terminal = "TDX" if source == "TDX" else intent.terminal
                self.store.sync_terminal_write("order", {
                    "order_id": order_id or 0,
                    "client_order_id": intent.client_order_id,
                    "code": code_fmt,
                    "direction": intent.direction,
                    "volume": intent.volume,
                    "price": actual_price,
                    "price_type": intent.price_type,
                    "status": 50,
                    "status_msg": "已提交",
                    "seq": order_id or 0,
                    "mode": (self.runtime_state.mode if self.runtime_state
                             else self.config.mode),
                    "strategy_name": intent.strategy_name,
                    "order_remark": intent.reason,
                    "terminal": terminal,
                    "created_at": now,
                    "updated_at": now,
                })

            # 10. 审计(WB/TDX/EXIT 都写)
            mode = (self.runtime_state.mode if self.runtime_state
                    else self.config.mode)
            if self.audit:
                self.audit.order_placed(code_fmt, order_id, mode, {
                    "direction": intent.direction, "volume": intent.volume,
                    "price": actual_price, "price_type": intent.price_type,
                    "source": source,
                })

            logger.info(
                f"下单成功 {code_fmt} {intent.direction} {intent.volume}@{actual_price}"
                f" oid={order_id} mode={mode} source={source}"
            )

            # 11. 提交成功回调(EXIT 用作 TP 档位标记)
            # 回调异常时 logger.exception 带 traceback,主路径仍返回 ok=True
            # (锁释放由 callback_handler 在 QMT 成交通知时通过 release_by_order_id 兜底)
            if on_order_submitted is not None and order_id and order_id > 0:
                try:
                    on_order_submitted(order_id, intent)
                except Exception:
                    logger.exception(
                        f"on_order_submitted 回调异常 {code_fmt} oid={order_id}"
                        f"(订单已提交成功,只是 TP 标记/扩展处理失败)"
                    )

            return {
                "ok": True, "order_id": order_id,
                "client_order_id": intent.client_order_id,
                "code": code_fmt,
                "status": "submitted", "reason": "",
                "mode": mode, "source": source,
            }

        except Exception as e:
            logger.error(f"下单异常 {intent.code}: {e}")
            if self.clearance_lock and lock_acquired:
                self.clearance_lock.release(code_fmt)
            return {"ok": False, "status": "error", "reason": f"下单异常: {e}"}
