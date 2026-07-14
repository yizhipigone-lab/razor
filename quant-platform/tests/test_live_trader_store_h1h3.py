"""live_trader store H1 + H3 专项测试(2026-07-14)

H1: callback_handler._on_deal_callback 入口按 trade_id 幂等(防 QMT 重复回报双扣持仓)。
H3: _write_to_db / sync_terminal_write 失败必须向上抛(防静默丢数据)。

幂等门位置决策(2026-07-14):放在 callback_handler 入口,而非 store.apply_buy_fill 内,
因为 apply_buy_fill 自身不写 deals 表,查 deals 表查不到会失效。入口统一拦截后:
  - deal 不重复写
  - position 不重复加(因为 apply_buy_fill 不会被重复调)
  - 盈亏不重复算(因为 callback 链路整体 return)

不连真 QMT(mock),用 duckdb 临时文件。
"""
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def tmp_config(tmp_path):
    from app.live_trader.config import LiveTraderConfig
    return LiveTraderConfig(
        qmt_account_id="test_account",
        live_capital=100000.0,
        mode="dry-run",
        db_path=str(tmp_path / "test.duckdb"),
        lock_file=str(tmp_path / "test.lock"),
        restart_counter_file=str(tmp_path / "restart.json"),
        wal_path=str(tmp_path / "deals.wal"),
        preserved_codes=["159226.SZ", "159290.SZ"],
    )


@pytest.fixture
def store(tmp_config):
    from app.live_trader.store import LiveTraderStore
    s = LiveTraderStore(tmp_config)
    yield s
    s.close()


# ============================================================
# H1: callback_handler._on_deal_callback 入口按 trade_id 幂等
# ============================================================

class TestH1DealIdempotentAtCallback:
    """审计 H1:同一 trade_id 重复回报,callback_handler 入口整体跳过"""

    def test_get_deal_by_trade_id_returns_none_for_new_trade_id(self, store):
        """新 trade_id 在 deals 表查不到(预期入口放行)"""
        assert store.get_deal_by_trade_id(9999) is None

    def test_get_deal_by_trade_id_returns_none_for_zero(self, store):
        """trade_id=0 或 None 视为无效,直接放行(避免误杀第一笔成交)"""
        assert store.get_deal_by_trade_id(0) is None
        assert store.get_deal_by_trade_id(None) is None

    def test_first_deal_inserts_then_second_call_idempotent(self, store):
        """直接验证 store 层:写一次 deal 后,再查得到(供 callback 入口判定)"""
        # 第一次:写 deal(模拟 callback 写入)
        store._conn.execute(
            "INSERT INTO live_deals (trade_id, order_id, code, direction, "
            "filled_volume, filled_price, filled_amount, commission, mode, traded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [1001, 5001, "000001.SZ", "buy", 1000, 10.0, 10000.0, 5.0, "dry-run", datetime.now()]
        )
        # 第二次:同一 trade_id 能查到 → callback 入口会拦
        deal = store.get_deal_by_trade_id(1001)
        assert deal is not None
        assert deal["trade_id"] == 1001
        assert deal["code"] == "000001.SZ"
        assert deal["filled_volume"] == 1000


# ============================================================
# H3: _write_to_db / sync_terminal_write 异常向上抛
# ============================================================

class TestH3WriteToDbRaises:
    """审计 H3:_write_to_db 失败必须向上抛,不能静默吞"""

    def test_write_to_db_unknown_kind_raises(self, store):
        """未知的 kind 必须 raise(避免静默吞异常类型错误)"""
        with pytest.raises(ValueError, match="_write_to_db 未知 kind"):
            store._write_to_db("unknown_kind", {"code": "000001.SZ"})

    def test_write_to_db_invalid_sql_raises(self, store):
        """SQL 错误(如字段类型错)必须 raise,不能吞"""
        with pytest.raises(Exception):
            store._write_to_db("deal", {"trade_id": "not_a_number"})

    def test_sync_terminal_write_db_failure_raises(self, store):
        """sync_terminal_write:DB 写入失败必须 raise(WAL 已先写,可重启补)"""
        with patch.object(store, "_write_to_db", side_effect=RuntimeError("DB 磁盘满")):
            with pytest.raises(RuntimeError, match="DB 磁盘满"):
                store.sync_terminal_write("deal", {
                    "trade_id": 9999, "code": "000001.SZ",
                    "direction": "buy", "filled_volume": 100,
                })

    def test_sync_terminal_write_writes_wal_first(self, store, tmp_path):
        """sync_terminal_write:WAL 先写,DB 后写。DB 失败时 WAL 已存。"""
        wal_path = tmp_path / "deals.wal"
        assert not wal_path.exists()
        with patch.object(store, "_write_to_db", side_effect=RuntimeError("DB 失败")):
            try:
                store.sync_terminal_write("deal", {
                    "trade_id": 8888, "code": "000001.SZ",
                    "direction": "buy", "filled_volume": 100,
                })
            except RuntimeError:
                pass
        # WAL 应该已写
        assert wal_path.exists()
        content = wal_path.read_text(encoding="utf-8")
        assert "8888" in content  # WAL 里有 trade_id

    def test_flush_buffer_propagates_db_failure(self, store):
        """_flush_buffer:DB 写入失败向上抛(让 flusher loop catch)"""
        store._buffer.append(("deal", {
            "trade_id": 7777, "code": "000001.SZ",
            "direction": "buy", "filled_volume": 100,
        }))
        with patch.object(store, "_write_to_db", side_effect=RuntimeError("DB 写入失败")):
            with pytest.raises(RuntimeError, match="DB 写入失败"):
                store._flush_buffer()

    def test_legacy_silent_swalllow_removed(self, store):
        """验证 _write_to_db 不再有 catch+log 吞异常模式(H3 审计要求 raise)"""
        import inspect
        src = inspect.getsource(store._write_to_db)
        assert "except Exception" not in src, \
            "_write_to_db 不应再有 catch+log 吞异常模式(H3 审计要求 raise)"