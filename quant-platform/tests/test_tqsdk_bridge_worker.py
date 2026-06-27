"""
测试 worker 的核心逻辑:
- _is_signal_value() 归一化
- 三个函数 (_do_screen / _do_range / _probe_formulas) 的 result 解析
- 不直接依赖 TDX (TDX 不可用也能跑测试)

可以用 pytest 直接跑 (不需要 TDX 连接)
"""
import sys
from pathlib import Path
import importlib.util

import pytest

WORKER_PATH = Path(r"e:\1target\p9_project\quant-platform\app\tqsdk\worker\tqsdk_bridge_worker.py")


@pytest.fixture(scope="module")
def worker():
    """加载 worker 模块 (mock tqcenter)"""
    class MockTQ:
        def __getattr__(self, name):
            return lambda *a, **k: {}
    sys.modules['tqcenter'] = type('M', (), {'tq': MockTQ()})()
    spec = importlib.util.spec_from_file_location("tqsdk_bridge_worker", str(WORKER_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# === _is_signal_value 测试 ===

def test_is_signal_value_zero(worker):
    """0/0.0/空字符串/None → 不命中"""
    assert worker._is_signal_value("0") is False
    assert worker._is_signal_value("0.0") is False
    assert worker._is_signal_value("") is False
    assert worker._is_signal_value(None) is False


def test_is_signal_value_one(worker):
    """'1' → 命中"""
    assert worker._is_signal_value("1") is True


def test_is_signal_value_hundred(worker):
    """'100' → 命中 (GUPIAO_011 返回值)"""
    assert worker._is_signal_value("100") is True


def test_is_signal_value_decimal(worker):
    """'0.5' → 命中 (非零)"""
    assert worker._is_signal_value("0.5") is True


def test_is_signal_value_negative(worker):
    """负数 (-0.5) → 命中 (非零)"""
    assert worker._is_signal_value("-0.5") is True


def test_is_signal_value_invalid_string(worker):
    """非数字字符串 → False (保守)"""
    assert worker._is_signal_value("abc") is False


# === _do_screen 解析测试 ===

def test_do_screen_finds_signal_with_zp_var(worker):
    """ZP=1 命中"""
    fake_result = {
        "605289.SH": {
            "ZP": [
                {"Date": "20260626", "Value": "0"},
                {"Date": "20260627", "Value": "1"},
            ]
        },
        "ErrorId": "0",
    }
    matched = worker._do_screen_parse(fake_result, "20260627")
    assert "605289.SH" in matched


def test_do_screen_finds_signal_with_zt_var(worker):
    """ZT=100 (GUPIAO_011) 命中"""
    fake_result = {
        "605289.SH": {
            "ZT": [
                {"Date": "20260626", "Value": "0"},
                {"Date": "20260627", "Value": "100"},
            ]
        },
        "ErrorId": "0",
    }
    matched = worker._do_screen_parse(fake_result, "20260627")
    assert "605289.SH" in matched


def test_do_screen_finds_signal_with_chinese_var_name(worker):
    """中文变量名 ('追跌反弹') 也能命中"""
    fake_result = {
        "605289.SH": {
            "追跌反弹": [
                {"Date": "20260627", "Value": "1"},
            ]
        },
        "ErrorId": "0",
    }
    matched = worker._do_screen_parse(fake_result, "20260627")
    assert "605289.SH" in matched


def test_do_screen_skips_zero(worker):
    """Value=0 不命中"""
    fake_result = {
        "605289.SH": {
            "ZP": [{"Date": "20260627", "Value": "0"}]
        },
        "ErrorId": "0",
    }
    matched = worker._do_screen_parse(fake_result, "20260627")
    assert "605289.SH" not in matched


def test_do_screen_skips_wrong_date(worker):
    """日期不匹配 end_time 不命中"""
    fake_result = {
        "605289.SH": {
            "ZP": [{"Date": "20260626", "Value": "1"}]
        },
        "ErrorId": "0",
    }
    matched = worker._do_screen_parse(fake_result, "20260627")
    assert "605289.SH" not in matched


# === _do_range 解析测试 ===

def test_do_range_finds_signals_with_zt_var(worker):
    """ZT 字段含 100 也算命中"""
    fake_result = {
        "605289.SH": {
            "ZT": [
                {"Date": "20260101", "Value": "0"},
                {"Date": "20260102", "Value": "100"},
                {"Date": "20260103", "Value": "0"},
            ]
        },
        "ErrorId": "0",
    }
    has_signal, hit_var, dates, values = worker._do_range_check_signal(fake_result["605289.SH"], "605289.SH")
    assert has_signal is True
    assert hit_var == "ZT"
    assert len(dates) == 3
    assert values[1] == "100"


def test_do_range_no_signal(worker):
    fake_result = {
        "605289.SH": {
            "ZT": [
                {"Date": "20260101", "Value": "0"},
                {"Date": "20260102", "Value": "0"},
            ]
        },
        "ErrorId": "0",
    }
    has_signal, hit_var, dates, values = worker._do_range_check_signal(fake_result["605289.SH"], "605289.SH")
    assert has_signal is False
    assert hit_var is None


# === _probe_formulas 归一化测试 ===

def test_probe_normalizes_value(worker):
    """探测时 Value=100 也算 trigger_count"""
    fake_r = {
        "605289.SH": {
            "ZT": [
                {"Date": "20260101", "Value": "0"},
                {"Date": "20260102", "Value": "100"},
            ]
        }
    }
    trigger_count, last_trigger, hit_var = worker._probe_check_signal(fake_r["605289.SH"], "605289.SH", "GUPIAO_011")
    assert trigger_count == 1
    assert last_trigger == "20260102"
    assert hit_var == "ZT"


def test_probe_no_trigger(worker):
    fake_r = {
        "605289.SH": {
            "ZT": [{"Date": "20260101", "Value": "0"}]
        }
    }
    trigger_count, last_trigger, hit_var = worker._probe_check_signal(fake_r["605289.SH"], "605289.SH", "GUPIAO_011")
    assert trigger_count == 0
    assert last_trigger is None
    assert hit_var is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])