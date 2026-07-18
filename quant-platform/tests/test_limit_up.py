import math
import pytest
from app.utils.limit_up import get_limit_up_pct, is_limit_up, _is_valid_price


class TestGetLimitUpPct:
    def test_main_board(self):
        assert abs(get_limit_up_pct("600519") - 0.10) < 1e-6

    def test_gem(self):
        assert abs(get_limit_up_pct("300750") - 0.20) < 1e-6

    def test_star(self):
        assert abs(get_limit_up_pct("688981") - 0.20) < 1e-6

    def test_bse(self):
        assert abs(get_limit_up_pct("830123") - 0.30) < 1e-6


class TestIsValidPrice:
    def test_valid(self):
        assert _is_valid_price(10.0) is True

    def test_zero_invalid(self):
        assert _is_valid_price(0.0) is False

    def test_negative_invalid(self):
        assert _is_valid_price(-1.0) is False

    def test_nan_invalid(self):
        assert _is_valid_price(float("nan")) is False

    def test_none_invalid(self):
        assert _is_valid_price(None) is False


class TestIsLimitUp:
    def test_normal_not_limit(self):
        is_limit, reason = is_limit_up("600519", 100.0, 103.0)
        assert is_limit is False
        assert reason == "OK"

    def test_limit_up_blocked(self):
        is_limit, reason = is_limit_up("300750", 100.0, 120.0)
        assert is_limit is True
        assert "limit_up" in reason

    def test_strict_missing_prev_close(self):
        is_limit, reason = is_limit_up("600519", 0.0, 103.0, strict=True)
        assert is_limit is True
        assert reason == "missing_price_data"

    def test_non_strict_missing_prev_close(self):
        is_limit, reason = is_limit_up("600519", 0.0, 103.0, strict=False)
        assert is_limit is False
        assert reason == "missing_price_data_ok"

    def test_nan_input_strict(self):
        is_limit, reason = is_limit_up("600519", float("nan"), 103.0, strict=True)
        assert is_limit is True
        assert reason == "missing_price_data"

    def test_none_input_strict(self):
        is_limit, reason = is_limit_up("600519", None, 103.0, strict=True)
        assert is_limit is True
        assert reason == "missing_price_data"
