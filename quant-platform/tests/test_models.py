"""models.py 单元测试 — Position / Trade / CycleResult 叶子模块。"""
from datetime import date

import pytest

from app.sim_trader.models import Position, Trade, CycleResult


def _make_pos(entry=10.0, shares=1000):
    return Position(code="000001", entry_date=date(2026, 3, 2),
                    entry_price=entry, shares=shares, cost=entry * shares)


class TestPosition:
    def test_post_init_sets_peak_and_remaining(self):
        pos = _make_pos(10.0, 1000)
        assert pos.peak_price == 10.0
        assert pos.remaining_shares == 1000

    def test_market_value_uses_current_price(self):
        pos = _make_pos(10.0, 1000)
        pos.current_price = 15.0
        assert pos.market_value == 1000 * 15.0

    def test_profit_pct_zero_when_no_current_price(self):
        pos = _make_pos(10.0)
        assert pos.profit_pct == 0.0

    def test_profit_pct_uses_current_price(self):
        pos = _make_pos(10.0)
        pos.current_price = 12.0
        assert pos.profit_pct == pytest.approx(20.0)

    def test_today_pnl_overnight_uses_prev_close(self):
        pos = _make_pos(10.0, 1000)
        pos.remaining_shares = 1000
        # 过夜: 基准=昨收 10.5, 现 11.0 → +500
        assert pos.today_pnl(11.0, 10.5, date(2026, 3, 3)) == 500.0

    def test_today_pnl_bought_today_uses_entry(self):
        today = date(2026, 3, 3)
        pos = Position(code="000001", entry_date=today, entry_price=10.0,
                       shares=1000, cost=10_000)
        pos.remaining_shares = 1000
        # 当日买入: 基准=entry 10.0, 现 10.8 → +800
        assert pos.today_pnl(10.8, 10.5, today) == 800.0

    def test_today_pnl_closed_returns_none(self):
        pos = _make_pos(10.0, 1000)
        pos.remaining_shares = 0
        assert pos.today_pnl(11.0, 10.0, date(2026, 3, 3)) is None

    def test_today_pnl_missing_price_returns_none(self):
        pos = _make_pos(10.0, 1000)
        pos.remaining_shares = 1000
        assert pos.today_pnl(0.0, 10.0, date(2026, 3, 3)) is None

    def test_tier_trigger_helpers(self):
        pos = _make_pos(10.0)
        assert pos.is_tier_triggered(0) is False
        pos.mark_tier_triggered(0)
        assert pos.is_tier_triggered(0) is True
        assert pos.is_tier_triggered(1) is False
        pos.mark_tier_triggered(1)
        assert pos.is_tier_triggered(1) is True


class TestTrade:
    def test_trade_defaults(self):
        t = Trade(code="000001", entry_date=date(2026, 3, 2),
                  exit_date=date(2026, 3, 3), entry_price=10.0,
                  exit_price=11.0, shares=1000, return_pct=10.0,
                  profit_amount=1000.0, exit_reason="TP1", hold_days=1)
        assert t.exit_timing == "close"
        assert t.entry_reason == ""
        assert t.entry_time == "15:00"


class TestCycleResult:
    def test_defaults_sell_signals_empty_list(self):
        cr = CycleResult(0, 0, 1_000_000, 1_000_000, 0, 0)
        assert cr.sell_signals == []

    def test_frozen(self):
        cr = CycleResult(0, 0, 1.0, 1.0, 0, 0)
        with pytest.raises(Exception):
            cr.sell_count = 5  # frozen dataclass 不可变

    def test_independent_default_lists(self):
        """两个实例的 sell_signals 默认值应独立(不共享同一 list)。"""
        a = CycleResult(0, 0, 1.0, 1.0, 0, 0)
        b = CycleResult(0, 0, 1.0, 1.0, 0, 0)
        a.sell_signals.append(("000001", "HS"))
        assert b.sell_signals == []
