"""离场监控(v5.4 §5.7 / §7.3)

复用 sim_trader/intraday_monitor.py 的 tick 驱动 + exit_rules(复用度 92%)。
差异:(a)真实下单替换模拟扣减 (b)清仓锁 (c)跌停跳过 C2 (d)时点约束(14:50/14:55/14:57)
"""
import time
from datetime import datetime, date
from typing import Dict, List, Optional

from core.logger import get_logger

from .config import LiveTraderConfig

logger = get_logger("live_trader.exit_monitor")


class ExitMonitor:
    """离场监控(复用 exit_rules,定时 + tick 双驱动)"""

    def __init__(self, config: LiveTraderConfig, store=None, qmt_wrapper=None,
                 risk_gate=None, clearance_lock=None, kill_switch=None,
                 callback_handler=None, audit=None, pnl_engine=None, runtime_state=None):
        self.config = config
        self.store = store
        self.qmt = qmt_wrapper
        self.risk_gate = risk_gate
        self.clearance_lock = clearance_lock
        self.kill_switch = kill_switch
        self.callback_handler = callback_handler
        self.audit = audit
        self.pnl_engine = pnl_engine
        self.runtime_state = runtime_state  # v2(A6): 运行时 mode/开关

    def scan_once(self) -> List[Dict]:
        """扫描一次持仓,返回触发的卖出动作"""
        if self.kill_switch and self.kill_switch.is_active():
            logger.info("kill switch 激活,跳过离场扫描")
            return []

        if not self.qmt or not self.qmt.connected:
            logger.warning("QMT 未连接,跳过离场扫描")
            return []

        # 拉持仓 + 行情
        positions = self.qmt.query_positions()
        if not positions:
            return []

        actions = []
        for pos in positions:
            code = pos.get("code", "")
            # §3.3.1 ETF 保留持仓跳过(managed=false)
            local_pos = self.store.get_position(code) if self.store else None
            if local_pos and not local_pos.get("managed", True):
                continue  # 保留持仓(ETF)不触发 exit_rules
            # v2(审计M4):本地无持仓行 → 未知持仓,不默认管(防误卖 ETF/手动仓)
            if local_pos is None:
                logger.warning(f"{code} 本地无持仓记录(QMT有本地无),跳过 exit_rules(需人工接管)")
                continue

            # 构造 exit_rules 的 RuleContext
            ctx = self._build_context(pos, local_pos)
            if ctx is None:
                continue

            # 复用 exit_rules.check (v2 A2: signal 字段映射修正)
            from app.backtest.exit_rules import exit_rule_engine
            signal = exit_rule_engine.check(ctx, skip_eod_only=True)
            if signal:
                actions.append({
                    "code": code,
                    "trigger": signal.reason,                      # v2: ExitSignal 只有 reason
                    "sell_pct": signal.sell_ratio * 100,           # v2: sell_ratio(0~1) → pct
                    "priority": self._reason_priority(signal.reason),
                    "note": signal.reason,                         # 供 OrderIntent.reason 留痕
                    "pos": pos,
                })

        # 按优先级排序
        actions.sort(key=lambda x: x.get("priority", 0), reverse=True)

        # v2(F1): 单次扫描卖出数量上限,防首扫集中抛售(踩踏)
        MAX_SELL_PER_SCAN = 3
        if len(actions) > MAX_SELL_PER_SCAN:
            skipped = [a["code"] for a in actions[MAX_SELL_PER_SCAN:]]
            logger.warning(f"单次扫描卖出上限 {MAX_SELL_PER_SCAN},本次跳过(下次扫描处理): {skipped}")
            actions = actions[:MAX_SELL_PER_SCAN]

        # 执行
        executed = []
        for action in actions:
            try:
                self._execute_sell(action)
                executed.append(action)
            except Exception as e:
                logger.error(f"执行卖出失败 {action['code']}: {e}")

        return executed

    def _build_context(self, qmt_pos: Dict, local_pos: Optional[Dict]):
        """v2: 改用 exit_rule_engine.build_context(对齐模拟盘),修复构造失败(A1)

        修复点:
        - RuleContext(pos=,bar=) → build_context(pos_obj, bar, hold_days, params)
        - tp_triggered 从 DB 读(A3),非 sell_count 推断
        - bar low 用当日真实值(U1: HS 用真实 low)
        - 参数从 risk 段读(决策2)
        """
        try:
            from app.backtest.exit_rules import exit_rule_engine
            import json as _json
            code = qmt_pos.get("code", "")
            volume = int(qmt_pos.get("volume", 0))
            if volume <= 0:
                return None

            avg_cost = float(qmt_pos.get("avg_cost", 0) or (local_pos or {}).get("avg_cost", 0))
            last_price = float(qmt_pos.get("last_price", 0))

            # 拉行情补 last_price + 当日 high/low(U1: HS 用真实 low)
            today_high = last_price
            today_low = last_price
            if self.qmt:
                quotes = self.qmt.get_realtime_quotes([code])
                q = quotes.get(code, {}) if quotes else {}
                if q:
                    if last_price <= 0:
                        last_price = float(q.get("lastPrice", 0) or 0)
                    today_high = float(q.get("high", 0) or last_price)
                    today_low = float(q.get("low", 0) or last_price)

            # v2(审计M5): avg_cost/last_price 缺失则规则除零被 engine 静默吞,持仓永不退出 → 显式告警跳过
            if avg_cost <= 0 or last_price <= 0:
                logger.warning(f"{code} avg_cost={avg_cost} last_price={last_price} 数据缺失,跳过(防除零)")
                if self.audit:
                    self.audit.log("skip_no_data", code=code, reason="avg_cost/last_price<=0")
                return None

            # peak_price 从本地持仓取,缺失用 last_price 兜底(v2 F2: 非纯 avg_cost)
            peak_price = float((local_pos or {}).get("peak_price", 0) or last_price or avg_cost)

            # hold_days: 交易日计数(A10),entry_date 缺失用今日填充(T4)
            entry_date = (local_pos or {}).get("entry_date") or date.today()
            hold_days = self._calc_hold_days(entry_date)

            # tp_triggered: 从 DB 读(A3),JSON set;不用 sell_count 推断
            tp_trig_raw = (local_pos or {}).get("tp_triggered", "[]") or "[]"
            try:
                tp_triggered = set(int(x) for x in _json.loads(tp_trig_raw))  # 强制 int(审计M6)
            except Exception:
                tp_triggered = set()

            # pos_obj(duck-type): build_context 读 entry_price/peak_price/shares/tp_triggered
            pos_obj = type("Pos", (), {
                "entry_price": avg_cost,
                "peak_price": peak_price,
                "shares": volume,
                "remaining_shares": volume,
                "tp_triggered": tp_triggered,
            })()

            # bar: HS 用当日真实 low,TP/TR 用 close(对齐模拟盘)(U1)
            bar = {
                "close": last_price,
                "high": today_high,
                "low": today_low,
                "open": last_price,
                "atr": 0,
            }

            params = self._load_risk_params()
            ctx = exit_rule_engine.build_context(pos_obj, bar, hold_days, params)
            return ctx
        except Exception as e:
            logger.error(f"构造 RuleContext 失败 {qmt_pos.get('code','')}: {e}")
            return None

    def _reason_priority(self, reason: str) -> int:
        """v2: 从 signal.reason 映射优先级

        单次扫描≤3 上限下,HS(全卖止损)优先于 TP(部分卖止盈),防快跌时 HS 被饿死(审计M3)。
        """
        if reason.startswith("HS"):
            return 120   # 硬止损(全卖,最紧急)
        if reason.startswith("TF"):
            return 115   # 强制时间退出(全卖)
        if reason.startswith("TP"):
            return 110   # 止盈(部分卖)
        if reason.startswith("TR"):
            return 105   # 移动止盈(部分卖)
        if reason.startswith("FD"):
            return 90    # 首日弱势离场
        if reason.startswith("TC"):
            return 20    # 时间条件退出
        return 0

    def _calc_hold_days(self, entry_date) -> int:
        """v2(A10/T4): 交易日计数(对齐模拟盘 intraday_monitor);entry_date 缺失用今日填充"""
        try:
            if entry_date is None:
                entry_date = date.today()
            if hasattr(entry_date, "date"):
                entry_d = entry_date.date()
            else:
                entry_d = entry_date
            from app.api.sim_trader import _load_trading_calendar
            cal = _load_trading_calendar() or set()
            today = date.today()
            if cal:
                window = sorted(d for d in cal if entry_d <= d <= today)
                return max(1, len(window))
            return max(1, (today - entry_d).days)  # fallback: 自然日
        except Exception:
            return 1

    def _load_risk_params(self) -> dict:
        """v5.5(2026-07-14):统一走 risk_params.load_risk_params,与 sim_trader 共享,杜绝 4 套默认值漂移"""
        from app.config.risk_params import load_risk_params as _load_risk_params
        import dataclasses
        return dataclasses.asdict(_load_risk_params())


    def _execute_sell(self, action: Dict) -> None:
        """执行卖出(真实下单 + 清仓锁 + 跌停判断)"""
        code = action["code"]
        pos = action["pos"]
        sell_pct = action.get("sell_pct", 100)

        # 时点判断(§7.3)
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        force_market = current_time >= self.config.force_market_after
        no_new_order = current_time >= self.config.no_new_order_after

        if no_new_order and not force_market:
            logger.info(f"{code} 已过 {self.config.no_new_order_after},只撤不挂")
            return

        # C2:跌停/停牌/一字板判断
        quotes = self.qmt.get_realtime_quotes([code])
        quote = quotes.get(code, {})
        last_price = quote.get("lastPrice", 0)
        prev_close = quote.get("lastClose", 0)

        if last_price > 0 and prev_close > 0:
            from app.backtest.execution import is_limit_down, is_suspended_or_locked
            today_high = quote.get("high", 0)
            today_low = quote.get("low", 0)
            today_open = quote.get("open", 0)
            if is_limit_down(code, last_price, prev_close):
                logger.warning(f"{code} 已跌停,市价清仓无意义,跳过(明日开盘处理)")
                if self.audit:
                    self.audit.log("limit_down_skip", code=code,
                                   reason=f"跌停无法止损 last={last_price} prev={prev_close}")
                if self.callback_handler and self.callback_handler.notify:
                    self.callback_handler.notify.reconcile_diff(
                        code, 0, 0, "WARN:跌停无法止损,明日处理"
                    )
                return
            if is_suspended_or_locked(code, last_price, prev_close, today_open, today_high, today_low):
                logger.warning(f"{code} 停牌/一字板,跳过强平")
                if self.audit:
                    self.audit.log("suspended_skip", code=code, reason="停牌/一字板")
                return

        # 计算卖出股数(板块取整)
        volume = int(pos.get("volume", 0))
        can_use = int(pos.get("can_use_volume", 0))
        sell_volume = self._calc_sell_volume(code, can_use, sell_pct)
        if sell_volume <= 0:
            return

        # 候选③:卖单下单委托给 OrderExecutor
        # cancel_inflight=True(撤在途)+risk_positions_only=True(只查 positions)/
        # persist_live_orders=False(保留旧不写表行为)+on_order_submitted=TP 标记
        import hashlib
        client_order_id = hashlib.md5(
            f"exit|{code}|{date.today()}|{int(time.time())}".encode()
        ).hexdigest()[:16]

        # 价格类型
        if force_market:
            # 14:57 后强制市价(未跌停才到这里)
            from app.utils.xtquant_compat import PRICE_TYPE_PEER_FIRST
            price_type = PRICE_TYPE_PEER_FIRST
            price = 0.0
        else:
            from app.utils.xtquant_compat import PRICE_TYPE_FIX
            price_type = PRICE_TYPE_FIX
            price = last_price if last_price > 0 else float(pos.get("avg_cost", 0))

        from .schemas import OrderIntent
        intent = OrderIntent(
            code=code, direction="sell", volume=sell_volume,
            price=price, price_type=price_type,
            strategy_name="exit_monitor", terminal="SYS",
            client_order_id=client_order_id,
            reason=action.get("note", ""),
        )

        def _tp_mark(order_id, _intent):
            """v2(A3/H2):TP 档位乐观标记 — 仅下单成功且 trigger=TP 才标,
            防废单误标永不重试。委托 OrderExecutor.on_order_submitted 触发。
            """
            trigger = action.get("trigger", "")
            if not trigger.startswith("TP") or not self.store:
                return
            try:
                idx = int(trigger[2:]) - 1  # TP1→0, TP2→1
                import json as _json
                _pos = self.store.get_position(code)
                _existing = set()
                if _pos and _pos.get("tp_triggered"):
                    _existing = set(int(x) for x in _json.loads(_pos["tp_triggered"]))
                _existing.add(idx)  # int(非str),exit_rules 用 int idx 检查 triggered_tiers
                self.store.update_tp_triggered(code, _json.dumps(sorted(_existing, key=int)))
            except Exception as _e:
                logger.error(f"update_tp_triggered 失败 {code}: {_e}")

        try:
            order_executor = getattr(self, "order_executor", None)
            if order_executor is None:
                # 兜底:未注入 OrderExecutor 时报需启动(单测/未走 lifespan)
                logger.error(f"{code} order_executor 未注入,跳过卖出")
                return
            result = order_executor.execute(
                intent, source="EXIT", lock_wait_sec=0,
                cancel_inflight=True,
                risk_positions_only=True,
                persist_live_orders=False,
                on_order_submitted=_tp_mark,
            )
            ok = result.get("ok")
            status = result.get("status")
            oid = result.get("order_id")
            logger.info(
                f"卖出下单 {code} {sell_volume}股@{price} pt={price_type}"
                f" ok={ok} status={status} oid={oid}"
            )
            if status == "locked":
                logger.info(f"{code} 清仓锁占用,跳过")
                return
            if status == "risk_rejected":
                logger.info(f"{code} 卖出被风控拒绝: {result.get('reason')}")
                return
        except Exception as e:
            logger.error(f"卖出执行异常 {code}: {e}")
            raise

    def _calc_sell_volume(self, code: str, can_use: int, sell_pct: float) -> int:
        """计算卖出股数(板块取整)"""
        if can_use <= 0:
            return 0
        if sell_pct >= 100:
            return can_use
        raw = can_use * sell_pct / 100
        # 板块取整(主板100,科创/北交所可1股,简化:主板按100)
        if code.startswith(("688", "8", "4", "920")):
            return int(raw)  # 科创/北交所可1股
        vol = int(raw // 100) * 100  # 主板按100
        if vol <= 0:
            # v2(审计L6):部分卖取整为0(仓位不足100股),告警,下次扫描重试
            logger.warning(f"{code} 部分卖 sell_pct={sell_pct:.0f}% can_use={can_use} 取整为0,跳过(仓位不足100股)")
        return vol
