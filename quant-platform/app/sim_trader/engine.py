"""
模拟盘交易 — 核心引擎
每日流程:
  14:52 — 止盈止损卖出（先回收现金）
  14:54 — 选股买入
  14:56 — 记录净值
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pandas as pd
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from collections import Counter

from app.sim_trader.config import *


@dataclass
class Position:
    code: str
    entry_date: date
    entry_price: float
    shares: int
    cost: float
    peak_price: float = 0.0
    remaining_shares: int = 0
    tp1_triggered: bool = False
    tp2_triggered: bool = False
    is_active: bool = True
    strategy_name: str = ""

    def __post_init__(self):
        self.peak_price = self.entry_price
        self.remaining_shares = self.shares

    @property
    def market_value(self) -> float:
        return self.remaining_shares * self.entry_price  # 用成本价，由外部更新

    @property
    def profit_pct(self, current_price: float) -> float:
        return (current_price / self.entry_price - 1) * 100

    def is_tier_triggered(self, idx: int) -> bool:
        return self.tp1_triggered if idx == 0 else self.tp2_triggered

    def mark_tier_triggered(self, idx: int):
        if idx == 0:
            self.tp1_triggered = True
        else:
            self.tp2_triggered = True


@dataclass
class Trade:
    code: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    shares: int
    return_pct: float
    profit_amount: float
    exit_reason: str
    hold_days: int
    entry_reason: str = ""
    exit_timing: str = "close"  # "intraday" | "close"


class SimTraderEngine:
    """尾盘模拟交易引擎（含盘中实时监控）"""

    def __init__(self, persist: bool = True):
        self._persist = persist
        self._store = None

        if persist:
            from app.sim_trader.store import SimTraderStore
            self._store = SimTraderStore()
            state = self._store.load_state()
            self.cash = state['cash']
            self.consecutive_losses = state['consecutive_losses']
            self.pause_until = state['pause_until']
            self._trade_count = state['trade_count']
            self.positions = self._store.load_positions()
            self.trades = self._store.load_trades()
            self.equity_curve = self._store.load_equity_curve()
        else:
            self.cash = INITIAL_CAPITAL
            self.positions: Dict[str, Position] = {}
            self.trades: List[Trade] = []
            self.equity_curve: List[Dict] = []
            self.consecutive_losses = 0
            self.pause_until: Optional[date] = None
            self._trade_count = 0

        self._prev_snap: dict = {}  # 前一日快照，用于除权跳空保护

        # 盘中监控器（延迟初始化，避免循环导入）
        self._monitor = None

    @property
    def monitor(self):
        if self._monitor is None:
            try:
                from app.sim_trader.intraday_monitor import IntradayMonitor
                self._monitor = IntradayMonitor(self)
            except ImportError:
                return None
        return self._monitor

    @property
    def monitor_enabled(self) -> bool:
        if self.monitor is None:
            return False
        return getattr(self.monitor, 'enabled', False)

    @property
    def auto_sell(self) -> bool:
        from app.sim_trader.config import AUTO_SELL
        return AUTO_SELL

    @property
    def auto_buy(self) -> bool:
        from app.sim_trader.config import AUTO_BUY
        return AUTO_BUY

    @property
    def auto_scan(self) -> bool:
        from app.sim_trader.config import AUTO_SCAN
        return AUTO_SCAN

    # ── 仓位管理 ──────────────────────────────

    def max_buy_amount(self) -> float:
        if self.consecutive_losses >= LOSS_STREAK_HALVE:
            return POSITION_SIZE / 2
        return POSITION_SIZE

    def active_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.is_active]

    @property
    def position_count(self) -> int:
        return len(self.active_positions())

    def total_equity(self, snapshot: dict) -> float:
        """snapshot: {code: {'close': float, ...}}"""
        pv = sum(
            p.remaining_shares * snapshot.get(p.code, {}).get('close', p.entry_price)
            for p in self.active_positions()
        )
        return self.cash + pv

    # ── 买入 ──────────────────────────────────

    def execute_buy(self, today: date, code: str, price: float,
                    strategy_name: str = "") -> Optional[Position]:
        """买入一只股票，收盘价成交"""
        if code in self.positions:
            return None
        max_amt = min(self.max_buy_amount(), self.cash)
        if max_amt < MIN_BUY_AMT:
            return None
        shares = int(max_amt / price / 100) * 100
        if shares < 100:
            return None
        cost = shares * price
        if cost > self.cash:
            return None

        pos = Position(code=code, entry_date=today, entry_price=price,
                       shares=shares, cost=cost, strategy_name=strategy_name)
        self.cash -= cost
        self.positions[code] = pos
        if self._store:
            self._store.save_positions(self.positions)
            self._store.save_state(
                self.cash, self.consecutive_losses,
                self.pause_until, self._trade_count)
        return pos

    # ── 卖出（止盈止损） ──────────────────────

    def check_stops(self, today: date, snapshot: dict,
                    trading_dates: List[date],
                    prev_snap: dict = None) -> List[Tuple]:
        """
        按优先级检查所有持仓的止盈止损
        返回: [(pos, exit_price, reason, partial_shares_or_None), ...]
        """
        sells = []
        for code, pos in list(self.positions.items()):
            if not pos.is_active or pos.remaining_shares <= 0:
                continue

            bar = snapshot.get(code)
            if bar is None:
                continue

            close_p = bar['close']
            high_p  = bar['high']

            current_pct = close_p / pos.entry_price - 1
            hold_days = sum(1 for td in trading_dates if pos.entry_date <= td <= today)

            # 0. 除权跳空保护：检测隔夜跳空下跌，调整入场价避免误触发硬止损
            if prev_snap:
                prev_bar = prev_snap.get(code)
                if prev_bar and prev_bar.get('close', 0) > 0:
                    overnight_gap = close_p / prev_bar['close'] - 1
                    prefix = code[:3] if len(code) >= 3 else code
                    if prefix in ('300', '301', '688'):
                        gap_limit = -0.20
                    elif prefix[0] == '8':
                        gap_limit = -0.30
                    else:
                        gap_limit = -0.10
                    if overnight_gap <= gap_limit:
                        ratio = close_p / prev_bar['close']
                        pos.entry_price *= ratio
                        pos.peak_price *= ratio
                        current_pct = close_p / pos.entry_price - 1

            # 1. 硬止损
            if current_pct <= HARD_STOP:
                sells.append((pos, close_p,
                    f"硬止损({current_pct*100:.1f}%)", None))
                continue

            # 2. 时间强制
            if hold_days > TIME_FORCE_DAYS:
                sells.append((pos, close_p, f"时间强制({hold_days}天)", None))
                continue

            # 3. 多档阶梯止盈（在 peak_price 更新前检查，避免 TP 拉宽 TR 回撤基准）
            for idx, tier in enumerate(TAKE_PROFIT_TIERS):
                if not pos.is_tier_triggered(idx) and current_pct >= tier['profit_pct']:
                    ss = int(pos.remaining_shares * tier['sell_ratio'] / 100) * 100
                    if ss >= 100:
                        pos.mark_tier_triggered(idx)
                        sells.append((pos, close_p,
                            f"TP{idx+1} +{tier['profit_pct']*100:.0f}%({current_pct*100:.1f}%)", ss))
                        break

            # 更新峰值（在 TP 之后，避免 TP 当天的高点拉宽 TR 回撤）
            if high_p > pos.peak_price:
                pos.peak_price = high_p
            peak_pct = pos.peak_price / pos.entry_price - 1

            # 4. 移动止盈（支持 ATR 动态回撤）
            if peak_pct >= TRAIL_ACTIVATE:
                dd = close_p / pos.peak_price - 1
                eff_trail_dd = TRAIL_DD
                if USE_ATR_TRAIL and bar.get('atr', 0) > 0:
                    atr_pct = ATR_TRAIL_MULTIPLIER * bar['atr'] / pos.entry_price
                    eff_trail_dd = max(TRAIL_DD, atr_pct)
                if dd <= -eff_trail_dd:
                    sells.append((pos, close_p,
                        f"移动止盈(峰{peak_pct*100:.1f}%回{dd*100:.1f}%)", None))
                    continue

            # 5. 时间条件
            if hold_days > TIME_EXIT_DAYS and current_pct > TIME_EXIT_PROFIT:
                sells.append((pos, close_p,
                    f"时间条件({hold_days}天+{current_pct*100:.1f}%)", None))
                continue

        return sells

    def execute_sell(self, pos: Position, exit_price: float, reason: str,
                     partial: Optional[int] = None,
                     exit_date: Optional[date] = None,
                     exit_timing: str = "close") -> Optional[Trade]:
        ss = partial if partial is not None else pos.remaining_shares
        ss = int(ss // 100 * 100)
        if ss <= 0:
            return None

        rp = (exit_price / pos.entry_price - 1) * 100
        profit = ss * (exit_price - pos.entry_price)
        pos.remaining_shares -= ss

        if pos.remaining_shares <= 0:
            pos.is_active = False
            pos.remaining_shares = 0

        self.cash += ss * exit_price
        self._trade_count += 1

        return Trade(
            code=pos.code, entry_date=pos.entry_date,
            exit_date=exit_date or date.today(),
            entry_price=pos.entry_price, exit_price=exit_price,
            shares=ss, return_pct=rp, profit_amount=profit,
            exit_reason=reason, hold_days=0,
            entry_reason=pos.strategy_name, exit_timing=exit_timing,
        )

    def sell_phase(self, today: date, snapshot: dict,
                   trading_dates: List[date]):
        """卖出阶段（先卖后买，回收现金）"""
        sells = self.check_stops(today, snapshot, trading_dates,
                                 prev_snap=self._prev_snap)

        for pos, exit_price, reason, partial in sells:
            trade = self.execute_sell(pos, exit_price, reason, partial,
                                      exit_date=today, exit_timing="close")
            if trade:
                trade.hold_days = sum(1 for td in trading_dates
                                      if pos.entry_date <= td <= today)
                self.trades.append(trade)

                if trade.return_pct <= 0:
                    self.consecutive_losses += 1
                else:
                    self.consecutive_losses = 0
                    self.pause_until = None

                if self.consecutive_losses >= LOSS_STREAK_PAUSE:
                    self.pause_until = today + timedelta(days=PAUSE_DAYS)

                if self._store:
                    self._store.save_trade(trade)

        # 清理已平仓
        self.positions = {k: v for k, v in self.positions.items() if v.is_active}
        # 保存当日快照供次日除权跳空保护
        self._prev_snap = {k: dict(v) for k, v in snapshot.items()}
        if self._store:
            self._store.save_positions(self.positions)
            self._store.save_state(
                self.cash, self.consecutive_losses,
                self.pause_until, self._trade_count)

    # ── 记录 ──────────────────────────────────

    def record(self, today: date, snapshot: dict):
        eq = self.total_equity(snapshot)
        self.equity_curve.append({
            'date': today,
            'equity': eq,
            'cash': self.cash,
            'positions': self.position_count,
        })
        if self._store:
            self._store.save_equity_point(today, eq, self.cash, self.position_count)
            self._store.save_state(
                self.cash, self.consecutive_losses,
                self.pause_until, self._trade_count)
