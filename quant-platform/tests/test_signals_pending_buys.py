"""验证改动2（信号反向索引）的语义等价性

pending_buys 预建 + sorted(pending_buys[d_str]) 必须与原
sorted(code for code, sigs in sig_by_code.items() if _is_signal_value(sigs.get(d_str)))
产出完全一致的列表。保证买入顺序、资金分配、成交结果不变。
"""
from collections import defaultdict

from app.backtest.tdx_runner import _is_signal_value


def _old_signals_today(sig_by_code, d_str):
    """原逻辑：每天全量扫描 sig_by_code（O(天×股)）"""
    return sorted(code for code, sigs in sig_by_code.items() if _is_signal_value(sigs.get(d_str)))


def _new_signals_today(sig_by_code, d_str):
    """新逻辑：预建反向索引，主循环 O(1) 取当天信号"""
    pending_buys = defaultdict(list)
    for code, sigs in sig_by_code.items():
        for dt_str, zp in sigs.items():
            if _is_signal_value(zp):
                pending_buys[dt_str].append(code)
    return sorted(pending_buys.get(d_str, []))


class TestSignalsTodayEquiv:
    """新老两种 signals_today 实现必须产出完全一致的列表"""

    def test_empty_sig_by_code(self):
        sig_by_code = {}
        for d in ['20260101', '20260102']:
            assert _new_signals_today(sig_by_code, d) == _old_signals_today(sig_by_code, d) == []

    def test_no_signal_that_day(self):
        sig_by_code = {'000001': {'20260101': '1'}}
        assert _new_signals_today(sig_by_code, '20260102') == _old_signals_today(sig_by_code, '20260102') == []

    def test_single_signal(self):
        sig_by_code = {'000001': {'20260101': '1'}}
        assert _new_signals_today(sig_by_code, '20260101') == ['000001']
        assert _old_signals_today(sig_by_code, '20260101') == ['000001']

    def test_zero_value_not_signal(self):
        """signal_value='0'/'0.0'/'' 都不算信号"""
        sig_by_code = {'000001': {'20260101': '0'}, '000002': {'20260101': '1'},
                       '000003': {'20260101': '0.0'}, '000004': {'20260101': ''}}
        assert _new_signals_today(sig_by_code, '20260101') == ['000002']
        assert _old_signals_today(sig_by_code, '20260101') == ['000002']

    def test_multiple_codes_sorted_by_code(self):
        """多只同日有信号：按 code 字符串升序（保证买入顺序确定性）"""
        sig_by_code = {
            '600519': {'20260101': '1'},
            '000001': {'20260101': '100'},
            '300750': {'20260101': '0.5'},
        }
        expected = ['000001', '300750', '600519']
        assert _new_signals_today(sig_by_code, '20260101') == expected
        assert _old_signals_today(sig_by_code, '20260101') == expected

    def test_code_with_signals_on_multiple_days(self):
        """同一只股票多日有信号：每天独立判断"""
        sig_by_code = {'000001': {'20260101': '1', '20260102': '0', '20260103': '1'}}
        assert _new_signals_today(sig_by_code, '20260101') == ['000001']
        assert _new_signals_today(sig_by_code, '20260102') == []
        assert _new_signals_today(sig_by_code, '20260103') == ['000001']

    def test_randomized_equivalence(self):
        """随机化对比：各种 sig_by_code + 日期组合，新老必须一致"""
        import random
        rng = random.Random(7)
        codes = [f'{i:06d}' for i in range(50)]
        dates = [f'202601{d:02d}' for d in range(1, 32)]
        sig_vals = ['0', '0.0', '', '1', '100', '0.5', '2']
        for _ in range(100):
            sig_by_code = {}
            for code in codes:
                picked = rng.sample(dates, rng.randint(0, 10))
                sig_by_code[code] = {d: rng.choice(sig_vals) for d in picked}
            for d in dates:
                assert _new_signals_today(sig_by_code, d) == _old_signals_today(sig_by_code, d), \
                    f"mismatch on date {d}"
