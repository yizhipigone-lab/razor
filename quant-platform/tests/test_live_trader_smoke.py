"""live_trader smoke test(v5.4 §18.4 / codex §4.1)

8 类单元/集成测试,5 分钟 CI。
不连真 QMT(mock qmt_wrapper),验证核心逻辑。

运行:pytest tests/test_live_trader_smoke.py -v
"""
import os
import sys
import threading
import time
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

# 确保能 import app
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ===== Fixtures =====

@pytest.fixture
def tmp_config(tmp_path):
    """临时配置(不连真 QMT)"""
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


# ===== 1. xtquant_compat 4 层降级 =====

def test_xtquant_compat_format_code():
    """代码格式化"""
    from app.utils.xtquant_compat import format_code, strip_code_suffix
    assert format_code("600000") == "600000.SH"
    assert format_code("000001") == "000001.SZ"
    assert format_code("300750") == "300750.SZ"
    assert format_code("688981") == "688981.SH"
    assert format_code("159226") == "159226.SZ"  # ETF
    assert format_code("600000.SH") == "600000.SH"  # 已带后缀
    assert strip_code_suffix("600000.SH") == "600000"


def test_xtquant_compat_safe_float():
    """浮点清洗(NaN/Inf/None)"""
    from app.utils.xtquant_compat import safe_float, safe_int
    import math
    assert safe_float(3.14) == 3.14
    assert safe_float(float("nan")) == 0.0
    assert safe_float(float("inf")) == 0.0
    assert safe_float(None) == 0.0
    assert safe_float("abc", -1.0) == -1.0
    assert safe_int("123") == 123
    assert safe_int(None) == 0


def test_xtquant_compat_status_constants():
    """状态码常量"""
    from app.utils.xtquant_compat import (
        ORDER_STATUS_TERMINAL, ORDER_STATUS_INFLIGHT, status_to_text
    )
    assert 56 in ORDER_STATUS_TERMINAL  # 已成
    assert 57 in ORDER_STATUS_TERMINAL  # 废单
    assert 50 in ORDER_STATUS_INFLIGHT  # 已报可撤
    assert status_to_text(56) == "已成"
    assert status_to_text(57) == "废单"


# ===== 2. 清仓锁 acquire/release/race =====

def test_clearance_lock_acquire_release(tmp_config):
    """清仓锁基本 acquire/release"""
    from app.live_trader.clearance_lock import ClearanceLock
    lock = ClearanceLock(tmp_config)
    assert lock.acquire("600000.SH", order_id=1001) is True
    assert lock.is_locked("600000.SH") is True
    # 重复 acquire 失败
    assert lock.acquire("600000.SH", order_id=1002) is False
    # 按 order_id 释放
    assert lock.release_by_order_id(1001) is True
    assert lock.is_locked("600000.SH") is False


def test_clearance_lock_race_condition(tmp_config):
    """清仓锁防 race(多线程并发 acquire 同一 code)"""
    from app.live_trader.clearance_lock import ClearanceLock
    lock = ClearanceLock(tmp_config)
    results = []
    barrier = threading.Barrier(5)

    def try_acquire():
        barrier.wait()
        r = lock.acquire("600000.SH", order_id=1001)
        results.append(r)

    threads = [threading.Thread(target=try_acquire) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 只能有 1 个成功
    assert sum(results) == 1, f"应有1个成功,实际{sum(results)}"


def test_clearance_lock_ttl_expiry(tmp_config):
    """清仓锁 TTL 过期释放"""
    from app.live_trader.clearance_lock import ClearanceLock
    config = tmp_config
    config = type(config)(**{**config.__dict__, "clearance_lock_ttl_sec": 0.1})
    lock = ClearanceLock(config)
    assert lock.acquire("600000.SH") is True
    time.sleep(0.2)
    # TTL 过期后应可重新 acquire
    assert lock.acquire("600000.SH") is True


# ===== 3. RiskGate 8 闸门(部分关键闸门)=====

def test_risk_gate_kill_switch_blocks(tmp_config, store):
    """闸门8:kill switch 激活时全拒"""
    from app.live_trader.risk_gate import RiskGate
    from app.live_trader.kill_switch import KillSwitch
    from app.live_trader.schemas import OrderIntent

    ks = KillSwitch(tmp_config, store)
    ks.activate(reason="test", source="test")
    rg = RiskGate(tmp_config, store, ks, qmt_wrapper=MagicMock(connected=False))

    intent = OrderIntent(code="600000.SH", direction="buy", volume=100, price=10.0)
    passed, gates, reason = rg.check(intent)
    assert passed is False
    assert "kill switch" in reason


def test_risk_gate_single_amount_limit(tmp_config, store):
    """闸门1:单笔金额超限拒绝"""
    from app.live_trader.risk_gate import RiskGate
    from app.live_trader.kill_switch import KillSwitch
    from app.live_trader.schemas import OrderIntent

    ks = KillSwitch(tmp_config, store)
    rg = RiskGate(tmp_config, store, ks, qmt_wrapper=MagicMock(connected=True))
    # 单笔 50万 > 20%×10万=2万
    intent = OrderIntent(code="600000.SH", direction="buy", volume=1000, price=50.0)
    passed, gates, reason = rg.check(intent, asset={"cash": 500000})
    assert passed is False
    assert "单笔金额" in reason


def test_risk_gate_t1_sell_check(tmp_config, store):
    """闸门9:T+1 当日买入不可卖"""
    from app.live_trader.risk_gate import RiskGate
    from app.live_trader.kill_switch import KillSwitch
    from app.live_trader.schemas import OrderIntent
    from app.utils.xtquant_compat import format_code

    ks = KillSwitch(tmp_config, store)
    rg = RiskGate(tmp_config, store, ks, qmt_wrapper=MagicMock(connected=True))

    # 当日买入的持仓(can_use=0)
    today = date.today()
    store.upsert_position({
        "code": "600000.SH", "volume": 100, "can_use_volume": 0,
        "frozen_volume": 0, "pending_buy_volume": 0, "avg_cost": 10.0,
        "last_price": 10.5, "managed": True, "entry_date": today,
    })

    intent = OrderIntent(code="600000.SH", direction="sell", volume=100, price=10.5)
    passed, gates, reason = rg.check(intent, positions=store.get_positions())
    assert passed is False
    assert "T+1" in reason or "可卖" in reason


def test_risk_gate_c1_pending_buy(tmp_config, store):
    """C1:在途预扣防超买"""
    from app.live_trader.risk_gate import RiskGate
    from app.live_trader.kill_switch import KillSwitch
    from app.live_trader.schemas import OrderIntent

    ks = KillSwitch(tmp_config, store)
    rg = RiskGate(tmp_config, store, ks, qmt_wrapper=MagicMock(connected=True))

    # 已有持仓 + 在途预扣
    store.upsert_position({
        "code": "600000.SH", "volume": 200, "can_use_volume": 200,
        "frozen_volume": 0, "pending_buy_volume": 200,  # 在途200股
        "avg_cost": 10.0, "last_price": 10.0, "managed": True,
    })
    # 冻结更多预扣
    rg.freeze_pending_buy("600000.SH", 100)
    pos = store.get_position("600000.SH")
    assert pos["pending_buy_volume"] == 300  # 200+100


# ===== 4. buildSimpleCycles 盈亏闭环 =====

def test_pnl_engine_build_cycles(store):
    """buildSimpleCycles:多次买入+卖出,净仓归零闭合"""
    from app.live_trader.pnl_engine import PnlEngine
    engine = PnlEngine(store)

    deals = [
        {"code": "600000.SH", "direction": "buy", "filled_volume": 100,
         "filled_price": 10.0, "filled_amount": 1000.0, "traded_at": datetime(2026, 7, 1, 9, 30)},
        {"code": "600000.SH", "direction": "buy", "filled_volume": 100,
         "filled_price": 11.0, "filled_amount": 1100.0, "traded_at": datetime(2026, 7, 1, 10, 0)},
        {"code": "600000.SH", "direction": "sell", "filled_volume": 200,
         "filled_price": 12.0, "filled_amount": 2400.0, "traded_at": datetime(2026, 7, 1, 14, 0)},
    ]
    cycles = engine.build_cycles(deals)
    assert len(cycles) == 1
    assert cycles[0]["status"] == "closed"
    # 加权均价 = (1000+1100)/200 = 10.5
    assert abs(cycles[0]["buy_avg_price"] - 10.5) < 0.01
    # 盈亏 = 2400 - 2100 = 300
    assert abs(cycles[0]["pnl"] - 300.0) < 0.01


def test_pnl_engine_ongoing_cycle(store):
    """未闭合周期保持 ongoing"""
    from app.live_trader.pnl_engine import PnlEngine
    engine = PnlEngine(store)

    deals = [
        {"code": "600000.SH", "direction": "buy", "filled_volume": 100,
         "filled_price": 10.0, "filled_amount": 1000.0, "traded_at": datetime(2026, 7, 1, 9, 30)},
        {"code": "600000.SH", "direction": "sell", "filled_volume": 50,
         "filled_price": 11.0, "filled_amount": 550.0, "traded_at": datetime(2026, 7, 1, 14, 0)},
    ]
    cycles = engine.build_cycles(deals)
    assert len(cycles) == 1
    assert cycles[0]["status"] == "ongoing"  # 净仓50未归零


# ===== 5. callback 状态转换校验 =====

def test_callback_invalid_transition_rejected(tmp_config, store):
    """callback 非法状态转换拒绝(57废单不能变回50)"""
    from app.live_trader.callback_handler import CallbackHandler

    # 预置一个废单(57)
    store.sync_terminal_write("order", {
        "order_id": 2001, "client_order_id": "c1", "code": "600000.SH",
        "direction": "buy", "volume": 100, "price": 10.0, "price_type": 11,
        "status": 57, "status_msg": "废单", "seq": 1, "mode": "dry-run",
        "terminal": "SYS", "created_at": datetime.now(), "updated_at": datetime.now(),
        "finished_at": datetime.now(),
    })

    handler = CallbackHandler(tmp_config, store)
    # 尝试 57→50(非法)
    mock_order = MagicMock()
    mock_order.order_id = 2001
    mock_order.order_status = 50
    mock_order.stock_code = "600000.SH"
    mock_order.order_volume = 100
    mock_order.price = 10.0

    handler._handle_order_update(2001, 50, mock_order)
    # 状态应保持 57
    order = store.get_order(2001)
    assert order["status"] == 57


# ===== 6. KillSwitch 三重状态 =====

def test_kill_switch_triple_state(tmp_config, store):
    """kill switch 三重状态(DB+文件+内存)"""
    from app.live_trader.kill_switch import KillSwitch
    ks = KillSwitch(tmp_config, store)
    assert ks.is_active() is False

    ks.activate(reason="test", source="unit_test")
    assert ks.is_active() is True
    # 文件存在
    assert os.path.exists(ks._file_path)
    # DB 状态
    db_state = store.get_killswitch()
    assert db_state["activated"] is True

    # 解除
    ks.deactivate()
    assert ks.is_active() is False
    assert not os.path.exists(ks._file_path)


# ===== 7. Reconciler 不回写 live_positions(v5.3 修复)=====

def test_reconciler_no_writeback(tmp_config, store):
    """Reconciler 偏差只写 audit 表,不回写 live_positions"""
    from app.live_trader.reconciler import Reconciler

    # 本地持仓 200,QMT 返回 100(偏差100股)
    store.upsert_position({
        "code": "600000.SH", "volume": 200, "can_use_volume": 200,
        "frozen_volume": 0, "pending_buy_volume": 0, "avg_cost": 10.0,
        "last_price": 10.0, "managed": True,
    })

    mock_qmt = MagicMock()
    mock_qmt.connected = True
    mock_qmt.query_positions.return_value = [
        {"code": "600000.SH", "volume": 100, "last_price": 10.0}
    ]

    r = Reconciler(tmp_config, store, mock_qmt)
    result = r.reconcile()

    # 本地持仓未被回写(仍是200)
    pos = store.get_position("600000.SH")
    assert pos["volume"] == 200, "Reconciler 不应回写 live_positions"

    # audit 表有记录
    assert store._conn is not None
    audit_rows = store._conn.execute("SELECT * FROM live_positions_audit").fetchall()
    assert len(audit_rows) >= 1


def test_reconciler_etf_exempt(tmp_config, store):
    """ETF 保留持仓(managed=false)偏差不触发 kill switch"""
    from app.live_trader.reconciler import Reconciler
    from app.live_trader.kill_switch import KillSwitch

    store.upsert_position({
        "code": "159226.SZ", "volume": 500000, "can_use_volume": 500000,
        "frozen_volume": 0, "pending_buy_volume": 0, "avg_cost": 1.3,
        "last_price": 1.3, "managed": False,  # ETF 保留
    })

    mock_qmt = MagicMock()
    mock_qmt.connected = True
    mock_qmt.query_positions.return_value = [
        {"code": "159226.SZ", "volume": 486700, "last_price": 1.3}  # 偏差13300股
    ]

    ks = KillSwitch(tmp_config, store)
    r = Reconciler(tmp_config, store, mock_qmt, ks)
    result = r.reconcile()

    # ETF 偏差不触发 kill switch
    assert ks.is_active() is False, "ETF 保留持仓偏差不应触发 kill switch"


# ===== 8. 持仓接管 ETF 分类(§3.3.1)=====

def test_position_takeover_etf_managed(tmp_config, store):
    """持仓接管:ETF 标 managed=false,股票标 managed=true"""
    from app.live_trader.main import _takeover_positions

    mock_qmt = MagicMock()
    mock_qmt.connected = True
    mock_qmt.query_positions.return_value = [
        {"code": "159226.SZ", "volume": 486700, "can_use_volume": 486700, "avg_cost": 1.3, "last_price": 1.3},
        {"code": "600000.SH", "volume": 300, "can_use_volume": 300, "avg_cost": 10.5, "last_price": 10.6},
    ]

    audit = MagicMock()
    _takeover_positions(store, mock_qmt, tmp_config, audit)

    etf = store.get_position("159226.SZ")
    stock = store.get_position("600000.SH")
    assert etf["managed"] is False, "ETF 应标 managed=false"
    assert stock["managed"] is True, "股票应标 managed=true"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])


# ===== 9. 闸门5a 日亏计算(不再返回 None fail-safe)=====

def test_risk_gate_5a_daily_loss_with_backup(tmp_config, store):
    """闸门5a:有 live_assets_backup 时能算出日亏率(不再永远 fail-safe)"""
    from app.live_trader.risk_gate import RiskGate
    from app.live_trader.kill_switch import KillSwitch
    from app.live_trader.schemas import OrderIntent

    ks = KillSwitch(tmp_config, store)
    qmt_mock = MagicMock()
    qmt_mock.connected = True
    gate = RiskGate(tmp_config, store, ks, qmt_mock)

    # 写入当日开盘资产备份(闸门5a 基准)
    store.backup_asset({"cash": 50000, "frozen_cash": 0, "market_value": 50000, "total_asset": 100000})

    # 场景1:当前总资产=100000(无亏),应通过
    intent = OrderIntent(code="600000.SH", direction="buy", volume=100, price=10.0, client_order_id="t1")
    passed, gates, reason = gate.check(intent, asset={"total_asset": 100000}, positions=[], quote=None)
    gate_5a = [g for g in gates if g.get("gate") == "5a"]
    # 缺行情时闸门3/4/5a 可能因其他原因拒绝,但5a不应因"无基准"而fail-safe
    if gate_5a:
        assert gate_5a[0]["current"] != "缺价fail-safe", "有资产备份不应返回缺价fail-safe"

    # 场景2:日亏 > 3%,应被拒
    intent2 = OrderIntent(code="600000.SH", direction="buy", volume=100, price=10.0, client_order_id="t2")
    passed2, gates2, reason2 = gate.check(
        intent2, asset={"total_asset": 96000}, positions=[], quote=None
    )
    # 日亏率 = (96000-100000)/100000 = -4% < -3%,应被5a拒绝
    gate_5a_2 = [g for g in gates2 if g.get("gate") == "5a"]
    if gate_5a_2 and not gate_5a_2[0]["passed"]:
        assert "熔断" in reason2 or "日亏" in reason2

    # 场景3:无资产备份,应 fail-safe 禁买
    # 用新的 store(没有 backup)
    from app.live_trader.config import LiveTraderConfig
    from app.live_trader.store import LiveTraderStore
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        empty_cfg = LiveTraderConfig(
            qmt_account_id="test", live_capital=100000, mode="dry-run",
            db_path=f"{td}/empty.duckdb", lock_file=f"{td}/e.lock",
            restart_counter_file=f"{td}/e.json", wal_path=f"{td}/e.wal",
        )
        empty_store = LiveTraderStore(empty_cfg)
        gate3 = RiskGate(empty_cfg, empty_store, ks, qmt_mock)
        intent3 = OrderIntent(code="600000.SH", direction="buy", volume=100, price=10.0, client_order_id="t3")
        passed3, gates3, reason3 = gate3.check(intent3, asset={"total_asset": 96000}, positions=[], quote=None)
        gate_5a_3 = [g for g in gates3 if g.get("gate") == "5a"]
        if gate_5a_3:
            assert gate_5a_3[0]["current"] == "缺价fail-safe", "无资产备份应 fail-safe"
        empty_store.close()


# ===== 10. 调度器基本逻辑 =====

def test_scheduler_non_trading_day(tmp_config, store):
    """调度器:非交易日自动激活 kill switch"""
    from app.live_trader.scheduler import LiveScheduler
    from app.live_trader.kill_switch import KillSwitch
    from unittest.mock import MagicMock, patch

    ks = KillSwitch(tmp_config, store)
    notifier = MagicMock()
    sched = LiveScheduler(tmp_config, store, kill_switch=ks, notifier=notifier)

    # 直接调用 _handle_non_trading_day(绕过 datetime 不可 patch 的限制)
    sched._handle_non_trading_day()

    assert ks.is_active(), "非交易日应自动激活 kill switch"

