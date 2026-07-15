"""P0 测试: SimTraderEngine.total_equity / record / _validate_loaded_state

覆盖 CLAUDE.md 标记的"净值失真"历史 bug 根因:
- total_equity 三档估值兜底: snapshot当前价 > pos.current_price > entry_price
- record 行情覆盖率检测 + source=partial 标记
- _validate_loaded_state 首条 equity > 1.10× 本金拒收
"""

import pytest
from datetime import date
from app.sim_trader.engine import SimTraderEngine
from app.sim_trader.models import Position
from app.sim_trader.in_memory_store import InMemoryStore


def _pos(code, entry_price, shares, current_price=0.0, cost=None):
    """Helper: 创建 Position, cost 默认 = entry_price * shares。"""
    return Position(
        code=code, entry_date=date(2026, 7, 10),
        entry_price=entry_price, shares=shares,
        cost=cost if cost is not None else entry_price * shares,
        current_price=current_price,
    )


@pytest.fixture
def engine():
    """空引擎, 100 万本金, 无持仓。"""
    store = InMemoryStore()
    eg = SimTraderEngine(store=store)
    eg.cash = 1000000.0
    return eg


@pytest.fixture
def engine_with_pos(engine):
    """持一股 000001, entry_price=10, 1000shares, current_price=11, cost=10000。"""
    engine.cash = 900000.0
    engine.positions["000001"] = _pos("000001", 10.0, 1000, current_price=11.0, cost=10000.0)
    return engine


# ── total_equity ──────────────────────────────────────────────

def test_total_equity_no_positions(engine):
    assert engine.total_equity({}) == 1_000_000.0


def test_total_equity_snapshot_price_priority(engine_with_pos):
    """快照有价: 优先用 snapshot close。"""
    eq = engine_with_pos.total_equity({"000001": {"close": 12.5}})
    assert eq == pytest.approx(900000.0 + 1000 * 12.5)


def test_total_equity_fallback_to_current_price(engine_with_pos):
    """快照 close=0: 降级到 pos.current_price=11。"""
    eq = engine_with_pos.total_equity({"000001": {"close": 0}})
    assert eq == pytest.approx(900000.0 + 1000 * 11.0)


def test_total_equity_fallback_to_entry_price(engine_with_pos):
    """快照无该股: 兜底到 entry_price=10。"""
    engine_with_pos.positions["000001"].current_price = 0
    eq = engine_with_pos.total_equity({})
    assert eq == pytest.approx(900000.0 + 1000 * 10.0)


def test_total_equity_mixed(engine):
    """混合: 有价+缺价同时存在。"""
    engine.cash = 800000.0
    engine.positions["000001"] = _pos("000001", 10.0, 1000, current_price=11.0)
    engine.positions["000002"] = _pos("000002", 20.0, 500, current_price=0.0)
    snapshot = {"000001": {"close": 13.0}}
    eq = engine.total_equity(snapshot)
    assert eq == pytest.approx(800000.0 + 1000 * 13.0 + 500 * 20.0)


# ── equity_price_coverage ─────────────────────────────────────

def test_coverage_full(engine_with_pos):
    covered, active = engine_with_pos.equity_price_coverage({"000001": {"close": 10.5}})
    assert covered == 1 and active == 1


def test_coverage_partial(engine):
    engine.positions["000001"] = _pos("000001", 10.0, 1000)
    engine.positions["000002"] = _pos("000002", 20.0, 500)
    covered, active = engine.equity_price_coverage({"000001": {"close": 10.5}})
    assert covered == 1 and active == 2


def test_coverage_zero(engine_with_pos):
    covered, active = engine_with_pos.equity_price_coverage({})
    assert covered == 0 and active == 1


# ── record ────────────────────────────────────────────────────

def test_record_source_partial(engine):
    """行情不全: source=partial。"""
    engine.positions["000001"] = _pos("000001", 10.0, 1000, current_price=10.0)
    engine.record(date(2026, 7, 15), {})
    assert engine.equity_curve[-1]["source"] == "partial"


def test_record_source_full(engine_with_pos):
    """行情全: source=record。"""
    engine_with_pos.record(date(2026, 7, 15), {"000001": {"close": 12.0}})
    assert engine_with_pos.equity_curve[-1]["source"] == "record"


def test_record_updates_current_price(engine_with_pos):
    """record 后 current_price 更新为 snapshot close。"""
    engine_with_pos.record(date(2026, 7, 15), {"000001": {"close": 13.0}})
    assert engine_with_pos.positions["000001"].current_price == 13.0


def test_record_keeps_price_on_zero_close(engine_with_pos):
    """close=0: 保留原 current_price。"""
    old = engine_with_pos.positions["000001"].current_price
    engine_with_pos.record(date(2026, 7, 15), {"000001": {"close": 0}})
    assert engine_with_pos.positions["000001"].current_price == old


# ── _validate_loaded_state ────────────────────────────────────

def test_validate_rejects_contaminated(engine):
    """首条 equity > 1.10× INITIAL_CAPITAL(1,000,000): 拒收。"""
    engine.equity_curve = [
        {"date": "2026-07-10", "equity": 2_000_000.0, "cash": 500000.0,
         "positions": 1, "source": "record"},
    ]
    engine._validate_loaded_state()
    assert engine.equity_curve == []


def test_validate_accepts_normal(engine):
    """首条 equity ≤ 1.10×: 保留。"""
    engine.equity_curve = [
        {"date": "2026-07-10", "equity": 1_050_000.0, "cash": 950000.0,
         "positions": 1, "source": "record"},
    ]
    engine._validate_loaded_state()
    assert len(engine.equity_curve) == 1


def test_validate_empty_noop(engine):
    engine.equity_curve = []
    engine._validate_loaded_state()
    assert engine.equity_curve == []
