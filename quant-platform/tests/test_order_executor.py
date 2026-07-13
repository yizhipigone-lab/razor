"""order_executor 单元测试(候选③)

锁定 OrderExecutor 深 module 的契约:3 路委托(WEB/TDX/EXIT)统一走 execute(),
各路由通过 kwargs 区分行为差异。
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.live_trader.schemas import OrderIntent


# ===== Fixtures =====

@pytest.fixture
def deps():
    """OrderExecutor 9 个依赖的最小 mock 集合。"""
    qmt = MagicMock()
    qmt.connected = True
    # 默认 qmt.get_realtime_quotes 返回空 dict(覆盖不发生)
    qmt.get_realtime_quotes.return_value = {}
    qmt.query_asset.return_value = {"total_asset": 100000, "cash": 100000}
    qmt.query_orders.return_value = []
    qmt.cancel_order.return_value = 0
    # 默认 order_stock_async 返 seq=1,模拟成功
    qmt.order_stock_async.return_value = 1

    store = MagicMock()
    store.get_positions.return_value = []
    store.get_order_by_client_id.return_value = None
    # sync_terminal_write 默认无返回
    store.sync_terminal_write.return_value = None

    risk_gate = MagicMock()
    risk_gate.check.return_value = (True, [{"gate": "ok"}], "all passed")

    clearance_lock = MagicMock()
    clearance_lock.acquire_with_wait.return_value = True

    kill_switch = MagicMock()
    kill_switch.is_active.return_value = False

    callback = MagicMock()
    callback.mock_order_async_response.return_value = 999  # dry-run mock 返 999

    audit = MagicMock()
    notifier = MagicMock()

    runtime_state = SimpleNamespace(mode="dry-run", is_dry_run=lambda: True)

    config = SimpleNamespace(mode="dry-run", live_capital=100000)

    return SimpleNamespace(
        config=config, runtime_state=runtime_state, store=store, qmt=qmt,
        risk_gate=risk_gate, clearance_lock=clearance_lock,
        kill_switch=kill_switch,
        callback_handler=callback, callback=callback,  # 双别名,测试更顺手
        audit=audit, notifier=notifier,
    )


@pytest.fixture
def executor(deps):
    from app.live_trader.order_executor import OrderExecutor
    return OrderExecutor(
        config=deps.config, runtime_state=deps.runtime_state,
        store=deps.store, qmt=deps.qmt, risk_gate=deps.risk_gate,
        clearance_lock=deps.clearance_lock, kill_switch=deps.kill_switch,
        callback_handler=deps.callback_handler, audit=deps.audit,
        notifier=deps.notifier,
    )


@pytest.fixture
def buy_intent():
    return OrderIntent(
        code="600000.SH", direction="buy", volume=100,
        price=10.5, price_type=11, strategy_name="TEST",
        terminal="WEB", client_order_id="test123buy",
    )


@pytest.fixture
def sell_intent():
    return OrderIntent(
        code="600000.SH", direction="sell", volume=100,
        price=10.5, price_type=11, strategy_name="exit_monitor",
        terminal="SYS", client_order_id="test123sell",
    )


# ===== 1. 基础流程:dry-run + 通过全部闸门 =====

class TestBasicDryRun:
    def test_dry_run_uses_mock_order_async(self, executor, deps, buy_intent):
        result = executor.execute(buy_intent, source="WEB")
        deps.callback.mock_order_async_response.assert_called_once()
        deps.qmt.order_stock_async.assert_not_called()
        assert result["ok"] is True
        assert result["status"] == "submitted"
        assert result["source"] == "WEB"

    def test_dry_run_writes_live_orders(self, executor, deps, buy_intent):
        executor.execute(buy_intent, source="WEB")
        deps.store.sync_terminal_write.assert_called_once()
        args = deps.store.sync_terminal_write.call_args
        # sync_terminal_write("order", data)
        assert args[0][0] == "order"
        data = args[0][1]
        assert data["code"] == "600000.SH"
        assert data["direction"] == "buy"
        assert data["volume"] == 100
        assert data["status"] == 50  # 已报

    def test_dry_run_calls_audit_order_placed(self, executor, deps, buy_intent):
        executor.execute(buy_intent, source="WEB")
        deps.audit.order_placed.assert_called_once()
        audit_call = deps.audit.order_placed.call_args
        assert audit_call[0][0] == "600000.SH"  # code
        assert audit_call[0][1] == 999  # mock order_id
        assert audit_call[0][2] == "dry-run"  # mode
        # snapshot 应包含 source
        assert audit_call[0][3]["source"] == "WEB"


# ===== 2. Live 模式 =====

class TestLiveMode:
    def test_live_calls_qmt_order_stock_async(self, executor, deps, sell_intent):
        deps.runtime_state.mode = "live"
        deps.runtime_state.is_dry_run = lambda: False
        executor.execute(sell_intent, source="WEB")
        deps.qmt.order_stock_async.assert_called_once()
        deps.callback.mock_order_async_response.assert_not_called()

    def test_live_buy_freezes_pending(self, executor, deps, buy_intent):
        deps.runtime_state.mode = "live"
        deps.runtime_state.is_dry_run = lambda: False
        deps.qmt.order_stock_async.return_value = 42  # > 0 success
        executor.execute(buy_intent, source="WEB")
        deps.risk_gate.freeze_pending_buy.assert_called_once_with("600000.SH", 100)

    def test_live_sell_no_freeze(self, executor, deps, sell_intent):
        deps.runtime_state.mode = "live"
        deps.runtime_state.is_dry_run = lambda: False
        deps.qmt.order_stock_async.return_value = 42
        executor.execute(sell_intent, source="WEB")
        deps.risk_gate.freeze_pending_buy.assert_not_called()

    def test_live_buy_failed_no_freeze(self, executor, deps, buy_intent):
        deps.runtime_state.mode = "live"
        deps.runtime_state.is_dry_run = lambda: False
        # qmt 失败也 raise → 走 except 分支,不会 freeze
        deps.qmt.order_stock_async.side_effect = Exception("seq=0 failed")
        result = executor.execute(buy_intent, source="WEB")
        assert result["ok"] is False
        deps.risk_gate.freeze_pending_buy.assert_not_called()


# ===== 3. QMT 未连接 =====

class TestQmtDisconnected:
    def test_live_qmt_disconnected_returns_error(self, executor, deps, buy_intent):
        deps.runtime_state.mode = "live"
        deps.runtime_state.is_dry_run = lambda: False
        deps.qmt.connected = False
        result = executor.execute(buy_intent, source="WEB")
        assert result["ok"] is False
        assert result["status"] == "error"
        assert "QMT" in result["reason"]


# ===== 4. kill_switch =====

class TestKillSwitch:
    def test_active_kill_switch_returns_forbidden(self, executor, deps, buy_intent):
        deps.kill_switch.is_active.return_value = True
        result = executor.execute(buy_intent, source="WEB")
        assert result["ok"] is False
        assert result["status"] == "forbidden"
        deps.callback.mock_order_async_response.assert_not_called()


# ===== 5. C3 幂等 =====

class TestIdempotency:
    def test_existing_client_order_id_returns_duplicate(self, executor, deps, buy_intent):
        deps.store.get_order_by_client_id.return_value = {
            "order_id": 123, "client_order_id": buy_intent.client_order_id
        }
        result = executor.execute(buy_intent, source="WEB")
        assert result["ok"] is True
        assert result["status"] == "duplicate"
        assert result["order_id"] == 123
        deps.callback.mock_order_async_response.assert_not_called()

    def test_no_client_order_id_skips_idempotency(self, executor, deps, buy_intent):
        buy_intent.client_order_id = ""  # exit-monitor 偶有空
        result = executor.execute(buy_intent, source="WEB")
        assert result["ok"] is True
        assert result["status"] == "submitted"


# ===== 6. TDX 价格覆盖 =====

class TestTdxPriceOverride:
    def test_tdx_source_overrides_with_qmt_price(self, executor, deps, buy_intent):
        deps.qmt.get_realtime_quotes.return_value = {
            "600000.SH": {"lastPrice": 11.5, "lastClose": 10.0}
        }
        deps.runtime_state.mode = "live"
        deps.runtime_state.is_dry_run = lambda: False
        deps.qmt.order_stock_async.return_value = 42
        executor.execute(buy_intent, source="TDX")
        # qmt.order_stock_async 第 5 个参数是 price
        call_args = deps.qmt.order_stock_async.call_args
        assert call_args[0][4] == 11.5  # price overridden
        assert call_args[0][3] == 11  # price_type 保留

    def test_tdx_source_no_qmt_price_keeps_intent_price(self, executor, deps, buy_intent):
        deps.qmt.get_realtime_quotes.return_value = {
            "600000.SH": {"lastPrice": 0, "lastClose": 10.0}  # 0=无有效价
        }
        deps.runtime_state.mode = "live"
        deps.runtime_state.is_dry_run = lambda: False
        deps.qmt.order_stock_async.return_value = 42
        executor.execute(buy_intent, source="TDX")
        call_args = deps.qmt.order_stock_async.call_args
        assert call_args[0][4] == 10.5  # 仍用 intent.price

    def test_web_source_no_qmt_override(self, executor, deps, buy_intent):
        deps.qmt.get_realtime_quotes.return_value = {
            "600000.SH": {"lastPrice": 11.5, "lastClose": 10.0}
        }
        deps.runtime_state.mode = "live"
        deps.runtime_state.is_dry_run = lambda: False
        deps.qmt.order_stock_async.return_value = 42
        executor.execute(buy_intent, source="WEB")  # 不是 TDX
        call_args = deps.qmt.order_stock_async.call_args
        assert call_args[0][4] == 10.5  # 不会覆盖


# ===== 7. 风控拒绝 =====

class TestRiskGateReject:
    def test_risk_rejected_returns_risk_rejected(self, executor, deps, buy_intent):
        deps.risk_gate.check.return_value = (False, [{"gate": "G1"}], "仓位超限")
        result = executor.execute(buy_intent, source="WEB")
        assert result["ok"] is False
        assert result["status"] == "risk_rejected"
        assert "仓位超限" in result["reason"]
        deps.audit.gate_reject.assert_called_once()
        deps.callback.mock_order_async_response.assert_not_called()

    def test_risk_rejected_no_freeze_no_write(self, executor, deps, buy_intent):
        deps.risk_gate.check.return_value = (False, [{"gate": "G1"}], "仓位超限")
        executor.execute(buy_intent, source="WEB")
        deps.store.sync_terminal_write.assert_not_called()
        deps.audit.order_placed.assert_not_called()


# ===== 8. 清仓锁 =====

class TestClearanceLock:
    def test_lock_acquired_returns_ok(self, executor, deps, buy_intent):
        deps.clearance_lock.acquire_with_wait.return_value = True
        executor.execute(buy_intent, source="WEB", lock_wait_sec=5)
        deps.callback.mock_order_async_response.assert_called_once()

    def test_lock_timeout_returns_locked(self, executor, deps, buy_intent):
        deps.clearance_lock.acquire_with_wait.return_value = False
        result = executor.execute(buy_intent, source="WEB", lock_wait_sec=5)
        assert result["ok"] is False
        assert result["status"] == "locked"
        deps.callback.mock_order_async_response.assert_not_called()

    def test_lock_wait_sec_forwarded(self, executor, deps, buy_intent):
        executor.execute(buy_intent, source="TDX", lock_wait_sec=3)
        deps.clearance_lock.acquire_with_wait.assert_called_once()
        args = deps.clearance_lock.acquire_with_wait.call_args
        assert args[0][0] == "600000.SH"
        assert args[1]["timeout_sec"] == 3


# ===== 9. exit-monitor 特性 =====

class TestExitMonitorPath:
    """exit_monitor._execute_sell 委托到 executor 的差异化行为"""

    def test_cancel_inflight_true_calls_qmt_cancel(self, executor, deps, sell_intent):
        deps.qmt.query_orders.return_value = [
            {"order_id": 100, "code": "600000.SH"}
        ]
        executor.execute(
            sell_intent, source="EXIT", lock_wait_sec=0,
            cancel_inflight=True,
        )
        deps.qmt.query_orders.assert_called_with(cancelable_only=True)
        deps.qmt.cancel_order.assert_called_once_with(100)

    def test_cancel_inflight_false_skips_cancel(self, executor, deps, sell_intent):
        executor.execute(sell_intent, source="WEB")
        deps.qmt.cancel_order.assert_not_called()

    def test_risk_positions_only_skips_asset_quote(self, executor, deps, buy_intent):
        """exit-monitor 卖:只传 positions,asset/quote=None"""
        executor.execute(
            buy_intent, source="EXIT",
            risk_positions_only=True,
        )
        deps.risk_gate.check.assert_called_once()
        call_args = deps.risk_gate.check.call_args
        # asset, positions, quote 都应=None(positions_only 模式)
        # 第 1 个位置参数是 intent
        assert call_args[1].get("asset") is None
        assert call_args[1].get("quote") is None

    def test_persist_live_orders_false_skips_db_write(self, executor, deps, sell_intent):
        """exit-monitor 卖:不写 live_orders 表(保留旧行为,防破坏交易查询页)"""
        executor.execute(
            sell_intent, source="EXIT",
            persist_live_orders=False,
        )
        deps.store.sync_terminal_write.assert_not_called()
        # 但 audit 还是要写(原 exit_monitor 行为)
        deps.audit.order_placed.assert_called_once()

    def test_persist_live_orders_true_writes_db(self, executor, deps, sell_intent):
        """WEB/TDX 路径:始终写 live_orders 表"""
        executor.execute(sell_intent, source="WEB")
        deps.store.sync_terminal_write.assert_called_once()

    def test_on_order_submitted_callback_fires(self, executor, deps, sell_intent):
        """on_order_submitted 应在 submit 成功后触发(exit-monitor TP 标记用)"""
        captured = []

        def cb(order_id, intent):
            captured.append((order_id, intent.code))

        executor.execute(
            sell_intent, source="EXIT",
            on_order_submitted=cb,
        )
        assert len(captured) == 1
        assert captured[0] == (999, "600000.SH")  # mock 返 999

    def test_on_order_submitted_not_called_when_lock_failed(self, executor, deps, sell_intent):
        captured = []

        def cb(order_id, intent):
            captured.append(order_id)

        deps.clearance_lock.acquire_with_wait.return_value = False
        executor.execute(
            sell_intent, source="EXIT", lock_wait_sec=0,
            on_order_submitted=cb,
        )
        assert captured == []

    def test_on_order_submitted_not_called_when_risk_rejected(self, executor, deps, sell_intent):
        captured = []

        def cb(order_id, intent):
            captured.append(order_id)

        deps.risk_gate.check.return_value = (False, [], "G1 fail")
        executor.execute(
            sell_intent, source="EXIT",
            on_order_submitted=cb,
        )
        assert captured == []


# ===== 10. 异常兜底 =====

class TestExceptions:
    def test_qmt_exception_releases_lock_and_returns_error(self, executor, deps, buy_intent):
        deps.callback.mock_order_async_response.side_effect = Exception("boom")
        # 注意:execute() 内部 except 会捕获并返回 error
        result = executor.execute(buy_intent, source="WEB")
        assert result["ok"] is False
        assert "boom" in result["reason"]
        deps.clearance_lock.release.assert_called_once_with("600000.SH")
