import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.backtest.tdx_runner import _resolve_intraday_buy_price


class TestResolveIntradayBuyPrice:
    def test_5m_bar_available(self):
        px, source, fb = _resolve_intraday_buy_price(
            "600000", "20240103", {"600000"},
            {("600000", "20240103"): {"close": 25.0}},
            {}
        )
        assert px == 25.0
        assert source == "intraday"
        assert fb == 0

    def test_5m_missing_falls_back_to_daily_close(self):
        px, source, fb = _resolve_intraday_buy_price(
            "600000", "20240103", {"600000"},
            {},
            {"20240103": {"600000": {"close": 24.0}}}
        )
        assert px == 24.0
        assert source == "daily_fallback"
        assert fb == 1

    def test_5m_missing_and_daily_invalid_returns_none(self):
        px, source, fb = _resolve_intraday_buy_price(
            "600000", "20240103", {"600000"},
            {},
            {"20240103": {"600000": {"close": 0.0}}}
        )
        assert px is None
        assert source is None
        assert fb == 0

    def test_not_in_intraday_set_uses_daily(self):
        px, source, fb = _resolve_intraday_buy_price(
            "600000", "20240103", set(),
            {},
            {"20240103": {"600000": {"close": 23.0}}}
        )
        assert px == 23.0
        assert source == "daily"
        assert fb == 0

    def test_none_daily_close_returns_none(self):
        px, source, fb = _resolve_intraday_buy_price(
            "600000", "20240103", set(),
            {},
            {"20240103": {}}
        )
        assert px is None
        assert source is None
        assert fb == 0
