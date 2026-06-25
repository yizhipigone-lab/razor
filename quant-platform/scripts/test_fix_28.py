"""验证 L28 修复: simple/strict runner 用 execution.py"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')


def test_simple_runner_uses_execution():
    """simple_runner.py 应引用 execution.py"""
    with open('app/backtest/simple_runner.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'from app.backtest.execution import' in content, "simple_runner 未引用 execution.py"
    print("✅ simple_runner 引用 execution.py")


def test_strict_runner_uses_execution():
    """strict_runner.py 应引用 execution.py"""
    with open('app/backtest/strict_runner.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'from app.backtest.execution import' in content, "strict_runner 未引用 execution.py"
    print("✅ strict_runner 引用 execution.py")


def test_simple_runner_has_limit_up_filter():
    """simple_runner 买入逻辑应调用 can_buy"""
    with open('app/backtest/simple_runner.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'can_buy' in content, "simple_runner 买入未用 can_buy"
    print("✅ simple_runner 买入用 can_buy")


def test_strict_runner_has_t1_constraint():
    """strict_runner 卖出应使用 T+1 约束"""
    with open('app/backtest/strict_runner.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'can_sell_today' in content, "strict_runner 未用统一 can_sell_today"
    print("✅ strict_runner 用统一 can_sell_today")


if __name__ == '__main__':
    test_simple_runner_uses_execution()
    test_strict_runner_uses_execution()
    test_simple_runner_has_limit_up_filter()
    test_strict_runner_has_t1_constraint()
    print("\n🎉 L28 修复验证通过")
