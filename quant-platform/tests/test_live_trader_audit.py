"""审计 C-T1/C-T2/H-T1 测试

- C-T1: refresh_quotes 持久化 last_close (CASE WHEN > 0 逻辑)
- C-T2: /live/positions 返回契约含 last_close + today_buy_volume
- H-T1: today_buy_volume 只算今日买入,不算昨日

运行: pytest tests/test_live_trader_audit.py -v
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ===== Fixtures =====

@pytest.fixture
def tmp_config(tmp_path):
    """临时配置(不连真 QMT,不连 meta.db)"""
    from app.live_trader.config import LiveTraderConfig
    return LiveTraderConfig(
        qmt_account_id="test_account",
        live_capital=100000.0,
        mode="dry-run",
        db_path=str(tmp_path / "test_audit.duckdb"),
        lock_file=str(tmp_path / "test_audit.lock"),
        restart_counter_file=str(tmp_path / "test_audit.json"),
        wal_path=str(tmp_path / "test_audit.wal"),
        preserved_codes=["159226.SZ", "159290.SZ"],
    )


@pytest.fixture
def store(tmp_config):
    """临时 LiveTraderStore(独立 DuckDB 文件,不连 meta.db)"""
    from app.live_trader.store import LiveTraderStore
    s = LiveTraderStore(tmp_config)
    yield s
    s.close()


# ===== C-T1: refresh_quotes 持久化 last_close =====

def test_refresh_quotes_writes_last_close(store):
    """C-T1: refresh_quotes 用 CASE WHEN ? > 0 THEN ? ELSE last_close END 存 last_close

    验证:
    1. lastClose=9.5 时,last_close 被写入 9.5
    2. lastClose=0 时,不覆盖已有 last_close(保持 9.5)
    """
    # 插入一条持仓(无 last_close)
    store.upsert_position({
        "code": "600000.SH", "volume": 100, "can_use_volume": 100,
        "frozen_volume": 0, "pending_buy_volume": 0,
        "avg_cost": 10.0, "last_price": 10.0, "managed": True,
    })

    # Step 1: lastClose=9.5,应写入 last_close
    updated = store.refresh_quotes({"600000.SH": {"lastPrice": 10.0, "lastClose": 9.5}})
    assert updated == 1, "应更新 1 条持仓"

    pos = store.get_position("600000.SH")
    assert pos["last_close"] == 9.5, f"last_close 应为 9.5,实际 {pos['last_close']}"

    # Step 2: lastClose=0,不应覆盖已有 last_close
    store.refresh_quotes({"600000.SH": {"lastPrice": 10.5, "lastClose": 0}})
    pos = store.get_position("600000.SH")
    assert pos["last_close"] == 9.5, (
        f"lastClose=0 时 last_close 应保持 9.5,实际 {pos['last_close']}"
    )
    # 但 last_price 仍应更新
    assert pos["last_price"] == 10.5, f"last_price 应为 10.5,实际 {pos['last_price']}"


# ===== M2(2026-07-19): apply_buy_fill 缺价不污染浮盈 =====

def test_apply_buy_fill_no_price_does_not_inflate_float_profit(store):
    """M2: apply_buy_fill 遇 QMT 回报无价(filled_price=0)时,
    avg_cost 留 NULL 不冒充 0;后续 refresh_quotes 不该算出 (last-0)*volume 的虚高浮盈。"""
    # 模拟 QMT 成交回报缺价(filled_price=0)
    store.apply_buy_fill("600000.SH", filled_volume=100, filled_price=0)

    pos = store.get_position("600000.SH")
    assert pos["volume"] == 100
    # avg_cost 不该有正值(成本未确认,留 NULL)
    assert not (pos.get("avg_cost") or 0) > 0, "filled_price=0 时 avg_cost 不该冒充正值"

    # refresh_quotes 拿到真实 last=10.0
    store.refresh_quotes({"600000.SH": {"lastPrice": 10.0, "lastClose": 9.5}})

    pos = store.get_position("600000.SH")
    # 关键:float_profit 不该是 (10-0)*100=1000 的虚高值
    assert pos["float_profit"] == 0.0, (
        f"成本未确认时 float_profit 应为 0,实际 {pos['float_profit']}(不该虚高)"
    )
    assert pos["profit_rate"] == 0.0
    # market_value 仍按现价算(与成本无关)
    assert pos["market_value"] == 1000.0, f"市值仍按现价×数量算,实际 {pos['market_value']}"
    assert pos["last_price"] == 10.0


# ===== C-T2: /live/positions 契约(last_close + today_buy_volume) =====

def test_positions_contract(store):
    """C-T2: /live/positions 返回的每条持仓含 last_close + today_buy_volume 字段

    契约锁死,防未来改 SELECT 漏字段。
    """
    from app.live_trader.main import _state, app
    from fastapi.testclient import TestClient

    # 插入持仓 + 刷新行情(设 last_close)
    store.upsert_position({
        "code": "600000.SH", "volume": 100, "can_use_volume": 100,
        "frozen_volume": 0, "pending_buy_volume": 0,
        "avg_cost": 10.0, "last_price": 10.5, "managed": True,
    })
    store.refresh_quotes({"600000.SH": {"lastPrice": 10.5, "lastClose": 10.0}})

    # mock _state(不触发 lifespan,手动注入 store)
    old_store = _state.get("store")
    _state["store"] = store
    try:
        client = TestClient(app)
        resp = client.get("/live/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1, "应至少返回 1 条持仓"

        pos = data[0]
        # 契约:必须有 last_close 字段
        assert "last_close" in pos, "持仓缺失 last_close 字段(契约违规)"
        # 契约:必须有 today_buy_volume 字段
        assert "today_buy_volume" in pos, "持仓缺失 today_buy_volume 字段(契约违规)"
        # last_close 值正确
        assert pos["last_close"] == 10.0, (
            f"last_close 应为 10.0,实际 {pos['last_close']}"
        )
    finally:
        if old_store is not None:
            _state["store"] = old_store
        else:
            _state.pop("store", None)


# ===== H-T1/C1: today_buy_volume 只算今日买入 =====

def test_today_buy_volume_from_deals(store):
    """H-T1/C1: today_buy_volume 从 live_deals 算,只算今日买入,不算昨日

    场景:
    - 今日买入 100 股
    - 昨日买入 200 股
    - 断言 today_buy_volume == 100
    """
    from app.live_trader.main import _state, app
    from fastapi.testclient import TestClient

    # 插入持仓
    store.upsert_position({
        "code": "600000.SH", "volume": 300, "can_use_volume": 300,
        "frozen_volume": 0, "pending_buy_volume": 0,
        "avg_cost": 10.0, "last_price": 10.5, "managed": True,
    })

    # 插入 deals:今日买入 100 + 昨日买入 200
    now = datetime.now()
    store.sync_terminal_write("deal", {
        "trade_id": 9001, "order_id": 1001, "code": "600000.SH",
        "direction": "buy", "filled_volume": 100,
        "filled_price": 10.0, "filled_amount": 1000.0,
        "commission": 0.0, "mode": "dry-run",
        "traded_at": now,
    })
    store.sync_terminal_write("deal", {
        "trade_id": 9002, "order_id": 1002, "code": "600000.SH",
        "direction": "buy", "filled_volume": 200,
        "filled_price": 10.0, "filled_amount": 2000.0,
        "commission": 0.0, "mode": "dry-run",
        "traded_at": now - timedelta(days=1),
    })

    # mock _state
    old_store = _state.get("store")
    _state["store"] = store
    try:
        client = TestClient(app)
        resp = client.get("/live/positions")
        assert resp.status_code == 200
        data = resp.json()
        pos = [p for p in data if p["code"] == "600000.SH"]
        assert len(pos) == 1, f"应返回 1 条 600000.SH 持仓,实际 {len(pos)}"
        assert pos[0]["today_buy_volume"] == 100, (
            f"today_buy_volume 应为 100(只算今日),实际 {pos[0]['today_buy_volume']}"
        )
    finally:
        if old_store is not None:
            _state["store"] = old_store
        else:
            _state.pop("store", None)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
