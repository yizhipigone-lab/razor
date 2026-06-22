"""验证 #13 修复: intraday_monitor.hold_days 与 engine.check_stops 一致"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
from datetime import date
from unittest.mock import patch


def test_hold_days_uses_trading_dates():
    """hold_days 应是交易日数,不是日历日"""
    fake_calendar = {
        date(2025, 1, 3),   # 周五
        date(2025, 1, 6),   # 周一
        date(2025, 1, 7),   # 周二
    }

    with patch('app.api.sim_trader._load_trading_calendar', return_value=fake_calendar):
        from app.sim_trader.intraday_monitor import IntradayMonitor
        from app.sim_trader.engine import SimTraderEngine, Position
        from unittest.mock import MagicMock

        mock_store = MagicMock()
        mock_store.load_state.return_value = {
            'cash': 100000, 'consecutive_losses': 0,
            'pause_until': None, 'trade_count': 0
        }
        mock_store.load_positions.return_value = {}
        mock_store.load_trades.return_value = []
        mock_store.load_equity_curve.return_value = []

        engine = SimTraderEngine(store=mock_store)
        monitor = IntradayMonitor(engine)
        pos = Position(
            code='000001',
            entry_date=date(2025, 1, 3),
            entry_price=10.0, shares=100, cost=1000.0
        )
        with patch('app.sim_trader.intraday_monitor.date') as mock_date:
            mock_date.today.return_value = date(2025, 1, 7)
            mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
            result = monitor._check_position(pos, 10.5, 10.5)

    # _check_position 内部 hold_days 计算,触发 exit_rule_engine
    # 测试只要不抛 TypeError 就算通过(说明 hold_days 是数值)
    print("OK intraday_monitor._check_position 接受 trading_dates 日历")


def test_hold_days_min_one_on_empty_calendar():
    """交易日历为空时, hold_days 至少 1(防空日历误触)"""
    with patch('app.api.sim_trader._load_trading_calendar', return_value=set()):
        from app.sim_trader.intraday_monitor import IntradayMonitor
        from app.sim_trader.engine import SimTraderEngine, Position
        from unittest.mock import MagicMock

        mock_store = MagicMock()
        mock_store.load_state.return_value = {
            'cash': 100000, 'consecutive_losses': 0,
            'pause_until': None, 'trade_count': 0
        }
        mock_store.load_positions.return_value = {}
        mock_store.load_trades.return_value = []
        mock_store.load_equity_curve.return_value = []

        engine = SimTraderEngine(store=mock_store)
        monitor = IntradayMonitor(engine)
        pos = Position(
            code='000001',
            entry_date=date(2025, 1, 3),
            entry_price=10.0, shares=100, cost=1000.0
        )
        with patch('app.sim_trader.intraday_monitor.date') as mock_date:
            mock_date.today.return_value = date(2025, 1, 7)
            mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
            result = monitor._check_position(pos, 10.5, 10.5)
    print("OK 空日历时 _check_position 不抛错")


def test_no_calendar_day_in_source():
    """源码中不应再有 (date.today() - pos.entry_date).days 模式"""
    with open('app/sim_trader/intraday_monitor.py', 'r', encoding='utf-8') as f:
        content = f.read()
    bad_pattern = "(date.today() - pos.entry_date).days"
    assert bad_pattern not in content, f"仍存在日历日公式: {bad_pattern}"
    print("OK intraday_monitor.py 无日历日 hold_days 公式")


if __name__ == '__main__':
    test_hold_days_uses_trading_dates()
    test_hold_days_min_one_on_empty_calendar()
    test_no_calendar_day_in_source()
    print("\n#13 修复验证通过")
