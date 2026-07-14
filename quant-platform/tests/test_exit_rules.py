"""Unit tests for unified exit rules engine: HS, TR, TP."""

import pytest
from app.backtest.exit_rules import (
    ExitRuleEngine,
    RuleContext,
    rule_hard_stop,
    rule_trailing_stop,
    rule_take_profit,
)


class TestHardStop:
    def test_low_triggers(self):
        ctx = RuleContext(
            entry_price=10.0, peak_price=10.0,
            open=9.5, high=9.6, low=9.4, close=9.6, atr=0,
            hold_days=2, hard_stop=-0.05,
            take_profit_tiers=[],
            trail_activate=0.03, trail_dd=0.01,
            time_exit_days=7, time_exit_profit=0.02, time_force_days=12,
        )
        sig = rule_hard_stop(ctx)
        assert sig is not None
        assert sig.reason == "HS"

    def test_close_fallback_triggers(self):
        ctx = RuleContext(
            entry_price=10.0, peak_price=10.0,
            open=9.5, high=9.6, low=0, close=9.5, atr=0,
            hold_days=2, hard_stop=-0.05,
            take_profit_tiers=[],
            trail_activate=0.03, trail_dd=0.01,
            time_exit_days=7, time_exit_profit=0.02, time_force_days=12,
        )
        sig = rule_hard_stop(ctx)
        assert sig is not None
        assert sig.reason == "HS"
        assert sig.sell_price == 9.5

    def test_no_trigger_above_stop(self):
        ctx = RuleContext(
            entry_price=10.0, peak_price=10.0,
            open=9.6, high=9.7, low=9.55, close=9.6, atr=0,
            hold_days=1, hard_stop=-0.05,
            take_profit_tiers=[],
            trail_activate=0.03, trail_dd=0.01,
            time_exit_days=7, time_exit_profit=0.02, time_force_days=12,
        )
        assert rule_hard_stop(ctx) is None


class TestTrailingStop:
    def test_peaks_above_activate(self):
        """peak reached activate threshold, then low retreats beyond trail_dd"""
        ctx = RuleContext(
            entry_price=10.0, peak_price=12.0,  # peak 20%
            open=11.5, high=11.6, low=9.0, close=9.0, atr=0,
            hold_days=5, hard_stop=-0.05,
            take_profit_tiers=[],
            trail_activate=0.05, trail_dd=0.02,
            time_exit_days=7, time_exit_profit=0.02, time_force_days=12,
        )
        sig = rule_trailing_stop(ctx)
        assert sig is not None
        assert sig.reason == "TR"

    def test_peak_not_activated(self):
        ctx = RuleContext(
            entry_price=10.0, peak_price=10.2,  # only 2%
            open=9.9, high=10.0, low=9.0, close=9.0, atr=0,
            hold_days=5, hard_stop=-0.05,
            take_profit_tiers=[],
            trail_activate=0.05, trail_dd=0.02,
            time_exit_days=7, time_exit_profit=0.02, time_force_days=12,
        )
        assert rule_trailing_stop(ctx) is None


class TestTakeProfit:
    def test_tp1_trigger(self):
        ctx = RuleContext(
            entry_price=10.0, peak_price=10.0,
            open=11.5, high=11.6, low=11.4, close=11.6, atr=0,
            hold_days=3, hard_stop=-0.05,
            take_profit_tiers=[{"profit_pct": 0.15, "sell_ratio": 1.0}],
            trail_activate=0.03, trail_dd=0.01,
            time_exit_days=7, time_exit_profit=0.02, time_force_days=12,
            use_high_for_tp=True,
        )
        sig = rule_take_profit(ctx)
        assert sig is not None
        assert sig.reason == "TP1"

    def test_tp_not_triggered(self):
        ctx = RuleContext(
            entry_price=10.0, peak_price=10.0,
            open=11.0, high=11.2, low=10.9, close=11.1, atr=0,
            hold_days=3, hard_stop=-0.05,
            take_profit_tiers=[{"profit_pct": 0.15, "sell_ratio": 1.0}],
            trail_activate=0.03, trail_dd=0.01,
            time_exit_days=7, time_exit_profit=0.02, time_force_days=12,
            use_high_for_tp=True,
        )
        assert rule_take_profit(ctx) is None

    def test_tier_already_triggered_skips(self):
        ctx = RuleContext(
            entry_price=10.0, peak_price=10.0,
            open=12.6, high=12.7, low=11.4, close=12.6, atr=0,
            hold_days=3, hard_stop=-0.05,
            take_profit_tiers=[
                {"profit_pct": 0.15, "sell_ratio": 0.3},
                {"profit_pct": 0.25, "sell_ratio": 1.0},
            ],
            triggered_tiers={0},  # TP1 already done
            trail_activate=0.03, trail_dd=0.01,
            time_exit_days=7, time_exit_profit=0.02, time_force_days=12,
            use_high_for_tp=True,
        )
        sig = rule_take_profit(ctx)
        assert sig is not None
        assert sig.reason == "TP2"


# ── H8(2026-07-15 全项目审计): adjust_for_gap 不应把正常跌停误判为除权 ──

class TestAdjustForGap:
    """H8: 旧版阈值 -0.10/-0.12/-0.30 恰好等于/小于跌停幅度, 正常跌停被误判除权
    → 永久下调 entry/peak。修复后阈值超过跌停幅度才触发。"""

    def test_main_board_normal_limit_down_not_triggered(self):
        from app.backtest.exit_rules import adjust_for_gap
        # 主板跌停 -10%: 不应触发(旧 bug 会把 entry 10→9)
        entry, peak = adjust_for_gap("000001.SZ", 10.0, 12.0, 9.0, 10.0)
        assert entry == 10.0
        assert peak == 12.0

    def test_main_board_ex_rights_beyond_limit_triggered(self):
        from app.backtest.exit_rules import adjust_for_gap
        # 超过跌停(-25%): 真除权, 按比例下调
        entry, peak = adjust_for_gap("000001.SZ", 10.0, 12.0, 7.5, 10.0)
        assert entry == pytest.approx(7.5)
        assert peak == pytest.approx(9.0)

    def test_chinext_normal_limit_down_not_triggered(self):
        from app.backtest.exit_rules import adjust_for_gap
        # 创业板跌停 -20%: 旧 -0.12 阈值会误判, 修复后不触发
        entry, peak = adjust_for_gap("300001.SZ", 10.0, 12.0, 8.0, 10.0)
        assert entry == 10.0
        assert peak == 12.0

    def test_star_market_normal_drop_not_triggered(self):
        from app.backtest.exit_rules import adjust_for_gap
        # 科创板 -18%(跌停-20%内正常波动): 不触发
        entry, peak = adjust_for_gap("688001.SH", 10.0, 12.0, 8.2, 10.0)
        assert entry == 10.0

    def test_bj_4xx_uses_30pct_threshold(self):
        """M1: 北证 4xx 应走 -30% 阈值, 不是主板 -10%。-15% 不触发。"""
        from app.backtest.exit_rules import adjust_for_gap
        entry, peak = adjust_for_gap("430123.BJ", 10.0, 12.0, 8.5, 10.0)  # -15%
        assert entry == 10.0  # 不触发(旧版 4xx 走 -0.10 会误判)

    def test_zero_prev_close_no_change(self):
        from app.backtest.exit_rules import adjust_for_gap
        entry, peak = adjust_for_gap("000001.SZ", 10.0, 12.0, 9.0, 0.0)
        assert entry == 10.0
        assert peak == 12.0
