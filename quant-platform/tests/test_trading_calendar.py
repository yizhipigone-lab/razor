"""TradingCalendar 单元测试"""
from datetime import date

from app.backtest.trading_calendar import TradingCalendar


def test_trading_calendar_basic():
    """TradingCalendar 基本功能：交易日计数"""
    cal = TradingCalendar([
        date(2026, 6, 22),
        date(2026, 6, 23),
        date(2026, 6, 24),
        date(2026, 6, 25),
        date(2026, 6, 26),
    ])
    # 区间内交易日数
    assert cal.trading_days_between(date(2026, 6, 22), date(2026, 6, 24)) == 3
    # 包含首尾
    assert cal.trading_days_between(date(2026, 6, 22), date(2026, 6, 22)) == 1
    # 区间外
    assert cal.trading_days_between(date(2026, 6, 1), date(2026, 6, 10)) == 0


def test_is_trading_day():
    """is_trading_day 判断"""
    cal = TradingCalendar([
        date(2026, 6, 22),
        date(2026, 6, 24),
        date(2026, 6, 26),
    ])
    assert cal.is_trading_day(date(2026, 6, 22))
    assert not cal.is_trading_day(date(2026, 6, 23))
    assert cal.is_trading_day(date(2026, 6, 26))
    assert not cal.is_trading_day(date(2026, 6, 27))
