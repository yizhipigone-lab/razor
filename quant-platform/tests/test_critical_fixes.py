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


def test_simple_runner_buy_requires_real_prev_close():
    """HIGH-3 回归测试 - simple_runner.buy 缺 prev_close 必 raise

    2026-07-15: 涨停过滤实化 — buy() 必须传真实 prev_close=None 时显式 raise,
    不复刻 'prev_close = px' 静默失效的 bug。
    """
    import pytest
    from app.backtest.simple_runner import FastEngine
    td_list = [date(2024, 1, d) for d in range(2, 11)]
    params = {
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "min_buy_amt": 5_000,
    }
    eng = FastEngine(td_list, params)
    with pytest.raises(ValueError, match="缺真实 prev_close"):
        eng.buy(date(2024, 1, 3), "600000", 11.5, prev_close=None)


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
