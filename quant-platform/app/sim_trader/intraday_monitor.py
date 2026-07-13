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

        # 更新盘中峰值
        prev_peak = self._intraday_peak.get(code, pos.peak_price)
        if price > prev_peak:
            self._intraday_peak[code] = price
        session_peak = max(prev_peak, price)

        result = self._check_position(pos, price, session_peak)
        if not result:
            return

        reason, partial_qty = result
        if self.engine.auto_sell and self.mode == "intraday":
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
                        daily_atr: float = 0.0):
        """
        用统一规则引擎检查止盈止损，返回 (reason, partial_qty) 或 None
        """
        from app.backtest.exit_rules import exit_rule_engine
        from core.settings import settings

        # 从持久化配置读取（重启不丢失），config.py 只在首次启动时作为兜底
        def _cfg(key, default):
            val = settings.get("risk", key)
            if val is None:
                # 兜底：读 config.py
                import app.sim_trader.config as sc
                return getattr(sc, key.upper(), default)
            return val

        overall_peak = max(pos.peak_price, session_peak)
        # 与 sim_trader.engine.check_stops 公式保持一致（#13 修复）
        # 用交易日计数（自然日会受周末/假期干扰，触发时机与尾盘不同步）
        from app.api.sim_trader import _load_trading_calendar
        _cal = _load_trading_calendar() or set()
        _today = date.today()
        trading_dates_window = sorted(d for d in _cal if pos.entry_date <= d <= _today)
        hold_days = max(1, len(trading_dates_window))  # 至少 1,防空日历误触

        ctx = exit_rule_engine.build_context(
            pos,
            {"close": current_price, "high": current_price, "low": current_price, "open": current_price, "atr": daily_atr},
            hold_days,
            {
                "hard_stop": _cfg("hard_stop_loss_pct", -6.0) / 100.0,  # settings用百分比→转小数
                "take_profit_tiers": _cfg("take_profit_tiers", [{"profit_pct": 0.03, "sell_ratio": 0.30}]),
                "trail_activate": _cfg("trailing_stop_activate_pct", 5.0) / 100.0,
                "trail_dd": _cfg("trailing_stop_drawdown_pct", 2.0) / 100.0,
                "time_exit_days": _cfg("time_exit_days", 7),
                "time_exit_profit": _cfg("time_exit_min_profit_pct", 3.0) / 100.0,
                "time_force_days": _cfg("time_exit_force_days", 12),
                "first_day_exit_min_profit": _cfg("first_day_exit_min_profit", 0.0),
                "first_day_exit_days": _cfg("first_day_exit_days", 1),
                "use_atr_trail": _cfg("use_atr_stop", False),
                "atr_trail_multiplier": _cfg("atr_stop_multiplier", 1.0),
            },
        )
        # 覆盖峰值：盘中使用 session_peak
        ctx.peak_price = overall_peak

        signal = exit_rule_engine.check(ctx, skip_eod_only=True)
        if signal is None:
            return None

        if signal.reason.startswith('TP'):
            idx = int(signal.reason[2]) - 1
            pos.mark_tier_triggered(idx)
            ss = int(pos.remaining_shares * signal.sell_ratio / 100) * 100
            if ss >= 100:
                return (signal.reason, ss)
            return None

        return (signal.reason, None)

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

            # 真实券商委托
            from app.sim_trader.config import BROKER_ENABLED
            if BROKER_ENABLED:
                try:
                    from core.gateway import get_gateway
                    gw = get_gateway()
                    gw.sell(code=pos.code, price=price,
                            volume=trade.shares, reason=reason)
                    log.info(f"券商委托: {pos.code} 卖出 {trade.shares}股 @ {price:.2f} [{reason}]")
                except Exception as e:
                    log.error(f"券商委托失败 {pos.code}: {e}")

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
