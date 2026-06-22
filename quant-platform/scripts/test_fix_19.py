"""验证 L5 修复: _prev_day_snap 持久化到 store,冷启动可加载"""
import sys
import os
import datetime as _dt
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')
from datetime import date
from unittest.mock import MagicMock, patch


class _FakeDatetime(_dt.datetime):
    """fake datetime.now 返回 14:55,在 sell_phase 交易时段守卫内"""
    @classmethod
    def now(cls, tz=None):
        return _dt.datetime(2025, 1, 6, 14, 55, 0)


def test_store_source_has_methods():
    """store.py 源码应有 save_prev_day_snap 和 load_prev_day_snap"""
    with open('app/sim_trader/store.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'def save_prev_day_snap' in content, "缺 save_prev_day_snap"
    assert 'def load_prev_day_snap' in content, "缺 load_prev_day_snap"
    print("PASS: store.py 源码有 save/load_prev_day_snap")


def test_engine_loads_prev_day_snap_on_init():
    """engine 初始化时应从 store 加载 _prev_day_snap"""
    from app.sim_trader.engine import SimTraderEngine

    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = []
    mock_store.load_equity_curve.return_value = []
    mock_store.load_prev_day_snap.return_value = {'000001': {'close': 10.0}}

    engine = SimTraderEngine(store=mock_store)
    assert mock_store.load_prev_day_snap.called, "未调用 load_prev_day_snap"
    assert engine._prev_day_snap == {'000001': {'close': 10.0}}, \
        f"_prev_day_snap 未从 store 加载: {engine._prev_day_snap}"
    print("PASS: engine 初始化时从 store 加载 _prev_day_snap")


def test_engine_saves_prev_day_snap_in_sell_phase():
    """sell_phase 末尾应保存 _prev_day_snap 到 store"""
    from app.sim_trader.engine import SimTraderEngine

    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = []
    mock_store.load_equity_curve.return_value = []
    mock_store.load_prev_day_snap.return_value = {}

    engine = SimTraderEngine(store=mock_store)
    today_snap = {'000001': {'close': 10.0, 'open': 9.8, 'high': 10.2, 'low': 9.7}}
    trading_dates = [date(2025, 1, 6)]

    # sell_phase 守卫交易时段,需 mock datetime.now 为 14:55
    with patch('datetime.datetime', _FakeDatetime):
        engine.sell_phase(date(2025, 1, 6), today_snap, trading_dates)

    assert mock_store.save_prev_day_snap.called, "sell_phase 末尾未调用 save_prev_day_snap"
    print("PASS: sell_phase 末尾保存 _prev_day_snap 到 store")


if __name__ == '__main__':
    test_store_source_has_methods()
    test_engine_loads_prev_day_snap_on_init()
    test_engine_saves_prev_day_snap_in_sell_phase()
    print("\nAll L5 fix verifications passed")
