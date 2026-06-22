"""验证 L4 修复: refresh_trades_from_store() 从 store 加载,reporter 入口调用"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')
from datetime import date
from unittest.mock import MagicMock

def test_refresh_trades_from_store_method_exists():
    """engine 应该有 refresh_trades_from_store 方法"""
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
    assert hasattr(engine, 'refresh_trades_from_store'), "缺 refresh_trades_from_store 方法"
    assert callable(engine.refresh_trades_from_store), "refresh_trades_from_store 不可调用"
    print("PASS: refresh_trades_from_store 方法存在")

def test_refresh_loads_from_store():
    """refresh_trades_from_store 应从 store 加载"""
    from app.sim_trader.engine import SimTraderEngine, Trade

    fake_trade = Trade(
        code='000001', entry_date=date(2025, 1, 3),
        exit_date=date(2025, 1, 6),
        entry_price=10.0, exit_price=11.0,
        shares=100, return_pct=10.0, profit_amount=100.0,
        exit_reason='TP1', hold_days=3
    )

    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = [fake_trade]  # store 有 1 笔
    mock_store.load_equity_curve.return_value = []

    engine = SimTraderEngine(store=mock_store)
    # engine.__init__ 已经从 store load 了 trades, 验证初始 trades 来自 store
    assert len(engine.trades) == 1, f"engine 初始化后应有 1 笔 (来自 store load), 实际 {len(engine.trades)}"
    mock_store.load_trades.assert_called_once()

    # 模拟运行期新增 trade 后, store 又多了一笔, refresh 应能拉到最新
    extra_trade = Trade(
        code='000002', entry_date=date(2025, 1, 5),
        exit_date=date(2025, 1, 8),
        entry_price=20.0, exit_price=22.0,
        shares=50, return_pct=10.0, profit_amount=100.0,
        exit_reason='TP1', hold_days=3
    )
    mock_store.load_trades.return_value = [fake_trade, extra_trade]  # store 现在有 2 笔
    engine.refresh_trades_from_store()

    assert len(engine.trades) == 2, f"refresh 后应有 2 笔, 实际 {len(engine.trades)}"
    print(f"PASS: refresh_trades_from_store 从 store 加载 {len(engine.trades)} 笔")

def test_refresh_no_store_is_noop():
    """store=None 时 refresh_trades_from_store 应为 no-op (回测模式 persist=False)"""
    from app.sim_trader.engine import SimTraderEngine

    engine = SimTraderEngine()  # store=None, 纯回测模式
    assert engine.trades == [], "初始 trades 应为空 (store=None)"
    # 不应崩溃
    engine.refresh_trades_from_store()
    assert engine.trades == [], "store=None refresh 后 trades 应仍为空"
    print("PASS: refresh_trades_from_store 在 store=None 时为 no-op")

def test_reporter_uses_refresh():
    """reporter.py final_report 入口应调用 refresh_trades_from_store"""
    with open('app/sim_trader/reporter.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'refresh_trades_from_store' in content, "reporter.py 未调用 refresh_trades_from_store"
    print("PASS: reporter.py 引用 refresh_trades_from_store")

if __name__ == '__main__':
    test_refresh_trades_from_store_method_exists()
    test_refresh_loads_from_store()
    test_refresh_no_store_is_noop()
    test_reporter_uses_refresh()
    print("\nAll L4 fix verifications passed")
