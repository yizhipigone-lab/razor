"""
模拟盘交易 — 数据模型（叶子模块）

Position / Trade / CycleResult 的唯一定义处。
2026-07-14 从 engine.py 抽出，打破 engine ↔ store 的循环依赖：
  - engine.py 顶部 `from app.sim_trader.models import Position, Trade, CycleResult`
    并 re-export（`from app.sim_trader.engine import Position` 仍可用，向后兼容）
  - store.py 改从 models 导入（models 是叶子，无循环）

本文件只依赖标准库，禁止 import engine/store，否则重新引入循环。
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


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
    entry_time: str = "15:00"
    current_price: float = 0.0  # #8 修复:由 record() 阶段从 snapshot 写入

    def __post_init__(self):
        self.peak_price = self.entry_price
        self.remaining_shares = self.shares

    @property
    def market_value(self) -> float:
        return self.remaining_shares * self.current_price  # #8 修复:用当前价

    @property
    def profit_pct(self) -> float:  # #8 修复:从方法变 property
        if self.current_price <= 0:
            return 0.0
        return (self.current_price / self.entry_price - 1) * 100

    def today_pnl(self, cur_price: float, prev_close: float,
                  today: date) -> Optional[float]:
        """CARD4 统一今日盈亏(后端唯一真相源)。

        基准: 当日买入 = entry_price, 过夜 = prev_close。
        return round(remaining_shares * (cur - base), 0)
        已平仓 / 缺现价 / 缺基准 → None(保留 api 471 哨兵语义)。
        """
        if self.remaining_shares <= 0:
            return None
        if not cur_price or cur_price <= 0:
            return None
        base_px = self.entry_price if self.entry_date == today else prev_close
        if not base_px or base_px <= 0:
            return None
        rem = self.remaining_shares or self.shares
        return round(rem * (cur_price - base_px), 0)

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
    entry_time: str = "15:00"
    exit_time: str = "15:00"


@dataclass(frozen=True)
class CycleResult:
    """execute_daily_cycle 返回值。字段对齐现有 caller 的 broadcast payload。

    注: 本 dataclass 在本安全子集阶段先落地(供后续 Step 7 execute_daily_cycle 使用)，
    sell_signals 字段为 cron_jobs readonly 模式预留。
    """
    sell_count: int
    buy_count: int
    equity: float
    cash: float
    positions: int
    signals_count: int
    sell_signals: list = field(default_factory=list)  # [(code, reason), ...]
