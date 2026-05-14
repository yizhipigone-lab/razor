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

    def _check_and_act(self, code: str, price: float):
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
        优先级：HS → TF → 多档止盈 → TR → TC
        返回 (reason, partial_qty) 或 None
        """
        from app.sim_trader.config import (
            HARD_STOP, TAKE_PROFIT_TIERS,
            TRAIL_ACTIVATE, TRAIL_DD,
            TIME_EXIT_DAYS, TIME_EXIT_PROFIT, TIME_FORCE_DAYS,
            USE_ATR_TRAIL, ATR_TRAIL_MULTIPLIER,
        )

        entry = pos.entry_price
        current_pct = current_price / entry - 1

        # 1. 硬止损 HS: -6.0%
        if current_pct <= HARD_STOP:
            return (f"HS({current_pct*100:.1f}%)", None)

        # 2. 时间强制 TF: 持仓 > 9天
        hold_days = (date.today() - pos.entry_date).days
        if hold_days > TIME_FORCE_DAYS:
            return (f"TF({hold_days}天)", None)

        # 3. 多档阶梯止盈
        for idx, tier in enumerate(TAKE_PROFIT_TIERS):
            if not pos.is_tier_triggered(idx) and current_pct >= tier['profit_pct']:
                ss = int(pos.remaining_shares * tier['sell_ratio'] / 100) * 100
                if ss >= 100:
                    pos.mark_tier_triggered(idx)
                    return (f"TP{idx+1}({current_pct*100:.1f}%)", ss)

        # 4. 移动止盈 TR: 峰≥+3% 且 回撤≥1%（支持 ATR 动态回撤）
        overall_peak = max(pos.peak_price, session_peak)
        peak_pct = overall_peak / entry - 1
        if peak_pct >= TRAIL_ACTIVATE:
            dd = current_price / overall_peak - 1
            eff_trail_dd = TRAIL_DD
            if USE_ATR_TRAIL and daily_atr > 0:
                atr_pct = ATR_TRAIL_MULTIPLIER * daily_atr / entry
                eff_trail_dd = max(TRAIL_DD, atr_pct)
            if dd <= -eff_trail_dd:
                return (f"TR(峰{peak_pct*100:.1f}%回{dd*100:.1f}%)", None)

        # 5. 时间条件 TC: >3天 且 >3%
        if hold_days > TIME_EXIT_DAYS and current_pct > TIME_EXIT_PROFIT:
            return (f"TC({hold_days}天+{current_pct*100:.1f}%)", None)

        return None

    def _execute_sell(self, pos, price: float, reason: str, partial_qty: Optional[int]):
        """执行卖出并广播"""
        with self._lock:
            trade = self.engine.execute_sell(
                pos, price, reason, partial_qty,
                exit_date=date.today(),
                exit_timing="intraday",
            )
        if trade:
            # 连败计数
            if trade.return_pct <= 0:
                self.engine.consecutive_losses += 1
            else:
                self.engine.consecutive_losses = 0
                self.engine.pause_until = None
            from app.sim_trader.config import LOSS_STREAK_PAUSE, PAUSE_DAYS
            if self.engine.consecutive_losses >= LOSS_STREAK_PAUSE:
                self.engine.pause_until = date.today() + timedelta(days=PAUSE_DAYS)

            self.engine.trades.append(trade)
            self.engine.positions = {
                k: v for k, v in self.engine.positions.items() if v.is_active
            }

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
            self._check_and_act(pos.code, price)

    # ── 辅助 ────────────────────────────────────

    @staticmethod
    def _in_trading_hours() -> bool:
        now = datetime.now()
        minutes = now.hour * 60 + now.minute
        return 9 * 60 + 25 <= minutes <= 15 * 60
