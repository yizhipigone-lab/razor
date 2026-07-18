# -*- coding: utf-8 -*-
"""Critical fixes regression tests.

每个 test_* 函数对应一个已知风险点的护栏,失败 = 防线被破。
"""
from datetime import date
from pathlib import Path


def test_pytest_does_not_collect_scripts_directory():
    """pytest.ini 必须排除 scripts/ 下的 test_*.py,即使外部跑 pytest scripts/

    scripts/test_fix_*.py 共 28 个,命名匹配 python_files = test_*.py,
    当前 testpaths = tests 保护 pytest 不主动收, 但 `pytest scripts/` 显式跑会触发误收集。
    必须 addopts = --ignore=scripts 才安全。
    """
    cfg = Path("pytest.ini").read_text(encoding="utf-8")
    assert "addopts" in cfg, "pytest.ini 缺 [pytest].addopts 段"
    assert "--ignore=scripts" in cfg, "缺 --ignore=scripts 防御 scripts/test_fix_*.py 误收集"


def test_can_buy_rejects_limit_up_with_real_prev_close():
    """HIGH-3 回归测试 - can_buy 真实签名 + 涨停判断可用

    can_buy 真实签名(app/backtest/execution.py:30):
      def can_buy(code: str, prev_close: float, today_high: float) -> Tuple[bool, str]
    prev_close=10, today_high=11.5 → change = 15% >= 10% 主板涨停线 → 拒
    """
    from app.backtest.execution import can_buy
    can_buy_ok, reason = can_buy("600000", 10.0, 11.5)
    assert can_buy_ok is False, "涨停日必须被拒"
    assert reason and ("涨停" in reason or "limit" in reason.lower())


def test_simple_runner_buy_without_prev_close_warns_compat(caplog):
    """HIGH-3 (compat 4b) 回归测试 - 旧三参缺 prev_close 仍放行,但加 deprecation warning

    scripts/ 下 22 个 CLI 用旧三参 eng.buy(d, code, px) 不应崩 — 此测试守住兼容降级。
    """
    import logging
    import warnings
    import pytest
    from app.backtest.simple_runner import FastEngine
    td_list = [date(2024, 1, d) for d in range(2, 11)]
    params = {
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "min_buy_amt": 5_000,
    }
    eng = FastEngine(td_list, params)

    with caplog.at_level(logging.WARNING, logger="FastEngine"):
        with pytest.warns(DeprecationWarning, match="prev_close"):
            res = eng.buy(date(2024, 1, 3), "600000", 10.0, prev_close=None)

    assert res is not None
    assert getattr(res, "code", None) == "600000"
    assert any("缺 prev_close" in r.message for r in caplog.records)


def test_simple_runner_buy_with_prev_close_strict_rejects_limit_up(caplog):
    """HIGH-3 回归测试 - 主回测路径传真实 prev_close 时,涨停日应被拒"""
    import logging
    from app.backtest.simple_runner import FastEngine
    td_list = [date(2024, 1, d) for d in range(2, 11)]
    params = {
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "min_buy_amt": 5_000,
    }
    eng = FastEngine(td_list, params)

    with caplog.at_level(logging.WARNING, logger="FastEngine"):
        res = eng.buy(date(2024, 1, 3), "600000", 11.0, prev_close=10.0)
    assert res is None, "涨停日应被拒"
    assert not any("缺 prev_close" in r.message for r in caplog.records)


def test_simple_runner_main_loop_passes_prev_close(caplog):
    """HIGH-3 回归测试 - 主回测路径传真实 prev_close 时,不应触发降级 warning

    验证 22 个 scripts/ CLI 才是降级消费者,
    simple_runner 主回测路径(self call)走严格 prev_close。
    模拟主回测传 prev_close=10.0 (无涨停日) → 应成功买入 + 无 warning。
    """
    import logging
    from app.backtest.simple_runner import FastEngine
    td_list = [date(2024, 1, d) for d in range(2, 11)]
    params = {
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "min_buy_amt": 5_000,
    }
    eng = FastEngine(td_list, params)

    with caplog.at_level(logging.WARNING, logger="FastEngine"):
        res = eng.buy(date(2024, 1, 3), "600000", 10.5, prev_close=10.0)
    # +5% 非涨停,应成功买入
    assert res is not None and getattr(res, "code", None) == "600000", \
        "传真实 prev_close + 非涨停价 → 应成功买入"
    # 传了 prev_close 不应触发 warning
    assert not any("缺 prev_close" in r.message for r in caplog.records), \
        "传了 prev_close 不应触发降级 warning"


def test_simple_runner_filters_limit_up():
    """HIGH-3 回归测试 - 端到端: 涨停日 buy 必拒 (传真实 prev_close 后)

    前收 10.0 + 现价 11.5 (+15% 主板涨停) → can_buy 拒 → buy() 返回 None。
    """
    from app.backtest.simple_runner import FastEngine
    td_list = [date(2024, 1, d) for d in range(2, 11)]
    params = {"initial_capital": 1_000_000, "position_size": 50_000, "min_buy_amt": 5_000}
    eng = FastEngine(td_list, params)
    # 前收 10 + 现价 11.5 (+15% 涨停主板)
    res = eng.buy(date(2024, 1, 3), "600000", 11.5, prev_close=10.0)
    assert res is None, "涨停日应拒, got position object 是 bug"


def test_tdx_runner_no_sim_trader_import():
    """CRITICAL-1 回归测试 - 架构分层禁止:回测引擎不准反向依赖模拟盘运行态

    2026-07-15: tdx_runner 直接 `from app.sim_trader.config`
    把模拟盘运行态常量偷渡到回测引擎,导致回测结果与 sim_trader 配置漂移。
    修复后必须改走 `app.config.risk_params` 路径。
    """
    import ast
    from pathlib import Path
    src = Path("app/backtest/tdx_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert "sim_trader" not in mod, \
                f"tdx_runner 反向依赖 {mod}.{[n.name for n in node.names]} — 必须改走 risk_params"
        elif isinstance(node, ast.Import):
            for n in node.names:
                assert "sim_trader" not in n.name, \
                    f"tdx_runner 反向依赖 {n.name} — 必须改走 risk_params"


def test_sim_trader_config_derives_from_risk_params():
    """CRITICAL-1 回归测试 - sim_trader/config.py 的 risk 段必须派生自 risk_params

    防止有人改回 sim_trader/config.py 硬编码 risk 段常量,
    破坏 H6 修复(2026-07-14)的"唯一真相源"约束。
    """
    from app.sim_trader.config import HARD_STOP, TRAIL_ACTIVATE, TRAIL_DD, TAKE_PROFIT_TIERS
    from app.config.risk_params import load_risk_params
    rp = load_risk_params()
    assert HARD_STOP == rp.hard_stop
    assert TRAIL_ACTIVATE == rp.trail_activate
    assert TRAIL_DD == rp.trail_dd
    assert TAKE_PROFIT_TIERS == rp.take_profit_tiers


def test_tdx_runner_uses_risk_params_consistently():
    """CRITICAL-1 回归测试 - tdx_runner 函数体内不应再残留 sim_trader.config import

    AST 检查 + 字符串源码双重护栏,防有人绕过 AST 在函数体内字符串里 import。
    """
    import inspect
    from app.backtest.tdx_runner import run_tdx_backtest
    func_src = inspect.getsource(run_tdx_backtest)
    assert "from app.sim_trader.config" not in func_src, \
        "run_tdx_backtest 函数体内仍残留 sim_trader.config import — 必须清除"
    assert "app.config.risk_params" in func_src or "load_risk_params" in func_src, \
        "run_tdx_backtest 应改走 risk_params 路径,未发现 load_risk_params 引用"


# ── CRITICAL-2: 未知 order_type 不允许 unknown 进入 DB ────────────────

def _build_callback_handler(stubs):
    """组装 callback_handler + LiveCallback(供 CRITICAL-2 三测复用)。

    stubs: dict,可覆盖 store / kill_switch / audit / notify / clearance_lock / pnl_engine。
    """
    from app.live_trader.callback_handler import CallbackHandler
    from unittest.mock import MagicMock
    handler = CallbackHandler(
        stubs.get("config") or MagicMock(),
        store=stubs.get("store", MagicMock()),
        kill_switch=stubs.get("kill_switch", MagicMock()),
        clearance_lock=stubs.get("clearance_lock"),
        pnl_engine=stubs.get("pnl_engine"),
        notify=stubs.get("notify", MagicMock()),
        runtime_state=stubs.get("runtime_state", MagicMock(mode="live")),
        audit=stubs.get("audit", MagicMock()),
    )
    return handler, handler.make_xtquant_callback()


def _build_raw_trade(**kw):
    """构造 MagicMock 模拟 xtquant 回报对象。"""
    from unittest.mock import MagicMock
    trade = MagicMock()
    trade.traded_id = kw.get("traded_id", 9999)
    trade.order_id = kw.get("order_id", 12345)
    trade.stock_code = kw.get("stock_code", "600000.SH")
    trade.order_type = kw.get("order_type", 23)  # 默认 buy
    trade.traded_volume = kw.get("traded_volume", 100)
    trade.traded_price = kw.get("traded_price", 10.0)
    trade.traded_amount = kw.get("traded_amount", 1000.0)
    trade.commission = kw.get("commission", 5.0)
    return trade


def test_unknown_order_type_raises_and_activates_kill_switch():
    """CRITICAL-2: 未知 order_type 必 raise + kill_switch.activate + audit + 飞书告警,
    不允许 direction='unknown' 落 DB。

    直接调 _handle_trade 验证 raise 行为(单测硬验证)
    — on_stock_trade 那层会接 ValueError 隔离,见下一个测试。
    """
    import pytest
    from unittest.mock import MagicMock
    audit = MagicMock()
    notify = MagicMock()
    kill_switch = MagicMock()
    store = MagicMock()
    store.get_order.return_value = None
    handler, _cb = _build_callback_handler({
        "store": store,
        "kill_switch": kill_switch,
        "audit": audit,
        "notify": notify,
        "clearance_lock": None,
        "pnl_engine": None,
    })
    raw_trade = _build_raw_trade(order_type=99)

    # 必须 raise ValueError
    with pytest.raises(ValueError, match="未知 order_type"):
        handler._handle_trade(raw_trade)

    # 必须 kill_switch.activate
    assert kill_switch.activate.called, "kill_switch.activate 必调用"
    call_kwargs = kill_switch.activate.call_args.kwargs
    assert "order_type" in call_kwargs.get("reason", "") and "99" in call_kwargs.get("reason", ""), \
        f"reason 应含 order_type=99,got: {call_kwargs}"
    assert "callback_unknown_order_type" in call_kwargs.get("source", ""), \
        f"source 应标明 callback 来源,got: {call_kwargs}"

    # 必须 audit 留痕
    assert audit.log.called, "audit.log 必调用"
    audit_call = audit.log.call_args
    audit_kwargs = audit_call.kwargs or {}
    audit_args = audit_call.args or ()
    audit_data = audit_kwargs.get("data")
    if audit_data is None and len(audit_args) >= 3:
        audit_data = audit_args[2]
    assert audit_data is not None, "audit.log data 必传"
    assert audit_data.get("order_type") == 99
    assert audit_data.get("trade_id") == 9999
    assert audit_data.get("code") == "600000.SH"

    # 必须飞书告警
    assert notify.send.called, "飞书 notify.send 必调用"

    # 关键: deal 不能落 DB —— sync_terminal_write / apply_*_fill 都不能调
    assert not store.sync_terminal_write.called, \
        "unknown direction 不允许落 deal,store.sync_terminal_write 必不调"
    assert not store.apply_sell_fill.called, "未知方向不应触发 apply_sell_fill"
    assert not store.apply_buy_fill.called, "未知方向不应触发 apply_buy_fill"


def test_unknown_order_type_outer_try_isolates_callback_chain():
    """CRITICAL-2 硬约束: _handle_trade 即使 raise ValueError,
    外层 on_stock_trade 的 try/except 必须显式隔离,不能让回调链路整体挂掉。

    验证 on_stock_trade 调用后,后续正常回报仍能进 _handle_trade(链不断)。
    """
    from app.live_trader.callback_handler import CallbackHandler
    from unittest.mock import MagicMock
    audit = MagicMock()
    notify = MagicMock()
    kill_switch = MagicMock()
    store = MagicMock()
    store.get_order.return_value = None

    handler = CallbackHandler(
        MagicMock(),
        store=store,
        kill_switch=kill_switch,
        clearance_lock=None,
        pnl_engine=None,
        notify=notify,
        runtime_state=MagicMock(mode="live"),
        audit=audit,
    )
    cb = handler.make_xtquant_callback()

    # 1. 先来一笔 unknown —— on_stock_trade 内部 try/except 会接, 不外抛
    bad = _build_raw_trade(traded_id=1, order_type=99)
    cb.on_stock_trade(bad)  # 不应向上 raise
    assert kill_switch.activate.called, "kill_switch.activate 必须触发"

    # 2. 紧随一笔正常 buy —— 仍能进 _handle_trade,不被前一笔 raise 拖垮
    store.reset_mock()  # 重置 mock 计数看后续调用
    store.get_order.return_value = {"order_id": 11111, "mode": "live", "client_order_id": "abc"}
    good = _build_raw_trade(traded_id=2, order_id=11111, order_type=23)
    cb.on_stock_trade(good)  # 不 raise
    assert store.sync_terminal_write.called, \
        "后续正常 buy 仍必须落 DB,链路完整性保住"


def test_known_order_type_buy_still_works():
    """order_type=23 (buy) 走原路径不被新校验误伤。"""
    from app.live_trader.callback_handler import CallbackHandler
    from unittest.mock import MagicMock
    audit = MagicMock()
    notify = MagicMock()
    kill_switch = MagicMock()
    store = MagicMock()
    store.get_order.return_value = {"order_id": 12345, "mode": "live", "client_order_id": "abc"}
    clearance_lock = MagicMock()

    handler = CallbackHandler(
        MagicMock(),
        store=store,
        kill_switch=kill_switch,
        clearance_lock=clearance_lock,
        pnl_engine=None,
        notify=notify,
        runtime_state=MagicMock(mode="live"),
        audit=audit,
    )
    cb = handler.make_xtquant_callback()
    raw_trade = _build_raw_trade(order_type=23)
    cb.on_stock_trade(raw_trade)  # 不 raise
    assert store.sync_terminal_write.called, "buy deal 必须落 DB"
    assert not kill_switch.activate.called, "正常 buy 不应触发 kill_switch"


def test_known_order_type_sell_still_applies_position():
    """order_type=24 (sell) 仍调 apply_sell_fill。"""
    from app.live_trader.callback_handler import CallbackHandler
    from unittest.mock import MagicMock
    audit = MagicMock()
    notify = MagicMock()
    kill_switch = MagicMock()
    store = MagicMock()
    store.get_order.return_value = {"order_id": 12346, "mode": "live", "client_order_id": "abc"}
    store.get_position.return_value = {"code": "600000", "avg_cost": 9.0}
    pnl_engine = MagicMock()
    clearance_lock = MagicMock()

    handler = CallbackHandler(
        MagicMock(),
        store=store,
        kill_switch=kill_switch,
        clearance_lock=clearance_lock,
        pnl_engine=pnl_engine,
        notify=notify,
        runtime_state=MagicMock(mode="live"),
        audit=audit,
    )
    cb = handler.make_xtquant_callback()
    raw_trade = _build_raw_trade(traded_id=8889, order_id=12346, order_type=24, traded_price=11.0)
    cb.on_stock_trade(raw_trade)  # 不 raise
    assert store.sync_terminal_write.called, "sell deal 必须落 DB"
    assert store.apply_sell_fill.called, "sell 必须触发 apply_sell_fill"
    assert not kill_switch.activate.called, "正常 sell 不应触发 kill_switch"
