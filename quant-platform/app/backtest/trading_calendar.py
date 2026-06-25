"""交易日历 (L26 修复) 4 引擎统一用交易日计数"""
from datetime import date
from typing import List


class TradingCalendar:
    def __init__(self, trading_dates: List[date]) -> None:
        self._dates = sorted(set(trading_dates))

    def trading_days_between(self, d1: date, d2: date) -> int:
        return sum(1 for d in self._dates if d1 <= d <= d2)

    def is_trading_day(self, d: date) -> bool:
        return d in self._dates
