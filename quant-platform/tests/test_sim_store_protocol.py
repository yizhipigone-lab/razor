"""SimStore Protocol 一致性测试 — 3 个 adapter 都满足契约。"""
from datetime import date

from app.sim_trader.store_protocol import SimStore
from app.sim_trader.in_memory_store import InMemoryStore
from app.sim_trader.store import JsonSimStore


def test_in_memory_store_is_sim_store():
    assert isinstance(InMemoryStore(), SimStore)


def test_json_sim_store_is_sim_store(tmp_path):
    s = JsonSimStore(path=str(tmp_path / "state.json"))
    assert isinstance(s, SimStore)


def test_all_adapters_implement_full_interface():
    """所有 adapter 必须实现 Protocol 的全部 10 个方法(结构化检查)。"""
    required = [
        'load_state', 'save_state', 'load_positions', 'save_positions',
        'save_trade', 'load_trades', 'save_equity_point', 'load_equity_curve',
        'save_prev_day_snap', 'load_prev_day_snap',
    ]
    adapters = [InMemoryStore(), JsonSimStore(path="/tmp/_protocol_test.json")]
    for ad in adapters:
        for meth in required:
            assert hasattr(ad, meth), f"{type(ad).__name__} 缺方法 {meth}"


def test_load_equity_curve_uses_pos_key(tmp_path):
    """契约: load_equity_curve 返回的 dict 必有 'pos' 键。"""
    # JsonSimStore
    js = JsonSimStore(path=str(tmp_path / "j.json"))
    js.save_equity_point(date(2026, 3, 2), 1_000_000, 1_000_000, 0)
    assert "pos" in js.load_equity_curve()[0]

    # InMemoryStore
    im = InMemoryStore()
    im.save_equity_point(date(2026, 3, 2), 1_000_000, 1_000_000, 0)
    assert "pos" in im.load_equity_curve()[0]


def test_clear_all_exists_on_all_adapters(tmp_path):
    """clear_all() 为可选但测试/回放需要, 3 个 adapter 都应有。"""
    js = JsonSimStore(path=str(tmp_path / "j.json"))
    im = InMemoryStore()
    assert hasattr(js, "clear_all")
    assert hasattr(im, "clear_all")
    # SimTraderStore.clear_all 不依赖运行态 DB 实例即可验证方法存在
    from app.sim_trader.store import SimTraderStore
    assert hasattr(SimTraderStore, "clear_all")
