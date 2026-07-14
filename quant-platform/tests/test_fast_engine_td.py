"""FastEngine._td 持仓天数计算单元测试

锁定 _td 的语义：返回 td_list 中落在 [d1, d2] 闭区间的元素个数（含首尾）。
作为 _td 查表化优化（i2-i1+1）的回归保护——优化前后结果必须完全一致。

参考实现（oracle）= 原线性扫描：sum(1 for t in td_list if d1 <= t <= d2)
"""
from datetime import date

from app.backtest.simple_runner import FastEngine


def _make_engine(td_list):
    """构造最小 FastEngine 实例，仅用于 _td 测试"""
    return FastEngine(td_list, {
        'initial_capital': 1_000_000,
        'position_size': 50_000,
    })


def _ref(td_list, d1, d2):
    """参考实现（原线性扫描），作为 oracle"""
    return sum(1 for t in td_list if d1 <= t <= d2)


class TestTdSemantic:
    """_td 必须与参考实现（闭区间计数）完全一致"""

    def setup_method(self):
        self.td_list = [
            date(2026, 6, 22), date(2026, 6, 23), date(2026, 6, 24),
            date(2026, 6, 25), date(2026, 6, 26),
        ]
        self.eng = _make_engine(self.td_list)

    def test_same_day(self):
        """d1 == d2 都在表：返回 1"""
        assert self.eng._td(date(2026, 6, 22), date(2026, 6, 22)) == 1

    def test_range_inclusive(self):
        """d1 < d2 都在表：含首尾计数"""
        assert self.eng._td(date(2026, 6, 22), date(2026, 6, 24)) == 3
        assert self.eng._td(date(2026, 6, 22), date(2026, 6, 26)) == 5
        assert self.eng._td(date(2026, 6, 23), date(2026, 6, 25)) == 3

    def test_d1_before_list(self):
        """d1 早于表首（不在表）：走 fallback，等价参考"""
        d1, d2 = date(2026, 6, 1), date(2026, 6, 24)
        assert self.eng._td(d1, d2) == _ref(self.td_list, d1, d2)

    def test_d2_after_list(self):
        """d2 晚于表尾（不在表）：走 fallback"""
        d1, d2 = date(2026, 6, 23), date(2026, 6, 30)
        assert self.eng._td(d1, d2) == _ref(self.td_list, d1, d2)

    def test_both_outside(self):
        """d1/d2 都不在表：走 fallback"""
        d1, d2 = date(2026, 6, 1), date(2026, 6, 30)
        assert self.eng._td(d1, d2) == _ref(self.td_list, d1, d2)

    def test_d1_greater_than_d2(self):
        """d1 > d2：返回 0（max(0,...) 防御负数，与原 sum 一致）"""
        assert self.eng._td(date(2026, 6, 25), date(2026, 6, 23)) == 0  # 都在表
        assert self.eng._td(date(2026, 6, 25), date(2026, 6, 1)) == 0   # d2 不在表

    def test_empty_td_list(self):
        """空 td_list：返回 0，不崩"""
        eng = _make_engine([])
        assert eng._td(date(2026, 6, 22), date(2026, 6, 24)) == 0

    def test_single_day_list(self):
        """单元素 td_list"""
        eng = _make_engine([date(2026, 6, 22)])
        assert eng._td(date(2026, 6, 22), date(2026, 6, 22)) == 1
        assert eng._td(date(2026, 6, 22), date(2026, 6, 23)) == 1  # d2 不在表

    def test_randomized_equivalence(self):
        """随机化对比：各种 (d1,d2) 组合，_td 必须等价参考实现"""
        import random
        rng = random.Random(42)
        candidates = [date(2026, 6, d) for d in range(15, 30)]  # 含表内外日期
        for _ in range(200):
            d1 = rng.choice(candidates)
            d2 = rng.choice(candidates)
            got = self.eng._td(d1, d2)
            exp = _ref(self.td_list, d1, d2)
            assert got == exp, f"_td({d1},{d2})={got} != ref {exp}"


class TestTdSubclassCompat:
    """子类若重写 __init__ 不调 super（缺 _td_index），_td 必须走 fallback 不崩"""

    def test_subclass_without_td_index(self):
        """模拟 monkey-patch 子类（如 scripts/test_atr.py 的 ATREngine 风格）
        重写 __init__ 不调 super → 实例无 _td_index → _td 应走 getattr fallback"""
        class BareEngine(FastEngine):
            def __init__(self, td_list):
                # 故意不调 super().__init__
                self.td_list = td_list
                self.p = {}

        eng = BareEngine([date(2026, 6, 22), date(2026, 6, 23), date(2026, 6, 24)])
        assert eng._td(date(2026, 6, 22), date(2026, 6, 24)) == 3
        assert eng._td(date(2026, 6, 22), date(2026, 6, 22)) == 1
