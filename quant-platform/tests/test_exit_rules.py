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
