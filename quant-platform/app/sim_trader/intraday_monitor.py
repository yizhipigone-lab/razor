"""
盘中实时监控器 — 内嵌于 sim_trader
优先级链与回测 simple_runner 完全一致：HS → TF → TP2 → TP1 → TR → TC
模式: intraday(触发即卖) | close(仅告警)
"""
import threading
from datetime import datetime, date, timedelta
from typing import Optional, Dict

from core.logger import get_logger
from core.event_engine import event_engine, EVENT_TICK
from server.websocket.manager import sync_broadcast

log = get_logger("IntradayMonitor")


class IntradayMonitor:
    """盘中实时监控器，内嵌于 SimTraderEngine"""

    def __init__(self, engine: "SimTraderEngine"):
        self.engine = engine
        self.enabled = False
        self.mode = "close"          # "intraday"（触发即卖）| "close"（仅告警）
        self._intraday_peak: Dict[str, float] = {}
        self._intraday_low: Dict[str, float] = {}   # 对称跟踪盘中最低(HS/TR 用真实 low)
        self._lock = threading.Lock()
        self._tick_handler = None
        log.info("盘中监控器已创建（待启动）")

    # ── 开关控制 ────────────────────────────────

    def start(self):
        if self.enabled:
            return
        self._tick_handler = self._on_tick
        event_engine.register(EVENT_TICK, self._tick_handler)
        self.enabled = True
        log.info(f"盘中监控已启动，模式={self.mode}")

    def stop(self):
        if not self.enabled:
            return
        if self._tick_handler:
            event_engine.unregister(EVENT_TICK, self._tick_handler)
            self._tick_handler = None
        self.enabled = False
        log.info("盘中监控已停止")

    # ── Tick 驱动 ───────────────────────────────

    def _on_tick(self, event):
        if not self._in_trading_hours():
            return

        tick = event.data if isinstance(event.data, dict) else {}
        code = tick.get("code", "")
        price = tick.get("price", tick.get("lastPrice", tick.get("now", 0)))
        if not code or not price:
            # 批量 tick 推送（quotes_push_webhook）
            quotes = tick.get("quotes", {})
            if quotes:
                for c, q in quotes.items():
                    p = q.get("price", q.get("lastPrice", q.get("now", 0)))
                    if p:
                        self._check_and_act(c, float(p))
            return

        self._check_and_act(code, float(price))

    @staticmethod
    def _is_market_hours() -> bool:
        """盘中监控仅在 9:25-15:00 之间生效"""
        from datetime import datetime
        now = datetime.now()
        t = now.hour * 100 + now.minute
        return 925 <= t <= 1500

    def _check_and_act(self, code: str, price: float):
        if not self._is_market_hours():
            return
        pos = self.engine.positions.get(code)
        if not pos or not pos.is_active or pos.remaining_shares <= 0:
            return

        # 更新盘中峰值 + 盘中最低(对称, HS/TR 用真实 low 而非当前 tick)
        prev_peak = self._intraday_peak.get(code, pos.peak_price)
        if price > prev_peak:
            self._intraday_peak[code] = price
        session_peak = max(prev_peak, price)

        prev_low = self._intraday_low.get(code)
        if prev_low is None or price < prev_low:
            self._intraday_low[code] = price
        session_low = self._intraday_low[code]

        result = self._check_position(pos, price, session_peak, session_low=session_low)
        if not result:
            return

        reason, partial_qty = result
        if self.engine.auto_sell and self.mode == "intraday":
            # 仅在确认卖出时才标记 TP 档位(避免告警模式烧档位导致 EOD 漏卖, HIGH-1 修复)
            if reason.startswith('TP'):
                idx = int(reason[2]) - 1
                pos.mark_tier_triggered(idx)
            self._execute_sell(pos, price, reason, partial_qty)
            # 卖出后更新一次净值快照（同一天只记一次，防重复）
            if not getattr(self, '_recorded_today', False):
                try:
                    from datetime import date as _d
                    if not hasattr(self, '_last_record_date') or self._last_record_date != _d.today():
                        # 根源修复: 用 QMT 实时行情构建今日 snapshot(而非昨日快照),
                        # 避免净值按昨收/买入价估值导致失真(2026-07盘中record虚高根因)。
                        # QMT 不可用时回退昨日快照, total_equity 再用 current_price 兜底。
                        snap = self.engine.build_live_snapshot() \
                               or self.engine._prev_day_snap or self.engine._prev_snap or {}
                        self.engine.record(_d.today(), snap)
                        self._last_record_date = _d.today()
                except Exception:
                    pass
        else:
            sync_broadcast({
                "type": "risk_alert",
                "code": code,
                "price": round(price, 2),
                "reason": reason,
                "mode": "尾盘监控" if self.mode == "close" else "卖出开关关闭",
            })
            log.info(f"[风险告警] {code} {reason}（模式={self.mode}）")

    # ── 风控优先级链（与回测 simple_runner 完全一致）──

    def _check_position(self, pos, current_price: float, session_peak: float,
                        daily_atr: float = 0.0, session_low: Optional[float] = None):
        """
        用统一规则引擎检查止盈止损，返回 (reason, partial_qty) 或 None。

        纯检查函数, 不修改 pos 状态(TP 档位由 _check_and_act 在确认卖出时标记,
        避免告警模式烧档位导致 EOD 漏卖, HIGH-1)。

        2026-07-14 修复 NameError: v5.5 重构后遗留 ctx/overall_peak 未定义,
        盘中 tick 命中持仓必崩。现对齐 live_trader.exit_monitor._build_context
        + engine.check_stops 的正确范式:
          build_context(pos, bar, hold_days, params) → check(ctx, skip_eod_only=True)。

        峰值处理: 不修改 pos.peak_price(对齐 master 盘中从不持久化峰值的行为,
        EOD check_stops 会用当日 high 更新), 而是构建 ctx 后覆盖 ctx.peak_price
        = max(历史峰值, session_peak), 让盘中检查用真实峰值 → EOD 行为零改变。

        bar.low: 用 session_low(盘中真实最低, 由 _check_and_act 对称跟踪),
        而非当前 tick 价——避免 HS/TR 漏掉盘中早先触及止损/回撤线的瞬时低点(HIGH-2)。
        """
        from app.backtest.exit_rules import exit_rule_engine
        from app.config.risk_params import load_risk_params as _load_risk_params
        import dataclasses

        sim_params = dataclasses.asdict(_load_risk_params())

        # 盘中 bar: close/open 用现价, high 用 session_peak, low 用 session_low(真实盘中最低)
        low_px = session_low if session_low is not None else current_price
        bar = {
            'open': current_price,
            'high': session_peak,
            'low': low_px,
            'close': current_price,
            'atr': daily_atr,
        }

        # hold_days: 交易日计数(对齐 exit_monitor._calc_hold_days)
        hold_days = self._calc_hold_days(pos.entry_date)

        ctx = exit_rule_engine.build_context(
            pos, bar, hold_days, sim_params, use_high_for_tp=True
        )
        # 盘中峰值覆盖(不修改 pos): 用 max(历史, session_peak) 作检查峰值
        if session_peak > ctx.peak_price:
            ctx.peak_price = session_peak

        signal = exit_rule_engine.check(ctx, skip_eod_only=True)
        if signal is None:
            return None

        if signal.reason.startswith('TP'):
            # 不在此标记档位(由 _check_and_act 卖出时标记), 只算部分卖股数
            ss = int(pos.remaining_shares * signal.sell_ratio / 100) * 100
            if ss >= 100:
                return (signal.reason, ss)
            return None

        return (signal.reason, None)

    def _calc_hold_days(self, entry_date) -> int:
        """交易日计数(对齐 live_trader.exit_monitor._calc_hold_days)。
        entry_date 缺失用今日; 无交易日历则自然日兜底; 异常返回 1(不阻断风控)。"""
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
            return max(1, (today - entry_d).days)
        except Exception:
            return 1

    def _execute_sell(self, pos, price: float, reason: str, partial_qty: Optional[int]):
        """执行卖出并广播"""
        with self._lock:
            trade = self.engine.execute_sell(
                pos, price, reason, partial_qty,
                exit_date=date.today(),
                exit_timing="intraday",
            )
        if trade:
            # 不再 append:self.engine.execute_sell 已写 DB
            self.engine.positions = {
                k: v for k, v in self.engine.positions.items() if v.is_active
            }

            # 真实券商委托路径已删除(2026-07-14):见 engine.py 同注释

            sync_broadcast({
                "type": "sim_trader_update",
                "today": str(date.today()),
                "buy_count": 0,
                "sell_count": 1,
                "equity": round(self.engine.cash, 2),
                "cash": round(self.engine.cash, 2),
                "positions": self.engine.position_count,
                "intraday_sell": True,
                "code": pos.code,
                "reason": reason,
            })
            log.info(f"[盘中卖出] {pos.code} {reason} 价格={price:.2f}")

    # ── 全量扫描兜底（无 QMT 时用 TDX）──

    def run_full_scan(self):
        """全量扫描：拉取行情 → 遍历持仓 → 风控判断"""
        active = self.engine.active_positions()
        if not active:
            return

        from app.data_manager.engine import get_realtime_quote
        import pandas as pd

        codes = [p.code for p in active]
        try:
            quotes = get_realtime_quote(codes)
        except Exception as e:
            log.warning(f"全量扫描获取行情失败: {e}")
            return

        if quotes is None or quotes.empty:
            return

        for pos in active:
            q = quotes[quotes["code"] == pos.code]
            if q.empty:
                continue
            price = float(q.iloc[0]["price"])
            # 委托 quote_source 后,缺价 code 会得到 source='missing' 行(price=NaN)。
            # 跳过缺价 pos(保留旧行为:未取到价不监控),防 NaN 传入 _check_and_act。
            if not (price > 0):
                continue
            self._check_and_act(pos.code, price)

    # ── 辅助 ────────────────────────────────────

    @staticmethod
    def _in_trading_hours() -> bool:
        now = datetime.now()
        minutes = now.hour * 60 + now.minute
        return 9 * 60 + 25 <= minutes <= 15 * 60
