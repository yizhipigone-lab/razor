"""验证 L24 修复: strategy_coder AST 沙箱
通过 AST 静态分析拦截 LLM 生成的恶意 prompt 注入代码
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')

from app.utils.ast_sandbox import validate_strategy_code


def test_safe_code_passes():
    safe_code = """
import pandas as pd
def my_strategy(df):
    close = df['close']
    ma20 = close.rolling(20).mean()
    return (close > ma20).astype(int)
"""
    ok, msg = validate_strategy_code(safe_code)
    assert ok, f"正常代码被拒: {msg}"
    print(f"[OK] 正常策略代码通过: {msg}")


def test_os_system_blocked():
    """含 os.system 的代码应被拒"""
    bad_code = """
import os
def evil_strategy(df):
    os.system('rm -rf /')
    return 0
"""
    ok, msg = validate_strategy_code(bad_code)
    assert not ok, f"os.system 没被拦截!"
    assert 'os' in msg, f"错误消息应提到 os, 实际: {msg}"
    print(f"[OK] os.system 被拒: {msg}")


def test_subprocess_blocked():
    bad_code = """
import subprocess
def evil(df):
    subprocess.run(['ls'])
    return 0
"""
    ok, msg = validate_strategy_code(bad_code)
    assert not ok, "subprocess 没被拦截"
    print(f"[OK] subprocess 被拒: {msg}")


def test_eval_exec_blocked():
    bad_code = """
def evil(df):
    eval('os.system("rm")')
    return 0
"""
    ok, msg = validate_strategy_code(bad_code)
    assert not ok, "eval 没被拦截"
    print(f"[OK] eval 被拒: {msg}")


def test_strategy_coder_uses_validation():
    """strategy_coder 加载代码前应调 validate"""
    src_path = os.path.join(
        os.path.dirname(__file__), '..', 'app', 'agents', 'strategy_coder.py'
    )
    with open(src_path, 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'validate_strategy_code' in content, "strategy_coder 没调 validate"
    print("[OK] strategy_coder 加载前调 validate")


if __name__ == '__main__':
    test_safe_code_passes()
    test_os_system_blocked()
    test_subprocess_blocked()
    test_eval_exec_blocked()
    test_strategy_coder_uses_validation()
    print("\n[ALL PASS] L24 修复验证通过")
