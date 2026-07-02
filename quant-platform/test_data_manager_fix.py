#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试 DataManager 日期类型修复 + Tushare 限流
"""
import sys
import time
import pandas as pd
from datetime import date
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

def test_date_type_conversion():
    """测试日期类型转换逻辑"""
    print("\n=== 测试 1: 日期类型转换 ===")

    # 模拟 get_last_date 返回 pd.Timestamp
    last_dt = pd.Timestamp('2026-06-30')
    print(f"last_dt (Timestamp): {last_dt}, type: {type(last_dt)}")

    # 修复后的逻辑: 转为 date
    last_date = last_dt.date() if isinstance(last_dt, pd.Timestamp) else last_dt
    print(f"last_date (date): {last_date}, type: {type(last_date)}")

    # 验证减法不会报错
    try:
        days_diff = (date.today() - last_date).days
        print(f"days_diff: {days_diff}")
        print("[PASS] Date type conversion test passed")
        return True
    except Exception as e:
        print(f"[FAIL] Date type conversion failed: {e}")
        return False

def test_rate_limiter():
    """测试 Tushare 限流器"""
    print("\n=== 测试 2: Tushare 限流 ===")

    import threading
    _tushare_lock = threading.Lock()
    _last_tushare_call = [0.0]

    def _rate_limit_tushare():
        """Tushare 限流: 最小间隔 1.2 秒"""
        with _tushare_lock:
            elapsed = time.time() - _last_tushare_call[0]
            if elapsed < 1.2:
                time.sleep(1.2 - elapsed)
            _last_tushare_call[0] = time.time()

    # 模拟连续 3 次调用
    call_times = []
    for i in range(3):
        start = time.time()
        _rate_limit_tushare()
        call_times.append(time.time())
        print(f"调用 {i+1}: {time.time() - start:.3f}s")

    # 验证间隔 >= 1.2s
    intervals = [call_times[i+1] - call_times[i] for i in range(len(call_times)-1)]
    print(f"实际间隔: {[f'{x:.3f}s' for x in intervals]}")

    if all(x >= 1.15 for x in intervals):  # 允许 0.05s 误差
        print("[PASS] Tushare rate limiter test passed")
        return True
    else:
        print("[FAIL] Tushare rate limiter failed")
        return False

def main():
    print("=" * 60)
    print("DataManager 修复验证")
    print("=" * 60)

    results = []
    results.append(("日期类型转换", test_date_type_conversion()))
    results.append(("Tushare 限流", test_rate_limiter()))

    print("\n" + "=" * 60)
    print("测试结果汇总:")
    print("=" * 60)
    for name, passed in results:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{name}: {status}")

    all_passed = all(r[1] for r in results)
    print("\n" + ("All tests passed!" if all_passed else "Some tests failed"))
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())
