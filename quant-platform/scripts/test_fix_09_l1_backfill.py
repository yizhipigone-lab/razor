"""验证 L1 修复 (Task 9 / current batch): backfill_daily_tushare.py 中 amount 不被 x 1000

注: 文件名用 test_fix_09_l1_backfill.py 而不是 test_fix_09.py,因为
    test_fix_09.py 已被之前批次 Task 9 (#9 _prev_day_snap 修复) 占用。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')


def test_no_multiply_1000_in_source():
    """backfill_daily_tushare.py 源码中不应再有 amount * 1000"""
    with open('scripts/backfill_daily_tushare.py', 'r', encoding='utf-8') as f:
        content = f.read()
    bad_patterns = [
        "df['amount'] = (df['amount'] * 1000)",
        'df["amount"] = (df["amount"] * 1000)',
    ]
    for pattern in bad_patterns:
        assert pattern not in content, f"仍存在错误模式: {pattern}"
    print("[OK] backfill_daily_tushare.py 无 amount * 1000")


if __name__ == '__main__':
    test_no_multiply_1000_in_source()
    print("\n[SUCCESS] L1 修复验证通过")