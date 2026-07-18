"""测试 app/backtest/execution.py 四个核心函数."""
import pytest
from app.backtest.execution import (
    get_limit_up_pct,
    can_buy,
    can_sell_today,
    calc_buy_cost,
    calc_sell_revenue,
)
from datetime import date


class TestGetLimitUpPct:
    def test_main_board(self):
        assert abs(get_limit_up_pct("600519") - 0.10) < 1e-6

    def test_gem(self):
        assert abs(get_limit_up_pct("300750") - 0.20) < 1e-6

    def test_star(self):
        assert abs(get_limit_up_pct("688981") - 0.20) < 1e-6

    def test_bse(self):
        assert abs(get_limit_up_pct("830123") - 0.30) < 1e-6


class TestCanBuy:
    def test_normal_buy(self):
        ok, _ = can_buy("600519", 100.0, 103.0)
        assert ok

    def test_limit_up_blocked(self):
        ok, msg = can_buy("300750", 100.0, 120.0)
        assert not ok
        assert "涨停" in msg or "limit_up" in msg

    def test_strict_prev_close_zero_rejects(self):
        ok, msg = can_buy("000001", 0, 110.0, strict=True)
        assert not ok
        assert msg == "missing_price_data"

    def test_non_strict_prev_close_zero_ok(self):
        ok, msg = can_buy("000001", 0, 110.0, strict=False)
        assert ok
        assert msg == "missing_price_data_ok"

    def test_strict_nan_rejects(self):
        ok, msg = can_buy("000001", float("nan"), 110.0, strict=True)
        assert not ok
        assert msg == "missing_price_data"

    def test_strict_none_rejects(self):
        ok, msg = can_buy("000001", None, 110.0, strict=True)
        assert not ok
        assert msg == "missing_price_data"


class TestCanSellToday:
    def test_same_day_blocked(self):
        d = date(2026, 1, 5)
        assert not can_sell_today(d, d)

    def test_next_day_allowed(self):
        assert can_sell_today(date(2026, 1, 5), date(2026, 1, 6))


class TestCost:
    def test_buy_cost_includes_commission(self):
        r = calc_buy_cost(10.0, 1000)
        assert r["commission"] >= 5.0
        assert r["total"] > 10000

    def test_sell_revenue_deducts_stamp_tax(self):
        r = calc_sell_revenue(11.0, 1000)
        assert r["stamp_tax"] == 5.5
        assert r["total"] < 11000
