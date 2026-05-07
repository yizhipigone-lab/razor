"""
盘中实时监控器 — 内嵌于 sim_trader
始终监听 tick，开关控制执行/告警，模式控制盘中/尾盘时机。
"""
import math
import threading
from datetime import datetime, date
from typing import Optional, Dict

from core.logger import get_logger
from core.event_engine import event_engine, EVENT_TICK
from server.websocket.manager import sync_broadcast

log = get_logger("IntradayMonitor")


class IntradayMonitor:
    """盘中实时监控器，内嵌于 SimTraderEngine"""

    def __init__(self, engine: "SimTraderEngine"):
        self.engine = engine
        self._intraday_peak: Dict[str, float] = {}
        self._lock = threading.Lock()
        event_engine.register(EVENT_TICK, self._on_tick)
        log.info("盘中监控器已就绪")

    # ── Tick 驱动 ─────────────────────────────

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
        if self.engine.auto_sell:
            self._execute_sell(pos, price, reason, partial_qty)
        else:
            sync_broadcast({
                "type": "risk_alert",
                "code": code,
                "price": round(price, 2),
                "reason": reason,
                "mode": "卖出开关关闭",
            })
            log.info(f"[风险告警] {code} {reason}（卖出开关关闭，不执行）")

    # ── 风控优先级链 ──────────────────────────

    def _check_position(self, pos, current_price: float, session_peak: float):
        """
        与 engine.check_stops 保持一致的优先级：
        1. TP2 +14% → 全清
        2. TP1 +4% → 卖 20%
        3. 移动止盈：峰值 ≥ +8% 且回撤 ≥ 2%
        4. 硬止损 -5.5%
        返回 (reason, partial_qty) 或 None
        """
        from app.sim_trader.config import (
            HARD_STOP, TP1_PCT, TP1_SELL_RATIO, TP2_PCT,
            TRAIL_ACTIVATE, TRAIL_DD,
        )

        entry = pos.entry_price
        current_pct = current_price / entry - 1

        # 1. TP2
        if not pos.tp2_triggered and current_pct >= TP2_PCT:
            return (f"TP2 +14%({current_pct*100:.1f}%)", None)

        # 2. TP1
        if not pos.tp1_triggered and current_pct >= TP1_PCT:
            ss = int(pos.remaining_shares * TP1_SELL_RATIO / 100) * 100
            if ss >= 100:
                return (f"TP1 +4%({current_pct*100:.1f}%)", ss)

        # 3. 移动止盈
        overall_peak = max(pos.peak_price, session_peak)
        peak_pct = overall_peak / entry - 1
        if peak_pct >= TRAIL_ACTIVATE:
            dd = current_price / overall_peak - 1
            if dd <= -TRAIL_DD:
                return (f"移动止盈(峰{peak_pct*100:.1f}%回{dd*100:.1f}%)", None)

        # 4. 硬止损
        if current_pct <= HARD_STOP:
            return (f"硬止损({current_pct*100:.1f}%)", None)

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
            # 更新连败计数
            if trade.return_pct <= 0:
                self.engine.consecutive_losses += 1
            else:
                self.engine.consecutive_losses = 0
                self.engine.pause_until = None
            if self.engine.consecutive_losses >= 5:
                from datetime import timedelta
                self.engine.pause_until = date.today() + timedelta(days=3)

            self.engine.trades.append(trade)
            # 清理已平仓
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

    # ── 全量扫描兜底（无 QMT 时用 TDX/腾讯行情） ──

    def run_full_scan(self):
        """全量扫描：拉取行情 → 遍历持仓 → 风控判断"""
        active = self.engine.active_positions()
        if not active:
            return

        # 用多源行情拉取
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

    # ── 辅助 ──────────────────────────────────

    @staticmethod
    def _in_trading_hours() -> bool:
        now = datetime.now()
        minutes = now.hour * 60 + now.minute
        return 9 * 60 + 25 <= minutes <= 15 * 60
