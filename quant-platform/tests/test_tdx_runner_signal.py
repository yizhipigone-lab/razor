"""
测试 tdx_runner 的 _pick_signal_var 和 _is_signal_value
确保上层消费者能用任意变量名 + 任意 Value
"""
import sys
from pathlib import Path
import importlib.util

import pytest

TXRUNNER_PATH = Path(r"e:\1target\p9_project\quant-platform\app\backtest\tdx_runner.py")


@pytest.fixture(scope="module")
def txrunner():
    """加载 tdx_runner (mock 掉 tqcenter 依赖)"""
    class MockTQ:
        def __getattr__(self, name):
            return lambda *a, **k: {}
    sys.modules['tqcenter'] = type('M', (), {'tq': MockTQ()})()
    spec = importlib.util.spec_from_file_location("tdx_runner", str(TXRUNNER_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# === _pick_signal_var ===

def test_pick_signal_var_zp(txrunner):
    d = {"Date": [...], "ZP": [1, 0, 1]}
    assert txrunner._pick_signal_var(d) == "ZP"


def test_pick_signal_var_zt(txrunner):
    d = {"Date": [...], "ZT": [100, 0, 100]}
    assert txrunner._pick_signal_var(d) == "ZT"


def test_pick_signal_var_chinese(txrunner):
    d = {"Date": [...], "追跌反弹": [1, 0]}
    assert txrunner._pick_signal_var(d) == "追跌反弹"


def test_pick_signal_var_empty(txrunner):
    assert txrunner._pick_signal_var({}) == "ZP"  # 兜底


def test_pick_signal_var_only_date(txrunner):
    """只有 Date 字段 (没有信号) → 兜底返回 'ZP'"""
    assert txrunner._pick_signal_var({"Date": []}) == "ZP"


# === _is_signal_value ===

def test_is_signal_value_zero(txrunner):
    assert txrunner._is_signal_value("0") is False
    assert txrunner._is_signal_value("0.0") is False
    assert txrunner._is_signal_value("") is False
    assert txrunner._is_signal_value(None) is False


def test_is_signal_value_one(txrunner):
    assert txrunner._is_signal_value("1") is True


def test_is_signal_value_hundred(txrunner):
    assert txrunner._is_signal_value("100") is True


# === 集成：模拟 tdx_runner 的 "intraday" 段核心逻辑 ===
# 验证：如果 worker 把信号存在 `d["ZT"]` 而非 `d["ZP"]`，
# tdx_runner 仍然能识别 has_any=True

def test_intraday_signal_detection_with_zt(txrunner):
    """模拟 tdx_runner._run_intraday_backtest 中的关键逻辑"""
    raw_signals = {
        "605289.SH": {"Date": ["20260101", "20260102"], "ZT": ["0", "100"]},
    }
    # 模拟 tdx_runner 解析逻辑
    sig_by_code = {}
    all_signal_codes = set()
    for code, d in raw_signals.items():
        code_num = code.split(".")[0]
        dates_list = d.get("Date", [])
        var_name = txrunner._pick_signal_var(d)
        zps = d.get(var_name, [])
        code_sigs = {}
        has_any = False
        for dt, zp in zip(dates_list, zps):
            if txrunner._is_signal_value(zp):
                has_any = True
                code_sigs[dt] = zp
        if has_any:
            sig_by_code[code_num] = code_sigs
            all_signal_codes.add(code_num)
    assert "605289" in all_signal_codes
    assert sig_by_code["605289"]["20260102"] == "100"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])