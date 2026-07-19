"""手工下单功能端点测试(2026-07-18 T2/T3/T4)

覆盖:
- T2 /live/order: dry-run 硬拒 / kill_switch 拒 / price_type_key 映射 / 市价单金额估算 fail-closed
- T3 /live/order/cancel: dry-run 拒 / 成功 / 未找到 / kill_switch 放行
- T4 /live/positions/sync: 单 code 过滤 / QMT 未连接拒

TestClient 不开 lifespan(避免真连 QMT),直接注入 _state mock。
运行:pytest tests/test_manual_order.py -v
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

import app.live_trader.main as main_mod
import app.live_trader.auth as auth_mod  # 阶段0b: _is_local 抽到 auth,_require_admin 内部调 auth._is_local


@pytest.fixture
def client():
    # 不用 with(不开 lifespan);_require_admin 依赖本机 IP,测试里打补丁
    # 阶段0b: _is_local 已抽到 auth.py, _require_admin 内部调 auth._is_local, 故 patch auth 模块
    auth_mod._is_local = lambda request: True
    return TestClient(main_mod.app, raise_server_exceptions=False)


@pytest.fixture
def state():
    """每个测试前清空并注入基础 _state,测后清理"""
    saved = dict(main_mod._state)
    main_mod._state.clear()
    yield main_mod._state
    main_mod._state.clear()
    main_mod._state.update(saved)


def _base_mocks(is_live=True, ks_active=False, qmt_connected=True):
    config = MagicMock()
    rs = MagicMock()
    rs.is_live.return_value = is_live
    ks = MagicMock()
    ks.is_active.return_value = ks_active
    qmt = MagicMock()
    qmt.connected = qmt_connected
    executor = MagicMock()
    executor.execute.return_value = {"ok": True, "status": "submitted", "order_id": 123}
    return {"config": config, "runtime_state": rs, "kill_switch": ks,
            "qmt": qmt, "executor": executor, "audit": MagicMock()}


# ===== T2 /live/order =====

def test_place_order_dry_run_rejected(client, state):
    """dry-run 模式硬拒 403(2026-07-18 行为变更)"""
    state.update(_base_mocks(is_live=False))
    r = client.post("/live/order", json={
        "code": "600000", "direction": "buy", "volume": 100, "price": 10.0})
    assert r.status_code == 403
    assert "dry-run" in r.json()["detail"]


def test_place_order_kill_switch_blocked(client, state):
    state.update(_base_mocks(ks_active=True))
    r = client.post("/live/order", json={
        "code": "600000", "direction": "buy", "volume": 100, "price": 10.0})
    assert r.status_code == 403
    assert "kill switch" in r.json()["detail"]


def test_place_order_backward_compat(client, state):
    """无 price_type_key:price_type 透传,行为与旧版一致"""
    mocks = _base_mocks()
    state.update(mocks)
    r = client.post("/live/order", json={
        "code": "600000", "direction": "buy", "volume": 100, "price": 10.5, "price_type": 11})
    assert r.status_code == 200
    intent = mocks["executor"].execute.call_args[0][0]
    assert intent.price_type == 11
    assert intent.price == 10.5


def test_place_order_price_type_key_mapping(client, state):
    """price_type_key 映射:深市代码选沪五档 → 降级对手最优 + warning 透传"""
    mocks = _base_mocks()
    state.update(mocks)
    mocks["qmt"].get_realtime_quotes.return_value = {"000001.SZ": {"lastPrice": 12.3}}
    r = client.post("/live/order", json={
        "code": "000001", "direction": "buy", "volume": 100,
        "price_type_key": "sh5_cancel"})
    assert r.status_code == 200
    intent = mocks["executor"].execute.call_args[0][0]
    assert intent.price_type == 44  # 降级对手最优
    assert r.json().get("price_type_warning")
    assert "降级" in r.json()["price_type_warning"]


def test_place_order_market_price_backfill(client, state):
    """市价单 price=0 → 用 QMT 实时价回填估算基准(HIGH-1 修复)"""
    mocks = _base_mocks()
    state.update(mocks)
    mocks["qmt"].get_realtime_quotes.return_value = {"600519.SH": {"lastPrice": 1700.0}}
    r = client.post("/live/order", json={
        "code": "600519", "direction": "buy", "volume": 100,
        "price_type_key": "peer_best"})
    assert r.status_code == 200
    intent = mocks["executor"].execute.call_args[0][0]
    assert intent.price_type == 44
    assert intent.price == 1700.0  # 回填实时价,闸门按此估算金额


def test_place_order_market_no_quote_fail_closed(client, state):
    """市价单取不到行情 → fail-closed 503,不放行"""
    mocks = _base_mocks()
    state.update(mocks)
    mocks["qmt"].get_realtime_quotes.return_value = {}
    r = client.post("/live/order", json={
        "code": "600519", "direction": "buy", "volume": 100,
        "price_type_key": "peer_best"})
    assert r.status_code == 503
    assert "fail-closed" in r.json()["detail"]
    mocks["executor"].execute.assert_not_called()


def test_place_order_unknown_price_type_key(client, state):
    state.update(_base_mocks())
    r = client.post("/live/order", json={
        "code": "600000", "direction": "buy", "volume": 100,
        "price_type_key": "not_a_key"})
    assert r.status_code == 400


def test_place_order_limit_price_zero_rejected(client, state):
    """限价单 price=0 → 400 拒绝(后端兜底,防 0 元限价进 QMT)"""
    state.update(_base_mocks())
    r = client.post("/live/order", json={
        "code": "600000", "direction": "buy", "volume": 100, "price": 0})
    assert r.status_code == 400
    assert "限价" in r.json()["detail"]


# ===== T3 /live/order/cancel =====

def test_cancel_dry_run_rejected(client, state):
    state.update(_base_mocks(is_live=False))
    r = client.post("/live/order/cancel", json={"order_id": 123})
    assert r.status_code == 403


def test_cancel_success(client, state):
    mocks = _base_mocks()
    mocks["qmt"].cancel_order.return_value = 0
    state.update(mocks)
    r = client.post("/live/order/cancel", json={"order_id": 123})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    mocks["qmt"].cancel_order.assert_called_once_with(123)


def test_cancel_not_found(client, state):
    mocks = _base_mocks()
    mocks["qmt"].cancel_order.return_value = -3
    state.update(mocks)
    r = client.post("/live/order/cancel", json={"order_id": 999})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "未找到" in r.json()["reason"]


def test_cancel_allowed_when_kill_switch_active(client, state):
    """kill_switch 激活时撤单放行(减风险操作)"""
    mocks = _base_mocks(ks_active=True)
    mocks["qmt"].cancel_order.return_value = 0
    state.update(mocks)
    r = client.post("/live/order/cancel", json={"order_id": 123})
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ===== T4 /live/positions/sync =====

@pytest.fixture
def store(tmp_path):
    from app.live_trader.config import LiveTraderConfig
    from app.live_trader.store import LiveTraderStore
    cfg = LiveTraderConfig(
        qmt_account_id="test", live_capital=100000.0, mode="dry-run",
        db_path=str(tmp_path / "t.duckdb"), lock_file=str(tmp_path / "t.lock"),
        restart_counter_file=str(tmp_path / "t.json"), wal_path=str(tmp_path / "t.wal"),
        preserved_codes=["159226.SZ", "159290.SZ"],
    )
    s = LiveTraderStore(cfg)
    yield s, cfg
    s.close()


def test_sync_positions_single_code_filter(client, state, store):
    """单 code 同步:QMT 有 2 只持仓,只 upsert 指定那只"""
    s, cfg = store
    qmt = MagicMock()
    qmt.connected = True
    qmt.query_positions.return_value = [
        {"code": "600000.SH", "volume": 100, "can_use_volume": 100,
         "frozen_volume": 0, "avg_cost": 10.0, "last_price": 10.5},
        {"code": "000001.SZ", "volume": 200, "can_use_volume": 200,
         "frozen_volume": 0, "avg_cost": 5.0, "last_price": 5.2},
    ]
    state.update({"store": s, "qmt": qmt, "config": cfg, "audit": MagicMock()})
    r = client.post("/live/positions/sync", json={"code": "600000.SH"})
    assert r.status_code == 200
    assert r.json()["synced"] == 1
    assert r.json()["codes"] == ["600000.SH"]
    assert s.get_position("600000.SH")["volume"] == 100
    assert s.get_position("000001.SZ") is None  # 未被误同步


def test_sync_positions_preserves_local_fields(client, state, store):
    """同步保留本地扩展字段(peak_price/sell_count/entry_date)"""
    from datetime import date
    s, cfg = store
    s.upsert_position({
        "code": "600000.SH", "volume": 50, "can_use_volume": 50, "frozen_volume": 0,
        "pending_buy_volume": 0, "avg_cost": 9.0, "last_price": 9.5, "market_value": 475,
        "float_profit": 25, "profit_rate": 5.0, "peak_price": 12.34, "sell_count": 2,
        "entry_date": date(2026, 7, 1), "managed": True, "strategy_name": "QUANTQQ",
    })
    qmt = MagicMock()
    qmt.connected = True
    qmt.query_positions.return_value = [
        {"code": "600000.SH", "volume": 100, "can_use_volume": 100,
         "frozen_volume": 0, "avg_cost": 10.0, "last_price": 10.5},
    ]
    state.update({"store": s, "qmt": qmt, "config": cfg, "audit": MagicMock()})
    r = client.post("/live/positions/sync", json={"code": "600000.SH"})
    assert r.status_code == 200
    pos = s.get_position("600000.SH")
    assert pos["volume"] == 100  # 数量被 QMT 刷新
    assert pos["peak_price"] == 12.34  # 本地扩展字段保留
    assert pos["sell_count"] == 2
    assert str(pos["entry_date"])[:10] == "2026-07-01"
    assert pos["strategy_name"] == "QUANTQQ"


def test_sync_positions_qmt_not_connected(client, state, store):
    s, cfg = store
    qmt = MagicMock()
    qmt.connected = False
    state.update({"store": s, "qmt": qmt, "config": cfg, "audit": MagicMock()})
    r = client.post("/live/positions/sync", json={"code": "600000.SH"})
    assert r.status_code == 503
