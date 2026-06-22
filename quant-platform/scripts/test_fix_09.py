"""验证 #9 修复: adjust_for_gap 收到的是昨日 close,不是今日

Bug: sell_phase 调用 check_stops 时传 self._prev_snap,但 _prev_snap 在 sell_phase 末尾
     已被赋值为"今日 snapshot"(line 481)。结果: 当日 14:52 卖出时,adjust_for_gap 收到的是
     "今日的 close" 而非 "昨日的 close",除权跳空保护失效。

Fix: 新增 self._prev_day_snap 字段,sell_phase 调用 check_stops 时改传 _prev_day_snap;
     sell_phase 末尾在更新 _prev_snap 后同步更新 _prev_day_snap(今日尾盘 = 次日的"昨日")。
     intraday_monitor.py 在 record() 时也优先用 _prev_day_snap 兜底。
"""
import sys
import os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from datetime import date
from unittest.mock import MagicMock, patch


def test_sell_phase_uses_prev_day_snap():
    """sell_phase 应传 _prev_day_snap 给 check_stops,不是 _prev_snap"""
    from app.sim_trader.engine import SimTraderEngine
    from datetime import datetime as _dt

    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = []
    mock_store.load_equity_curve.return_value = []

    engine = SimTraderEngine(store=mock_store)

    yesterday_snap = {'000001': {'close': 10.0, 'open': 9.8, 'high': 10.2, 'low': 9.7}}
    today_snap = {'000001': {'close': 8.0, 'open': 7.5, 'high': 8.2, 'low': 7.3}}  # 大幅低开

    engine._prev_day_snap = yesterday_snap

    # sell_phase 内部用 datetime.now() 判定交易时段,mock 为 14:55
    fake_now = _dt(2025, 1, 6, 14, 55, 0)
    with patch('datetime.datetime') as mock_dt, \
         patch.object(engine, 'check_stops', return_value=[]) as mock_check:
        mock_dt.now.return_value = fake_now
        # datetime(...) 也要能正常构造(供 trading_dates 比较等)
        mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
        trading_dates = [date(2025, 1, 6)]
        engine.sell_phase(date(2025, 1, 6), today_snap, trading_dates)
        assert mock_check.called, "check_stops 未被调用(sell_phase 可能被交易时段守卫拦截)"
        call_kwargs = mock_check.call_args.kwargs
        assert 'prev_snap' in call_kwargs, \
            "check_stops 调用缺少 prev_snap 参数"
        assert call_kwargs['prev_snap'] == yesterday_snap, \
            f"check_stops 收到的 prev_snap 不是昨日的: {call_kwargs['prev_snap']}"
        print(f"OK sell_phase 传 _prev_day_snap 给 check_stops: {call_kwargs['prev_snap']}")


def test_prev_day_snap_updated_after_sell_phase():
    """sell_phase 结束后 _prev_day_snap 应被更新(今日尾盘 = 次日的"昨日")"""
    from app.sim_trader.engine import SimTraderEngine
    from datetime import datetime as _dt

    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = []
    mock_store.load_equity_curve.return_value = []

    engine = SimTraderEngine(store=mock_store)
    today_snap = {'000001': {'close': 10.0, 'open': 9.8, 'high': 10.2, 'low': 9.7}}
    trading_dates = [date(2025, 1, 6)]

    # mock 交易时段内,避免 sell_phase 因非交易时段直接 return
    fake_now = _dt(2025, 1, 6, 14, 55, 0)
    with patch('datetime.datetime') as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: _dt(*a, **kw)
        engine.sell_phase(date(2025, 1, 6), today_snap, trading_dates)

    assert engine._prev_day_snap == today_snap, \
        f"_prev_day_snap 未更新: {engine._prev_day_snap}"
    print(f"OK _prev_day_snap 已更新: {engine._prev_day_snap}")


def test_prev_day_snap_field_exists():
    """engine._prev_day_snap 字段应存在,且为 dict 类型"""
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
    assert hasattr(engine, '_prev_day_snap'), \
        "engine 缺 _prev_day_snap 字段"
    assert isinstance(engine._prev_day_snap, dict), \
        f"_prev_day_snap 应是 dict, 实际 {type(engine._prev_day_snap)}"
    print(f"OK engine._prev_day_snap 字段存在, 类型 = {type(engine._prev_day_snap).__name__}")


def test_intraday_monitor_uses_prev_day_snap_for_record():
    """intraday_monitor 在 record() 时应优先用 _prev_day_snap 兜底(模拟盘中场景)

    注: 盘中时 _prev_snap 还是昨日的(因为 sell_phase 还没跑),所以这个测试主要验证
    代码确实读了 _prev_day_snap,而不是只读 _prev_snap。如果将来 _prev_snap 的语义变了,
    _prev_day_snap 是稳定的"昨日"快照来源。
    """
    import inspect
    src = inspect.getsource(
        __import__('app.sim_trader.intraday_monitor', fromlist=['IntradayMonitor'])
    )
    # 必须存在 _prev_day_snap 引用
    assert '_prev_day_snap' in src, \
        "intraday_monitor.py 未引用 _prev_day_snap"
    print("OK intraday_monitor.py 已引用 _prev_day_snap")


if __name__ == '__main__':
    test_sell_phase_uses_prev_day_snap()
    test_prev_day_snap_updated_after_sell_phase()
    test_prev_day_snap_field_exists()
    test_intraday_monitor_uses_prev_day_snap_for_record()
    print("\n#9 修复验证全部通过")