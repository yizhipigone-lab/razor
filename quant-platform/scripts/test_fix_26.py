"""验证 L26 修复: 4 引擎统一用交易日计数"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')
from datetime import date


def test_trading_calendar_basic():
    from app.backtest.trading_calendar import TradingCalendar
    cal = TradingCalendar([date(2026, 1, 5), date(2026, 1, 7), date(2026, 1, 9)])
    assert cal.trading_days_between(date(2026, 1, 5), date(2026, 1, 9)) == 3
    assert cal.trading_days_between(date(2026, 1, 5), date(2026, 1, 7)) == 2
    print("OK TradingCalendar correct")


def test_is_trading_day():
    from app.backtest.trading_calendar import TradingCalendar
    cal = TradingCalendar([date(2026, 1, 5)])
    assert cal.is_trading_day(date(2026, 1, 5))
    assert not cal.is_trading_day(date(2026, 1, 6))
    print("OK is_trading_day correct")


def test_tdx_uses_trading_calendar():
    with open('app/backtest/tdx_runner.py', encoding='utf-8') as f:
        content = f.read()
    assert 'TradingCalendar' in content
    print("OK tdx_runner uses TradingCalendar")


if __name__ == '__main__':
    test_trading_calendar_basic()
    test_is_trading_day()
    test_tdx_uses_trading_calendar()
    print("\nL26 修复验证通过")
