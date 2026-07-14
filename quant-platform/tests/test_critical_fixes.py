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


def test_simple_runner_buy_without_prev_close_warns_skips_filter(caplog):
    """HIGH-3 (compat 4b) 回归测试 - 缺 prev_close → logger.warning + 走无涨停过滤路径

    上一版该测试断言 raise,现改为 assert warning 日志出现 + 不崩。
    raise-严格模式不在 buy() 内部做,留给主回测路径(传入真实 prev_close)。
    scripts/ 下 22 个 CLI 用旧三参 eng.buy(d, code, px) 不应崩 — 此测试守住兼容降级。
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
        res = eng.buy(date(2024, 1, 3), "600000", 11.5, prev_close=None)
    # 缺 prev_close 时 buy 不应该崩,且能"模拟买入"或拒(取决于其它约束)
    assert res is None or getattr(res, "code", None) == "600000", \
        "降级路径应静默继续,不 raise"
    assert any("缺 prev_close" in r.message for r in caplog.records), \
        "应 logger.warning 显式记录"


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
