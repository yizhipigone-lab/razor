"""InMemoryStore 单元测试 — 行为对齐 JsonSimStore。"""
from datetime import date

from app.sim_trader.in_memory_store import InMemoryStore
from app.sim_trader.models import Position, Trade


def _make_pos(code="000001", entry=10.0):
    pos = Position(code=code, entry_date=date(2026, 3, 2),
                   entry_price=entry, shares=1000, cost=10_000,
                   strategy_name="QUANTQQ", entry_time="14:54")
    pos.peak_price = 12.5
    pos.remaining_shares = 2500
    pos.tp1_triggered = True
    return pos


def test_state_roundtrip():
    s = InMemoryStore()
    s.save_state(950_000, 3, date(2026, 3, 5), 7)
    st = s.load_state()
    assert st['cash'] == 950_000
    assert st['consecutive_losses'] == 3
    assert st['pause_until'] == date(2026, 3, 5)
    assert st['trade_count'] == 7


def test_positions_roundtrip_preserves_fields():
    s = InMemoryStore()
    s.save_positions({"000001": _make_pos()})
    loaded = s.load_positions()
    p = loaded["000001"]
    assert p.entry_price == 10.0
    assert p.peak_price == 12.5
    assert p.remaining_shares == 2500
    assert p.tp1_triggered is True
    assert p.is_active is True
    assert p.strategy_name == "QUANTQQ"


def test_trades_roundtrip():
    s = InMemoryStore()
    t = Trade(code="000001", entry_date=date(2026, 3, 2),
              exit_date=date(2026, 3, 3), entry_price=10.0,
              exit_price=11.0, shares=1000, return_pct=10.0,
              profit_amount=1000.0, exit_reason="TP1", hold_days=1)
    s.save_trade(t)
    loaded = s.load_trades()
    assert len(loaded) == 1
    assert loaded[0].code == "000001"
    assert loaded[0].exit_reason == "TP1"
    assert loaded[0].return_pct == 10.0


def test_equity_curve_dedup_and_pos_key():
    s = InMemoryStore()
    s.save_equity_point(date(2026, 3, 2), 1_000_000, 1_000_000, 0)
    s.save_equity_point(date(2026, 3, 2), 1_050_000, 900_000, 2, source="record")
    s.save_equity_point(date(2026, 3, 3), 1_060_000, 800_000, 3)
    ec = s.load_equity_curve()
    assert len(ec) == 2                       # 同日去重
    assert ec[0]["equity"] == 1_050_000       # 保留最新
    assert "pos" in ec[0]                     # 统一 pos 键
    assert ec[0]["source"] == "record"


def test_prev_day_snap_roundtrip():
    s = InMemoryStore()
    snap = {"000001": {"close": 10.5}}
    s.save_prev_day_snap(snap)
    assert s.load_prev_day_snap() == snap


def test_clear_all_resets_everything():
    s = InMemoryStore()
    s.save_positions({"000001": _make_pos()})
    s.save_equity_point(date(2026, 3, 2), 1_000_000, 1_000_000, 1)
    s.save_state(950_000, 1, None, 3)
    s.clear_all()
    assert s.load_positions() == {}
    assert s.load_equity_curve() == []
    assert s.load_state()['cash'] == 1_000_000  # 回到默认本金
    assert s.load_state()['trade_count'] == 0


def test_engine_works_with_in_memory_store():
    """端到端: SimTraderEngine 注入 InMemoryStore 可正常初始化 + record。"""
    from app.sim_trader.engine import SimTraderEngine
    s = InMemoryStore()
    s.save_state(1_000_000, 0, None, 0)  # 干净初始状态
    engine = SimTraderEngine(store=s)
    engine.cash = 1_000_000
    engine.record(date(2026, 3, 3), snapshot={})
    ec = s.load_equity_curve()
    assert len(ec) == 1
    assert ec[0]["pos"] == 0
