"""LiveTraderStore 持仓 entry_date 回填测试

覆盖 2026-07-16 线上 bug:
live_positions.entry_date 全部 NULL → exit_monitor._calc_hold_days 当 None→today
→ max(1,0)=1 → "持仓第1天"永远不更新。

根因: _takeover_positions 接管 QMT 持仓时 entry_date=(existing or {}).get("entry_date"),
新持仓永远 None。

修复: store._get_earliest_buy_date 从 live_deals 回填最早买入日,
无成交记录兜底 date.today()。
"""
import os
import tempfile
from datetime import date, datetime

import pytest


@pytest.fixture
def store():
    """独立 DuckDB store, 含 live_deals 表用于 entry_date 回填测试"""
    from app.live_trader.store import LiveTraderStore
    from app.live_trader.config import LiveTraderConfig

    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    config = LiveTraderConfig(db_path=path)
    s = LiveTraderStore(config)
    yield s
    s.close()
    try:
        os.unlink(path)
    except OSError:
        pass


def test_earliest_buy_date_from_deals(store):
    """有买入成交 → 返回最早日期"""
    # 写入两笔成交: 07-02 和 07-10
    store._conn.execute(
        "INSERT INTO live_deals (trade_id, order_id, code, direction, "
        "filled_volume, filled_price, filled_amount, mode, traded_at) "
        "VALUES (1, 100, '600000.SH', 'buy', 100, 8.5, 850, 'live', ?)",
        [datetime(2026, 7, 2, 13, 35)]
    )
    store._conn.execute(
        "INSERT INTO live_deals (trade_id, order_id, code, direction, "
        "filled_volume, filled_price, filled_amount, mode, traded_at) "
        "VALUES (2, 101, '600000.SH', 'buy', 200, 8.6, 1720, 'live', ?)",
        [datetime(2026, 7, 10, 14, 0)]
    )
    # 再加一笔卖出,不应该影响结果
    store._conn.execute(
        "INSERT INTO live_deals (trade_id, order_id, code, direction, "
        "filled_volume, filled_price, filled_amount, mode, traded_at) "
        "VALUES (3, 102, '600000.SH', 'sell', 50, 8.8, 440, 'live', ?)",
        [datetime(2026, 7, 15, 10, 0)]
    )
    result = store._get_earliest_buy_date("600000.SH")
    assert result == date(2026, 7, 2), f"应返回最早买入日 07-02, 实得 {result}"


def test_earliest_buy_date_no_deals_returns_none(store):
    """无成交记录 → 返回 None"""
    result = store._get_earliest_buy_date("000001.SZ")
    assert result is None


def test_earliest_buy_date_no_buy_deals_returns_none(store):
    """只有卖出无买入 → 返回 None"""
    store._conn.execute(
        "INSERT INTO live_deals (trade_id, order_id, code, direction, "
        "filled_volume, filled_price, filled_amount, mode, traded_at) "
        "VALUES (4, 103, '000001.SZ', 'sell', 100, 10.0, 1000, 'live', ?)",
        [datetime(2026, 7, 15, 14, 0)]
    )
    result = store._get_earliest_buy_date("000001.SZ")
    assert result is None


def test_buy_date_is_date_not_datetime(store):
    """返回类型必须是 date (不是 datetime),否则后续比较会出错"""
    store._conn.execute(
        "INSERT INTO live_deals (trade_id, order_id, code, direction, "
        "filled_volume, filled_price, filled_amount, mode, traded_at) "
        "VALUES (5, 104, '600000.SH', 'buy', 100, 8.5, 850, 'live', ?)",
        [datetime(2026, 7, 2, 13, 35, 49)]
    )
    result = store._get_earliest_buy_date("600000.SH")
    assert isinstance(result, date), f"应返回 date, 实得 {type(result)}"
    assert not isinstance(result, datetime), "不应返回 datetime"