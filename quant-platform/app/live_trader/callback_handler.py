"""Callback 异步回报处理(v5.4 §5.3 / §11)

核心约束:xtquant callback executor 是 max_workers=1(串行),callback 方法体
只做 getattr 数据提取 + 派发到独立 db_executor,禁止同步 IO。

7 个回调 + mock 回报生成器(dry-run 模式用)。
"""
import random
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, Optional

from core.logger import get_logger

from .config import LiveTraderConfig

logger = get_logger("live_trader.callback")

# 终态集合
from app.utils.xtquant_compat import ORDER_STATUS_TERMINAL, status_to_text


class CallbackHandler:
    """xtquant 回调处理 + mock 回报生成器"""

    def __init__(self, config: LiveTraderConfig, store=None, kill_switch=None,
                 clearance_lock=None, pnl_engine=None, notify=None, runtime_state=None,
                 audit=None):
        self.config = config
        self.store = store
        self.kill_switch = kill_switch
        self.clearance_lock = clearance_lock
        self.pnl_engine = pnl_engine
        self.notify = notify
        self.runtime_state = runtime_state  # v2(A6): 运行时 mode/开关
        self.audit = audit  # C3(2026-07-15 全项目审计): 注入 audit, DB 写失败兜底要用

        # db_executor:有界线程池,所有 DB 写入派发到这里(§5.3)
        self._db_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="live-db")

        # order_id → seq 映射(§16.9 LRU,终态立即删 M3)
        self._seq_map: Dict[str, int] = {}
        self._seq_map_lock = threading.Lock()
        self._seq_map_max = 10000

        # 内存 deals 缓存(§18.7,盈亏重算数据源,不依赖归档表)
        self._deals_buffer: Dict[str, deque] = {}  # code → deque(maxlen=500)
        self._deals_lock = threading.Lock()

        # 账户异常状态去重(防止周末服务器维护时 xtquant 持续推送 status=3 触发洪水)
        # 真实硬异常(8/9)仍立即触发;软异常(3/7)需持续 30 秒以上才视为真异常
        self._account_anomaly_first_seen: Dict[int, float] = {}
        self._account_anomaly_lock = threading.Lock()
        self._account_anomaly_window_sec = 30.0  # 软异常持续超过该秒数才触发 kill switch

        self._mock_counter = 100000  # mock 回报 order_id 计数器(仅 dry-run 用;mock 判定走 runtime_state.is_dry_run)

        # order_remark → tag 映射(用于成交通知标签)
        self._TAG_MAP = {
            "signal_buy":   "信号买入",
            "stop_loss":    "止损",
            "take_profit":  "止盈",
            "time_exit":    "时间退出",
            "force_exit":   "强制退出",
            "manual":       "手动",
        }

    def make_xtquant_callback(self):
        """构造 XtQuantTraderCallback 实例(供 ConnectionManager 注册)"""
        from xtquant.xttrader import XtQuantTraderCallback
        from app.utils.xtquant_compat import safe_getattr, safe_int, safe_float

        handler = self

        class LiveCallback(XtQuantTraderCallback):
            def on_disconnected(self):
                logger.error("[CB] QMT 连接断开")
                # v5.4 修复:触发 kill switch(非仅日志)
                if handler.kill_switch:
                    handler.kill_switch.activate(reason="QMT连接断开", source="on_disconnected")
                if handler.store:
                    handler._dispatch_db(handler.store.insert_audit,
                                     action="qmt_disconnected", reason="on_disconnected")

            def on_account_status(self, status):
                # v5.4 修复:监听异常状态(非空实现)
                # 2026-07-04 修复:周末 QMT 服务端维护时,xtquant 会持续推送 status=3
                # 真实硬异常(8/9)仍立即触发 kill switch;
                # 软异常(3/7)需持续 30 秒以上才视为真异常,过滤服务端临时维护误报
                s = safe_getattr(status, "status", 0)
                logger.warning(f"[CB] 账户状态变更: {s}")
                # xtquant 官方枚举(xtconstant.py):
                #   0=OK 正常
                #   1=WAITING_LOGIN 连接中
                #   2=STATUSING 登陆中
                #   3=FAIL 失败(软异常,可能只是服务端临时维护)
                #   4=INITING 初始化中
                #   5=CORRECTING 数据刷新校正中(正常过渡)
                #   6=CLOSED 收盘后
                #   7=ASSIS_FAIL 穿透副链接断开(软异常)
                #   8=DISABLEBYSYS 系统停用(密码错超限,硬异常,立即触发)
                #   9=DISABLEBYUSER 用户停用(硬异常,立即触发)
                now = time.time()

                if s in (8, 9):  # 硬异常:立即触发 kill switch
                    if handler.kill_switch:
                        handler.kill_switch.activate(
                            reason=f"账户状态硬异常:{s}", source="on_account_status"
                        )
                    return

                if s in (3, 7):  # 软异常:30 秒持续才触发
                    with handler._account_anomaly_lock:
                        first_seen = handler._account_anomaly_first_seen.get(s)
                        if first_seen is None:
                            # 首次出现,记录时间戳,只告警不触发
                            handler._account_anomaly_first_seen[s] = now
                            logger.warning(
                                f"[CB] 软异常 status={s} 首次出现,"
                                f"持续 {handler._account_anomaly_window_sec:.0f}s 仍未恢复才触发 kill switch"
                            )
                            return
                        # 持续时长
                        duration = now - first_seen
                        if duration < handler._account_anomaly_window_sec:
                            # 还没到 30 秒,继续观察
                            return
                        # 持续超 30 秒 → 真异常
                        logger.critical(
                            f"[CB] 软异常 status={s} 持续 {duration:.0f}s,"
                            f"超过 {handler._account_anomaly_window_sec:.0f}s 阈值"
                        )
                    if handler.kill_switch:
                        handler.kill_switch.activate(
                            reason=f"账户状态软异常 status={s} 持续 {duration:.0f}s",
                            source="on_account_status"
                        )
                    return

                # 状态 0/1/2/4/5/6 都是正常过渡态,不触发
                # 软异常恢复(收到 OK=0):清空去重计时器
                if s == 0:
                    with handler._account_anomaly_lock:
                        if handler._account_anomaly_first_seen:
                            handler._account_anomaly_first_seen.clear()
                            logger.info("[CB] 账户状态恢复正常,清空软异常计时器")

            def on_stock_order(self, order):
                # 只做提取 + 派发(< 100μs)
                order_id = safe_getattr(order, "order_id", 0)
                status = safe_int(safe_getattr(order, "order_status", 255))
                logger.info(f"[CB] on_stock_order oid={order_id} status={status}({status_to_text(status)})")
                handler._handle_order_update(order_id, status, order)

            def on_stock_trade(self, trade):
                trade_id = safe_getattr(trade, "traded_id", 0)
                order_id = safe_getattr(trade, "order_id", 0)
                logger.info(f"[CB] on_stock_trade tid={trade_id} oid={order_id}")
                # CRITICAL-2(2026-07-15):未知 order_type _handle_trade 会 raise ValueError
                # (已 audit + notify + kill_switch.activate)。这里 try/except 显式隔离,
                # 防一条异常回报拖垮整个 callback 链路——xtquant executor 是单线程串行,
                # 一处 raise 会卡死后续所有回报。
                try:
                    handler._handle_trade(trade)
                except ValueError as e:
                    # 已知协议级异常:已审计+告警+激活 kill switch,仅记录不传播
                    logger.warning(f"[CB] 已知协议级异常(已处理,链路继续): {e}")

            def on_order_error(self, err):
                order_id = safe_getattr(err, "order_id", 0)
                error_id = safe_getattr(err, "error_id", 0)
                error_msg = safe_getattr(err, "error_msg", "")
                logger.error(f"[CB] on_order_error oid={order_id} err={error_id} {error_msg}")
                handler._handle_order_error(order_id, error_id, error_msg)

            def on_cancel_error(self, err):
                order_id = safe_getattr(err, "order_id", 0)
                error_msg = safe_getattr(err, "error_msg", "")
                logger.error(f"[CB] on_cancel_error oid={order_id} {error_msg}")
                if handler.store:
                    handler._dispatch_db(handler.store.insert_audit,
                                     action="cancel_error", order_id=order_id, reason=error_msg)

            def on_order_stock_async_response(self, seq, order_id, err_msg):
                logger.info(f"[CB] async_response seq={seq} oid={order_id} err={err_msg}")
                if order_id and order_id != 0 and seq:
                    with handler._seq_map_lock:
                        handler._seq_map[str(order_id)] = seq
                        handler._cleanup_seq_map()

        return LiveCallback()

    def _dispatch_db(self, func, *args, **kwargs):
        """派发到 db_executor(不阻塞 callback 线程)"""
        self._db_executor.submit(self._safe_db_call, func, *args, **kwargs)

    def _safe_db_call(self, func, *args, **kwargs):
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.error(f"DB 派发失败: {e}")

    # ===== 委托状态更新(幂等 + 状态转换校验)=====

    def _handle_order_update(self, order_id: int, new_status: int, raw_order: Any) -> None:
        if not self.store:
            return
        # 幂等:查现有(先按 QMT 真实 order_id,再按 seq 反查)
        existing = self.store.get_order(order_id)
        if not existing:
            # 下单时用 seq 做临时 order_id,callback 回报时需通过 seq_map 反查
            seq = self._seq_map.get(str(order_id))
            if seq:
                existing = self.store.get_order_by_seq(seq)
            if not existing:
                # 最后尝试:直接用 order_id 当 seq 查(live 模式下单时 order_id=seq)
                existing = self.store.get_order_by_seq(order_id)

        if existing:
            old_status = existing.get("status")
            # 终态后不再处理(§5.3 幂等)
            if old_status in ORDER_STATUS_TERMINAL:
                logger.debug(f"订单 {order_id} 已终态 {old_status},忽略 {new_status}")
                return
            # 状态转换校验(非法转换拒绝)
            if not self._is_valid_transition(old_status, new_status):
                logger.warning(f"订单 {order_id} 非法转换 {old_status}→{new_status},拒绝")
                self._dispatch_db(self.store.insert_audit,
                                  action="invalid_transition", order_id=order_id,
                                  reason=f"{old_status}→{new_status}")
                return

            # 更新 order_id(seq → QMT 真实 order_id)
            old_order_id = existing.get("order_id")
            if old_order_id != order_id and old_order_id is not None:
                # update_order_id 内部已 with self._db_lock 自保护,线程安全;
                # 外层不可再加同一把 threading.Lock(不可重入会死锁)
                self.store.update_order_id(old_order_id, order_id)
                existing["order_id"] = order_id
                logger.info(f"order_id 映射更新: {old_order_id}(seq) → {order_id}(QMT)")

        # 更新订单
        from app.utils.xtquant_compat import safe_getattr, safe_int, safe_float
        update = {
            "order_id": order_id,
            "client_order_id": existing.get("client_order_id") if existing else "",
            "code": safe_getattr(raw_order, "stock_code", existing.get("code") if existing else ""),
            "direction": existing.get("direction") if existing else "",
            "volume": safe_int(safe_getattr(raw_order, "order_volume", existing.get("volume") if existing else 0)),
            "price": safe_float(safe_getattr(raw_order, "price", existing.get("price") if existing else 0)),
            "price_type": existing.get("price_type") if existing else 11,
            "status": new_status,
            "status_msg": status_to_text(new_status),
            "seq": existing.get("seq") if existing else 0,
            "mode": existing.get("mode") if existing else "dry-run",
            "terminal": existing.get("terminal") if existing else "SYS",
            # v2(审计H1):透传 order_remark/strategy_name/created_at,防 ON CONFLICT 覆写成""
            "order_remark": existing.get("order_remark", "") if existing else "",
            "strategy_name": existing.get("strategy_name", "") if existing else "",
            "created_at": existing.get("created_at") if existing else datetime.now(),
            "updated_at": datetime.now(),
            "finished_at": datetime.now() if new_status in ORDER_STATUS_TERMINAL else None,
        }
        # 终态同步落盘(H2)
        if new_status in ORDER_STATUS_TERMINAL:
            self.store.sync_terminal_write("order", update)
            # 释放清仓锁
            if self.clearance_lock:
                self.clearance_lock.release_by_order_id(order_id)
        else:
            self.store.buffer_order_update(update)

    def _is_valid_transition(self, old: int, new: int) -> bool:
        """状态转换合法性(§17.6)"""
        if new in ORDER_STATUS_TERMINAL and old in ORDER_STATUS_TERMINAL:
            return False  # 终态不能再变
        if old == 57 and new != 57:
            return False  # 废单不能变回
        if old == 56 and new != 56:
            return False  # 已成不能变
        return True

    def _handle_trade(self, raw_trade: Any) -> None:
        if not self.store:
            return
        from app.utils.xtquant_compat import safe_getattr, safe_int, safe_float
        trade_id = safe_getattr(raw_trade, "traded_id", 0)
        order_id = safe_getattr(raw_trade, "order_id", 0)
        code = safe_getattr(raw_trade, "stock_code", "")
        order_type = safe_int(safe_getattr(raw_trade, "order_type", 0))
        filled_volume = safe_int(safe_getattr(raw_trade, "traded_volume", 0))
        filled_price = safe_float(safe_getattr(raw_trade, "traded_price", 0))
        filled_amount = safe_float(safe_getattr(raw_trade, "traded_amount", 0))
        commission = safe_float(safe_getattr(raw_trade, "commission", 0))

        # 查 order 获取 mode(先按 QMT order_id,再按 seq 反查)
        order = self.store.get_order(order_id)
        if not order:
            seq = self._seq_map.get(str(order_id))
            if seq:
                order = self.store.get_order_by_seq(seq)
            if not order:
                order = self.store.get_order_by_seq(order_id)
        _rs_mode = self.runtime_state.mode if self.runtime_state else self.config.mode
        mode = order.get("mode", _rs_mode) if order else _rs_mode
        # v2(审计M1):显式判断方向,未知值不当卖出(否则 apply_sell_fill 误扣)
        # CRITICAL-2(2026-07-15):未知方向强抛,不允许 direction="unknown" 进入 DB——
        # 1 笔 unknown = 1 笔失联系持仓,必须 raise + kill switch + audit + 飞书告警,
        # 任一环节失败都不能吞掉。外层 on_stock_trade 的 try/except ValueError 隔离 raise,
        # 保证 xtquant executor 单线程串行不被一条坏回报卡死。
        if order_type == 23:
            direction = "buy"
        elif order_type == 24:
            direction = "sell"
        else:
            msg = (f"未知 order_type={order_type} trade_id={trade_id} "
                   f"code={code},拒绝落 deal 并激活 kill switch")
            logger.error(msg)
            # audit(失败吞,不传播)
            if self.audit:
                try:
                    self.audit.log("unknown_order_type", reason=msg, data={
                        "order_type": order_type,
                        "trade_id": trade_id,
                        "code": code,
                    })
                except Exception:
                    pass
            # 飞书告警(失败吞)
            if self.notify:
                try:
                    self.notify.send(
                        f"🚨 [CRITICAL-2] 实盘收到未知 order_type={order_type} "
                        f"trade_id={trade_id} code={code},已激活 kill_switch"
                    )
                except Exception:
                    pass
            # kill_switch(真方法是 activate(reason, source),不是 trigger)
            if self.kill_switch:
                try:
                    self.kill_switch.activate(
                        reason=f"unknown_order_type={order_type}",
                        source="callback_unknown_order_type",
                    )
                except Exception:
                    pass
            # 强抛:deal 不落 DB,后续 sync_terminal_write / apply_*_fill 不会执行;
            # 外层 on_stock_trade 的 try/except ValueError 接住,仅日志不传播。
            raise ValueError(msg)

        deal = {
            "trade_id": trade_id,
            "order_id": order_id,
            "code": code,
            "direction": direction,
            "filled_volume": filled_volume,
            "filled_price": filled_price,
            "filled_amount": filled_amount,
            "commission": commission,
            "mode": mode,
            "traded_at": datetime.now(),
        }
        # 终态成交同步落盘(H2)
        # H3(2026-07-14):sync_terminal_write 现在会抛(不再静默吞)。
        # 这里 catch + audit + 飞书告警,但不激活 kill_switch——
        # 单条 deal 写入失败是数据问题不是交易规则,杀进程会丢更多数据。
        # 已写 WAL(防 os._exit 丢),重启可补。运维看飞书手动处理。
        try:
            self.store.sync_terminal_write("deal", deal)
        except Exception as e:
            logger.error(f"[H3] 成交 deal 写入 DuckDB 失败(WAL 已写,可重启补救): trade_id={trade_id} code={code}: {e}", exc_info=True)
            if self.audit:
                try:
                    self.audit.log("db_write_failed", reason=str(e), data={
                        "kind": "deal", "trade_id": trade_id, "code": code,
                    })
                except Exception:
                    pass
            if self.notify:
                try:
                    self.notify.send(
                        f"⚠️ [H3] 实盘成交写入失败: code={code} trade_id={trade_id} "
                        f"WAL 已存,重启可补。错误:{e}"
                    )
                except Exception:
                    pass
            # 不 re-raise:不让单条 deal 失败拖垮整个 callback 链路
            # 但已审计+告警,运维可见

        # 内存缓存(§18.7)
        with self._deals_lock:
            if code not in self._deals_buffer:
                self._deals_buffer[code] = deque(maxlen=500)
            self._deals_buffer[code].append(deal)

        # C1:成交后释放在途预扣(买入)
        if direction == "buy" and order:
            client_order_id = order.get("client_order_id", "")
            # pending_buy_volume 释放由 risk_gate 在下单时冻结,这里通知释放
            # H1:传 trade_id 让 apply_buy_fill 幂等(防重复回报双扣持仓)
            self._release_pending_buy(code, filled_volume, filled_price=filled_price, trade_id=trade_id)

        # v2(F4/H1):卖出成交递减持仓,清仓则重置;trade_id 幂等防重复回报双扣
        # H3(2026-07-14):apply_sell_fill 现在会抛(不再静默吞)。同 deal 写入处理:
        # 审计+告警+不 re-raise,不让单条卖出回调拖垮整个链路。
        if direction == "sell":
            try:
                self.store.apply_sell_fill(code, filled_volume, trade_id=trade_id)
            except Exception as e:
                logger.error(f"[H3] 卖出 apply_sell_fill 失败: code={code} trade_id={trade_id}: {e}", exc_info=True)
                if self.audit:
                    try:
                        self.audit.log("db_write_failed", reason=str(e), data={
                            "kind": "sell_fill", "code": code, "trade_id": trade_id,
                        })
                    except Exception:
                        pass
                if self.notify:
                    try:
                        self.notify.send(
                            f"⚠️ [H3] 实盘卖出成交写入失败: code={code} trade_id={trade_id} 错误:{e}"
                        )
                    except Exception:
                        pass

        # 盈亏重算
        if self.pnl_engine and direction == "sell":
            self._dispatch_db(self.pnl_engine.recompute, code)

        # 释放清仓锁
        if self.clearance_lock:
            self.clearance_lock.release_by_order_id(order_id)

        # 通知(卖出时查持仓均价用于显示盈亏)
        if self.notify:
            avg_cost = 0
            tag = "其他"
            if direction == "sell" and self.store:
                pos = self.store.get_position(code)
                if pos:
                    avg_cost = float(pos.get("avg_cost", 0) or 0)
            if order:
                remark = order.get("order_remark", "")
                tag = self._TAG_MAP.get(remark, "其他")
            self.notify.order_traded_with_tag(
                code, direction, filled_volume, filled_price, mode, tag, avg_cost=avg_cost
            )

    def _release_pending_buy(self, code: str, filled_volume: int,
                             filled_price: float = 0.0,
                             trade_id: int = None) -> None:
        """C1:成交后释放在途预扣 + 首次建仓写 entry_date

        v2(审计H2/H3):改调 store.apply_buy_fill 原子SQL,避免全字段 upsert 覆盖
        tp_triggered/sell_count/peak_price,并补写 entry_date(修 hold_days 恒=1)。
        v3(2026-07-14 审计H1):传 trade_id 给 apply_buy_fill,防重复回报双扣持仓。
        v3(2026-07-14 审计H3):异常向上抛,不再吞。调用方 _on_deal_callback 需 try/except 接。
        v4(2026-07-19 code-review):透传 filled_price 给 apply_buy_fill(修 NameError:
            原引用未定义的 filled_price,买入成交回调崩、持仓不更新)。
        """
        if not self.store:
            return
        # H3: 异常向上抛(让 _on_deal_callback 接住 + 告警)
        self.store.apply_buy_fill(code, filled_volume, filled_price=filled_price, trade_id=trade_id)

    def _handle_order_error(self, order_id: int, error_id: int, error_msg: str) -> None:
        if not self.store:
            return
        # 标记废单(57)
        existing = self.store.get_order(order_id)
        if existing:
            update = dict(existing)
            update["status"] = 57
            update["status_msg"] = f"废单:{error_msg}"
            update["finished_at"] = datetime.now()
            self.store.sync_terminal_write("order", update)
            # 释放清仓锁
            if self.clearance_lock:
                self.clearance_lock.release_by_order_id(order_id)
            # C2(2026-07-15 全项目审计): 废单=零成交, 只释放 pending_buy_volume 冻结,
            # 绝不调 apply_buy_fill(会把废单股数加进 volume, 反向膨胀持仓)。
            if existing.get("direction") == "buy":
                self.store.release_pending_buy(existing.get("code", ""), existing.get("volume", 0))

        if self.notify:
            self.notify.order_error(order_id, error_msg)

    def _cleanup_seq_map(self) -> None:
        """终态订单从 seq_map 删除(M3)"""
        if not self.store:
            return
        with self._seq_map_lock:
            if len(self._seq_map) <= self._seq_map_max:
                return
            # 清理已终态的
            to_remove = []
            for oid_str in list(self._seq_map.keys()):
                try:
                    oid = int(oid_str)
                    order = self.store.get_order(oid)
                    if order and order.get("status") in ORDER_STATUS_TERMINAL:
                        to_remove.append(oid_str)
                except Exception:
                    to_remove.append(oid_str)
            for k in to_remove:
                self._seq_map.pop(k, None)

    # ===== 启动恢复回放(§17.1)=====

    def replay_order_update(self, order_id: int, new_status: int, qmt_order: Dict) -> None:
        """启动恢复:补写订单状态变化"""
        self._handle_order_update(order_id, new_status, qmt_order)

    # ===== mock 回报生成器(dry-run 模式)=====

    def mock_order_async_response(self, client_order_id: str, code: str, direction: str,
                                   volume: int, price: float, price_type: int,
                                   strategy_name: str, order_remark: str) -> int:
        """dry-run 模式:生成 mock order_id + 模拟 callback 全链路

        返回 mock order_id,并异步触发 on_stock_order(50)→on_stock_trade(56)回调,
        模拟真实时序(200-500ms 延迟)。
        """
        self._mock_counter += 1
        mock_order_id = self._mock_counter
        mock_seq = self._mock_counter + 500000

        # 记录 seq 映射
        with self._seq_map_lock:
            self._seq_map[str(mock_order_id)] = mock_seq

        mode = "dry-run"
        now = datetime.now()

        # 写初始订单(status=50 已报)
        order_data = {
            "order_id": mock_order_id,
            "client_order_id": client_order_id,
            "code": code,
            "direction": direction,
            "volume": volume,
            "price": price,
            "price_type": price_type,
            "status": 50,
            "status_msg": "已报(mock)",
            "seq": mock_seq,
            "mode": mode,
            "strategy_name": strategy_name,
            "order_remark": order_remark,
            "terminal": "SYS",
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        if self.store:
            self.store.sync_terminal_write("order", order_data)

        # 异步模拟 callback(200-500ms 后成交)
        delay = random.uniform(0.2, 0.5)
        self._db_executor.submit(self._mock_callback_chain, mock_order_id, code,
                                  direction, volume, price, delay)
        logger.info(f"[MOCK] 下单 oid={mock_order_id} code={code} {direction} {volume}@{price}, {delay:.2f}s 后成交")
        return mock_order_id

    def _mock_callback_chain(self, order_id: int, code: str, direction: str,
                              volume: int, price: float, delay: float) -> None:
        """模拟 callback 链:on_stock_order(50) → on_stock_trade(56)"""
        try:
            time.sleep(delay)
            # 1. on_stock_order status=56(已成)
            if self.store:
                order = self.store.get_order(order_id)
                if order:
                    update = dict(order)
                    update["status"] = 56
                    update["status_msg"] = "已成(mock)"
                    update["finished_at"] = datetime.now()
                    self.store.sync_terminal_write("order", update)
                    logger.info(f"[MOCK] on_stock_order oid={order_id} → 56 已成")

            # 2. on_stock_trade
            mock_trade_id = order_id + 900000
            trade = {
                "trade_id": mock_trade_id,
                "order_id": order_id,
                "code": code,
                "direction": direction,
                "filled_volume": volume,
                "filled_price": price,
                "filled_amount": price * volume,
                "commission": max(price * volume * 0.00025, 5.0),
                "mode": "dry-run",
                "traded_at": datetime.now(),
            }
            if self.store:
                self.store.sync_terminal_write("deal", trade)
            with self._deals_lock:
                if code not in self._deals_buffer:
                    self._deals_buffer[code] = deque(maxlen=500)
                self._deals_buffer[code].append(trade)

            # C1:买入成交释放预扣
            if direction == "buy":
                self._release_pending_buy(code, volume)

            # 释放清仓锁
            if self.clearance_lock:
                self.clearance_lock.release_by_order_id(order_id)

            # 盈亏重算
            if self.pnl_engine and direction == "sell":
                self._dispatch_db(self.pnl_engine.recompute, code)

            # 通知
            if self.notify:
                self.notify.order_traded(code, direction, volume, price, "dry-run")
            logger.info(f"[MOCK] on_stock_trade oid={order_id} code={code} {volume}@{price}")
        except Exception as e:
            logger.error(f"[MOCK] callback 链失败: {e}")

    def stop(self) -> None:
        try:
            self._db_executor.shutdown(wait=True)
        except Exception:
            self._db_executor.shutdown(wait=False)
