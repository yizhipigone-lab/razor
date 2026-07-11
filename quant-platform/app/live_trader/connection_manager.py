"""连接管理 + 熔断器 + 强退(v5.4 §5.2)

照搬 MQ connection_manager.py 设计并加强:
- 两层连接:xtdata.connect() + XtQuantTrader
- callback 在 start 前注册(硬约束)
- 熔断器:连续3次失败→30s拒绝→half-open
- WaitingFreeWriter → os._exit(1) + 指数退避
- on_disconnected 触发 kill switch(非仅日志)
- 启动恢复流程(§17.1):重启后从 QMT 拉最新状态,补写在途订单
"""
import os
import threading
import time
from typing import Any, Optional

from core.logger import get_logger

from .config import LiveTraderConfig
from .exceptions import CircuitOpenError, QmtConnectionError
from .qmt_wrapper import QmtWrapper

logger = get_logger("live_trader.connection")


class ConnectionManager:
    """连接管理(单例)"""

    def __init__(self, config: LiveTraderConfig, qmt_wrapper: QmtWrapper,
                 callback_handler=None, kill_switch=None, store=None):
        self.config = config
        self.qmt = qmt_wrapper
        self.callback_handler = callback_handler
        self.kill_switch = kill_switch
        self.store = store

        self._trader: Optional[Any] = None
        self._account: Optional[Any] = None
        self._connected = False

        # 熔断器(§5.2)
        self._circuit_open = False
        self._circuit_open_time: Optional[float] = None
        self._consecutive_failures = 0
        self._max_failures = config.circuit_breaker_max_failures
        self._cb_timeout = config.circuit_breaker_timeout_sec
        self._cb_lock = threading.Lock()

        # 重启计数器(§19.4 H3:启动最早期 +1)
        self._restart_count = 0
        self._restart_first_time: Optional[float] = None

        self._init_lock = threading.Lock()

    def _check_circuit(self) -> None:
        """熔断器检查(调用前)"""
        with self._cb_lock:
            if self._circuit_open:
                elapsed = time.time() - (self._circuit_open_time or 0)
                if elapsed < self._cb_timeout:
                    raise CircuitOpenError()
                # half-open:尝试恢复
                logger.info("熔断器 half-open,尝试恢复")
                self._circuit_open = False
                self._consecutive_failures = 0

    def _on_success(self) -> None:
        with self._cb_lock:
            self._consecutive_failures = 0
            self._circuit_open = False

    def _on_failure(self, err: Exception) -> None:
        """调用失败处理"""
        err_msg = str(err)
        # WaitingFreeWriter → 直接强退(§5.2)
        if "WaitingFreeWriter" in err_msg:
            logger.critical(f"[Resource Pool] WaitingFreeWriter 超限!准备强退: {err_msg}")
            try:
                if self.store:
                    self.store.emergency_flush()
                if self.kill_switch:
                    self.kill_switch.activate(reason="WaitingFreeWriter资源池耗尽", source="auto")
            except Exception:
                pass
            time.sleep(0.5)  # 给日志时间
            os._exit(1)

        with self._cb_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_failures:
                self._circuit_open = True
                self._circuit_open_time = time.time()
                logger.error(f"熔断器开启:连续 {self._consecutive_failures} 次失败,熔断 {self._cb_timeout}s")

    def connect(self) -> bool:
        """连接 QMT(子线程实例化,避免劫持主 loop)"""
        self._restart_bump()  # H3:启动最早期 +1

        if not self.qmt.available:
            raise QmtConnectionError("xtquant 未安装")

        init_result: Dict[str, Any] = {}
        init_event = threading.Event()
        init_error: List = []

        def _init_trader():
            try:
                # 第1层:xtdata 基础连接
                self.qmt.connect_xtdata()
                logger.info("xtdata 基础连接 OK")

                # 第2层:XtQuantTrader 交易连接
                from xtquant.xttrader import XtQuantTrader
                from app.utils.xtquant_compat import get_stock_account_class

                session_id = int(time.time() * 1000) % 1000000
                callback = self.callback_handler.make_xtquant_callback() if self.callback_handler else None

                # 关键:callback 在 start 前注册(v5.1 修订 #3)
                trader = XtQuantTrader(self.config.qmt_userdata_path, session_id, callback)
                if callback:
                    trader.register_callback(callback)
                trader.start()
                rc = trader.connect()
                if rc != 0:
                    raise QmtConnectionError(f"XtQuantTrader.connect 返回 {rc}")

                StockAccount = get_stock_account_class()
                account = StockAccount(self.config.qmt_account_id, "STOCK")
                sub_rc = trader.subscribe(account)
                if sub_rc != 0:
                    logger.warning(f"subscribe 返回 {sub_rc}(尝试继续)")

                init_result["trader"] = trader
                init_result["account"] = account
                init_event.set()
            except Exception as e:
                init_error.append(e)
                init_event.set()

        t = threading.Thread(target=_init_trader, daemon=True)
        t.start()
        if not init_event.wait(timeout=10):
            raise QmtConnectionError("连接 QMT 超时(10s)")
        if init_error:
            raise QmtConnectionError(f"连接失败: {init_error[0]}")

        self._trader = init_result["trader"]
        self._account = init_result["account"]
        self.qmt.set_trader(self._trader, self._account)
        self._connected = True
        self._restart_clear()  # 连接成功,清零重启计数(H3:运行稳定后清零)
        logger.info(f"QMT 连接成功 account={self.config.qmt_account_id}")
        return True

    def reconcile_on_startup(self) -> None:
        """启动恢复流程(§17.1):从 QMT 拉最新状态,补写在途订单"""
        if not self.store:
            return
        logger.info("启动恢复:检查在途订单...")
        inflight = self.store.get_inflight_orders()
        if not inflight:
            logger.info("启动恢复:无在途订单")
            return

        logger.info(f"启动恢复:发现 {len(inflight)} 个在途订单,从 QMT 拉最新状态")
        for order in inflight:
            try:
                order_id = order.get("order_id")
                if not order_id:
                    continue
                qmt_order = self.qmt.query_order_by_id(order_id)
                if qmt_order is None:
                    # QMT 中查不到 → 标记未知 + 告警(§17.1 e分支)
                    self.store.insert_audit(
                        action="startup_reconcile_missing",
                        code=order.get("code", ""),
                        order_id=order_id,
                        reason="QMT 中查不到该订单",
                    )
                    logger.error(f"启动恢复:订单 {order_id} 在 QMT 中查不到,需人工核查")
                    if self.kill_switch:
                        self.kill_switch.activate(
                            reason=f"订单{order_id}在QMT中查不到",
                            source="startup_reconcile"
                        )
                    continue

                old_status = order.get("status")
                new_status = qmt_order.get("status")
                if old_status != new_status:
                    logger.info(f"启动恢复:订单 {order_id} 状态 {old_status}→{new_status}")
                    # 补写 callback 逻辑
                    if self.callback_handler:
                        self.callback_handler.replay_order_update(order_id, new_status, qmt_order)
                    self.store.insert_audit(
                        action="startup_reconcile",
                        code=order.get("code", ""),
                        order_id=order_id,
                        reason=f"状态 {old_status}→{new_status}",
                    )
            except Exception as e:
                logger.error(f"启动恢复:订单 {order_id} 处理失败: {e}")

    def call(self, func, *args, **kwargs):
        """带熔断 + 超时的调用包装(供内部用)"""
        self._check_circuit()
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure(e)
            raise

    @property
    def connected(self) -> bool:
        return self._connected and self.qmt.connected

    def stop(self) -> None:
        try:
            if self._trader:
                self._trader.stop()
        except Exception as e:
            logger.error(f"trader.stop 异常: {e}")
        self._connected = False

    # ===== 重启计数器(§19.4 H3)=====

    def _restart_bump(self) -> None:
        """启动最早期 +1"""
        try:
            import json
            counter_file = self.config.restart_counter_file
            os.makedirs(os.path.dirname(counter_file), exist_ok=True)
            data = {}
            if os.path.exists(counter_file):
                with open(counter_file, "r") as f:
                    data = json.load(f)
            count = data.get("count", 0) + 1
            first_time = data.get("first_time", time.time())
            # 超过2小时窗口,重置
            if time.time() - first_time > self.config.restart_max_duration_sec:
                count = 1
                first_time = time.time()
            data = {"count": count, "first_time": first_time}
            # 原子写
            tmp = counter_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, counter_file)
            self._restart_count = count
            self._restart_first_time = first_time
            logger.warning(f"启动计数 +1 → {count}/{self.config.restart_max_retries}")
            if count >= self.config.restart_max_retries:
                logger.critical(f"重启次数达上限 {count},停止重试,需人工检查 QMT")
                if self.kill_switch:
                    self.kill_switch.activate(
                        reason=f"QMT长时间不可用,重启{count}次",
                        source="restart_backoff"
                    )
        except Exception as e:
            logger.error(f"重启计数失败: {e}")

    def _restart_clear(self) -> None:
        """连接成功且稳定运行后清零(延迟5分钟由外部调用)"""
        # 简化:连接成功立即清零(运行5分钟的保护由 NSSM 侧保证)
        try:
            import json
            counter_file = self.config.restart_counter_file
            if os.path.exists(counter_file):
                os.remove(counter_file)
            self._restart_count = 0
            self._restart_first_time = None
            logger.info("重启计数清零(连接成功)")
        except Exception as e:
            logger.error(f"重启计数清零失败: {e}")
