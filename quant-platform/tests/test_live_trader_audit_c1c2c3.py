"""全项目审计 CRITICAL 回归测试(2026-07-15)

C1: apply_*_fill 真实成交(trade_id≠None)持仓必须更新, 重复回报幂等不双扣。
    旧版 bug: sync_terminal_write 先插 deal → apply 查 deal 存在 → return → 持仓永不更新。
C2: release_pending_buy 只释放冻结, 不动 volume(废单不膨胀持仓)。
C3: CallbackHandler 注入 audit。
不连真 QMT, 用 duckdb 临时文件。
"""
import os
import sys
from datetime import datetime, date
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def tmp_config(tmp_path):
    from app.live_trader.config import LiveTraderConfig
    return LiveTraderConfig(
        qmt_account_id="test_account", live_capital=100000.0, mode="dry-run",
        db_path=str(tmp_path / "test.duckdb"), lock_file=str(tmp_path / "test.lock"),
        restart_counter_file=str(tmp_path / "restart.json"),
        wal_path=str(tmp_path / "deals.wal"), preserved_codes=[],
    )


@pytest.fixture
def store(tmp_config):
    from app.live_trader.store import LiveTraderStore
    s = LiveTraderStore(tmp_config)
    yield s
    s.close()


def _insert_position(store, code, volume, pending=0):
    store._conn.execute(
        "INSERT INTO live_positions (code, volume, can_use_volume, frozen_volume, "
        "pending_buy_volume, avg_cost, last_price, market_value, float_profit, "
        "profit_rate, peak_price, sell_count, entry_date, managed, strategy_name, "
        "tp_triggered) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [code, volume, volume, 0, pending, 10.0, 10.0, volume * 10.0, 0.0, 0.0,
         10.0, 0, date(2026, 7, 10), True, "test", "[]"]
    )


def _insert_deal(store, trade_id, code, direction, volume):
    """模拟 sync_terminal_write("deal") 插入成交(先于 apply_*_fill, 触发旧 bug 的顺序)。"""
    store.sync_terminal_write("deal", {
        "trade_id": trade_id, "order_id": 5001, "code": code, "direction": direction,
        "filled_volume": volume, "filled_price": 10.0, "filled_amount": volume * 10.0,
        "commission": 5.0, "mode": "live", "traded_at": datetime.now(),
    })


def _get_volume(store, code):
    return store._conn.execute(
        "SELECT volume, pending_buy_volume FROM live_positions WHERE code=?", [code]
    ).fetchone()


# ── C1: 真实成交持仓必须更新(旧 bug: 永不更新) ──────────────

class TestC1RealFillUpdatesPosition:
    def test_sell_fill_decrements_volume_after_deal_inserted(self, store):
        """C1 核心: sync_terminal_write 先插 deal, apply_sell_fill(trade_id) 仍递减持仓。"""
        _insert_position(store, "000001.SZ", volume=1000)
        _insert_deal(store, 1001, "000001.SZ", "sell", 300)
        # 旧 bug: 此处 return, volume 不变; 修复后: volume 递减
        store.apply_sell_fill("000001.SZ", 300, trade_id=1001)
        vol, _ = _get_volume(store, "000001.SZ")
        assert vol == 700  # 1000 - 300, 不是 1000

    def test_buy_fill_increments_volume_after_deal_inserted(self, store):
        """C1: 真实买入成交后 volume 递增(旧 bug: 永不递增)。"""
        _insert_position(store, "000001.SZ", volume=500, pending=300)
        _insert_deal(store, 1002, "000001.SZ", "buy", 300)
        store.apply_buy_fill("000001.SZ", 300, trade_id=1002)
        vol, pending = _get_volume(store, "000001.SZ")
        assert vol == 800  # 500 + 300, 不是 500
        assert pending == 0  # 释放预扣 300

    def test_duplicate_sell_fill_idempotent_no_double_decrement(self, store):
        """C1 幂等: 重复回报同 trade_id 不双扣。"""
        _insert_position(store, "000001.SZ", volume=1000)
        _insert_deal(store, 1003, "000001.SZ", "sell", 300)
        store.apply_sell_fill("000001.SZ", 300, trade_id=1003)
        store.apply_sell_fill("000001.SZ", 300, trade_id=1003)  # 重复
        vol, _ = _get_volume(store, "000001.SZ")
        assert vol == 700  # 只扣一次

    def test_duplicate_buy_fill_idempotent_no_double_increment(self, store):
        """C1 幂等: 重复买入回报同 trade_id 不双加。"""
        _insert_position(store, "000001.SZ", volume=500, pending=300)
        _insert_deal(store, 1004, "000001.SZ", "buy", 300)
        store.apply_buy_fill("000001.SZ", 300, trade_id=1004)
        store.apply_buy_fill("000001.SZ", 300, trade_id=1004)  # 重复
        vol, _ = _get_volume(store, "000001.SZ")
        assert vol == 800  # 只加一次

    def test_sell_full_clear_resets_state(self, store):
        """C1: 清仓重置 peak/tp_triggered/entry_date(旧 bug 因不更新而丢失)。"""
        _insert_position(store, "000001.SZ", volume=1000)
        _insert_deal(store, 1005, "000001.SZ", "sell", 1000)
        store.apply_sell_fill("000001.SZ", 1000, trade_id=1005)
        row = store._conn.execute(
            "SELECT volume, peak_price, entry_date, tp_triggered FROM live_positions WHERE code=?",
            ["000001.SZ"]
        ).fetchone()
        assert row[0] == 0          # volume 0
        assert row[1] == 0          # peak 重置
        assert row[2] is None       # entry_date 重置
        assert row[3] == "[]"       # tp_triggered 重置


# ── C2: 废单只释放冻结, 不膨胀 volume ──────────────────────

class TestC2ReleasePendingBuy:
    def test_release_only_pending_not_volume(self, store):
        """C2: release_pending_buy 释放 pending, volume 不动(废单不膨胀持仓)。"""
        _insert_position(store, "000001.SZ", volume=500, pending=300)
        store.release_pending_buy("000001.SZ", 300)
        vol, pending = _get_volume(store, "000001.SZ")
        assert vol == 500      # volume 不变(旧 bug: apply_buy_fill 会 += 300 → 800)
        assert pending == 0    # 冻结释放

    def test_release_clamped_at_zero(self, store):
        """C2: 释放超过 pending 量钳到 0, 不为负。"""
        _insert_position(store, "000001.SZ", volume=500, pending=100)
        store.release_pending_buy("000001.SZ", 300)
        vol, pending = _get_volume(store, "000001.SZ")
        assert pending == 0
        assert vol == 500


# ── C3: CallbackHandler 注入 audit ─────────────────────────

class TestC3AuditInjected:
    def test_audit_injected(self, tmp_config):
        """C3: CallbackHandler 接受 audit 参数并保存(旧版无此参数, 引用必 AttributeError)。"""
        from app.live_trader.callback_handler import CallbackHandler
        mock_audit = MagicMock()
        handler = CallbackHandler(tmp_config, store=None, audit=mock_audit)
        assert handler.audit is mock_audit

    def test_audit_defaults_none(self, tmp_config):
        """C3: 不传 audit 时默认 None(向后兼容, 不崩)。"""
        from app.live_trader.callback_handler import CallbackHandler
        handler = CallbackHandler(tmp_config, store=None)
        assert handler.audit is None
