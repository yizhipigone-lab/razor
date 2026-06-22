"""验证 #11 修复: self.trades 不再被双路径 append"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
from unittest.mock import MagicMock, patch
from datetime import date, datetime as real_datetime


def _make_engine():
    """构造一个 MagicMock store + SimTraderEngine"""
    from app.sim_trader.engine import SimTraderEngine
    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = []
    mock_store.load_equity_curve.return_value = []
    engine = SimTraderEngine(store=mock_store)
    return engine


def test_sell_phase_no_longer_appends():
    """sell_phase 不应再 append 到 engine.trades"""
    from app.sim_trader.engine import Position

    engine = _make_engine()
    pos = Position(
        code='000001', entry_date=date(2025, 1, 3),
        entry_price=10.0, shares=100, cost=1000.0
    )
    engine.positions['000001'] = pos

    initial_trades_len = len(engine.trades)
    snapshot = {'000001': {'close': 9.0, 'open': 9.5, 'high': 9.5, 'low': 8.5}}
    trading_dates = [date(2025, 1, 6)]

    # sell_phase 函数体内 from datetime import datetime,所以要 patch 全局 datetime
    fake_now = real_datetime(2025, 1, 6, 10, 0, 0)

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                from datetime import timezone
                return fake_now.replace(tzinfo=tz)
            return fake_now
        @classmethod
        def fromtimestamp(cls, t, *a, **kw):
            return real_datetime.fromtimestamp(t, *a, **kw)
        @classmethod
        def strptime(cls, *a, **kw):
            return real_datetime.strptime(*a, **kw)

    with patch('datetime.datetime', FakeDateTime):
        engine.sell_phase(date(2025, 1, 6), snapshot, trading_dates)

    final_trades_len = len(engine.trades)
    assert final_trades_len == initial_trades_len, \
        f"engine.trades 被 append: {initial_trades_len} -> {final_trades_len}"
    assert engine._store.save_trade.called, "未调用 save_trade"
    print(f"[OK] sell_phase: trades 长度 {initial_trades_len} -> {final_trades_len} (未膨胀)")


def test_intraday_execute_sell_no_longer_appends():
    """_execute_sell 不应再 append"""
    from app.sim_trader.engine import Position
    from app.sim_trader.intraday_monitor import IntradayMonitor

    engine = _make_engine()
    monitor = IntradayMonitor(engine)
    pos = Position(
        code='000001', entry_date=date(2025, 1, 3),
        entry_price=10.0, shares=100, cost=1000.0
    )
    engine.positions['000001'] = pos

    initial_trades_len = len(engine.trades)
    monitor._execute_sell(pos, 9.0, 'HS', None)
    final_trades_len = len(engine.trades)
    assert final_trades_len == initial_trades_len, \
        f"intraday _execute_sell 仍 append: {initial_trades_len} -> {final_trades_len}"
    print(f"[OK] intraday _execute_sell: trades 长度 {initial_trades_len} -> {final_trades_len} (未膨胀)")


def test_no_append_in_source():
    """engine.py 和 intraday_monitor.py 不应再有 self.trades.append"""
    import re
    for path in ['app/sim_trader/engine.py', 'app/sim_trader/intraday_monitor.py']:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        matches = re.findall(r'self\.trades\.append', content)
        assert len(matches) == 0, f"{path} 仍存在 self.trades.append: {matches}"
        print(f"[OK] {path} 无 self.trades.append")


if __name__ == '__main__':
    test_sell_phase_no_longer_appends()
    test_intraday_execute_sell_no_longer_appends()
    test_no_append_in_source()
    print("\n[ALL PASS] #11 修复验证通过")