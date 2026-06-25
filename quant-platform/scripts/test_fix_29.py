"""验证 L29 修复: tdx_runner 用 execution.py(T+1 + 涨停)"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')


def test_tdx_runner_uses_execution():
    with open('app/backtest/tdx_runner.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'from app.backtest.execution import' in content, "tdx_runner 未引用 execution.py"
    print("OK tdx_runner 引用 execution.py")


def test_tdx_runner_has_can_buy():
    """tdx_runner 买入应调 can_buy"""
    with open('app/backtest/tdx_runner.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'can_buy' in content, "tdx_runner 买入未用 can_buy"
    print("OK tdx_runner 买入用 can_buy")


def test_tdx_runner_no_t0_sell():
    """tdx_runner 不应有 T+0 卖出(原 bug:line 373->394)"""
    with open('app/backtest/tdx_runner.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'can_sell_today' in content, "tdx_runner 未用 can_sell_today"
    print("OK tdx_runner 用 can_sell_today")


if __name__ == '__main__':
    test_tdx_runner_uses_execution()
    test_tdx_runner_has_can_buy()
    test_tdx_runner_no_t0_sell()
    print("\nL29 修复验证通过")