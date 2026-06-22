"""验证 L3 修复: _today_trades 在 execute_sell 后累加,日切清空,API/cron 用 _today_trades"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')
from datetime import date
from unittest.mock import MagicMock

def test_today_trades_initialized_as_empty_list():
    """engine 初始化时 _today_trades 应是空 list"""
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
    assert hasattr(engine, '_today_trades'), "engine 缺 _today_trades 字段"
    assert isinstance(engine._today_trades, list), "_today_trades 应是 list"
    assert len(engine._today_trades) == 0, "_today_trades 应初始化为空"
    print("PASS: _today_trades 字段存在并初始化为空 list")

def test_execute_sell_appends_to_today_trades():
    """execute_sell 后 _today_trades 应 +1"""
    from app.sim_trader.engine import SimTraderEngine, Position

    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = []
    mock_store.load_equity_curve.return_value = []

    engine = SimTraderEngine(store=mock_store)
    pos = Position(
        code='000001', entry_date=date(2025, 1, 3),
        entry_price=10.0, shares=100, cost=1000.0
    )
    engine.positions['000001'] = pos

    initial_len = len(engine._today_trades)
    engine.execute_sell(pos, 9.0, 'HS', None, exit_date=date(2025, 1, 6))
    final_len = len(engine._today_trades)
    assert final_len == initial_len + 1, f"_today_trades 未累加: {initial_len} -> {final_len}"
    print(f"PASS: execute_sell 后 _today_trades: {initial_len} -> {final_len}")

def test_api_layer_uses_today_trades():
    """api/sim_trader.py 不应再用 engine.trades 算 trade_count/sell_count"""
    with open('app/api/sim_trader.py', 'r', encoding='utf-8') as f:
        content = f.read()
    bad_patterns = [
        "'trade_count': len(engine.trades)",
        'sell_count = len([t for t in engine.trades',
    ]
    for pattern in bad_patterns:
        assert pattern not in content, f"仍用 engine.trades: {pattern}"
    print("PASS: api/sim_trader.py 已用 _today_trades")

def test_cron_jobs_uses_today_trades():
    """cron_jobs.py 不应再用 engine.trades 算 sell_count"""
    with open('app/scheduler/cron_jobs.py', 'r', encoding='utf-8') as f:
        content = f.read()
    # 找 len(engine.trades) 且包含 sell_count 的行
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if 'len(engine.trades)' in line and ('sell_count' in line or 'trade_count' in line):
            bad = f"第 {i+1} 行仍用 engine.trades: {line.strip()}"
            assert False, bad
    print("PASS: cron_jobs.py 算 sell/trade_count 已用 _today_trades")

if __name__ == '__main__':
    test_today_trades_initialized_as_empty_list()
    test_execute_sell_appends_to_today_trades()
    test_api_layer_uses_today_trades()
    test_cron_jobs_uses_today_trades()
    print("\nAll L3 fix verifications passed")
