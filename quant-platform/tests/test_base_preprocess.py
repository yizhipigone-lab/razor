"""base.preprocess_bars 单元测试(候选⑤)。

锁定 base 统一过滤流水线的契约:ST/退市/北交所/停牌/涨停;子类 LIMIT_TABLE 覆盖;filter_bj_pattern 自定义。
"""
import pandas as pd
import pytest

from app.screener.strategies.base import preprocess_bars, BaseStrategy


def _bars(rows, has_volume=True):
    """rows: list of dict with code/date/open/high/low/close/name/volume."""
    if not has_volume:
        for r in rows:
            r.pop("volume", None)
    return pd.DataFrame(rows)


_PARAMS_DEFAULT = {}  # preprocess_bars 默认值:全过滤=True


# ── ST / 退市 ──────────────────────────────────────────
class TestSTFilter:
    def test_st_name_filtered(self):
        df = _bars([
            {"code": "000001", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "平安银行", "volume": 1000},
            {"code": "000002", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "*ST 测试", "volume": 1000},
            {"code": "000003", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "退市测试", "volume": 1000},
        ])
        out = preprocess_bars(df, _PARAMS_DEFAULT)
        codes = set(out["code"])
        assert "000001" in codes
        assert "000002" not in codes
        assert "000003" not in codes

    def test_filter_st_false_disables(self):
        df = _bars([
            {"code": "000002", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "*ST 测试", "volume": 1000},
        ])
        out = preprocess_bars(df, {"filter_st": False})
        assert "000002" in set(out["code"])


# ── 北交所 ──────────────────────────────────────────────
class TestBJFilter:
    def test_default_pattern_filters_8_and_4_prefix(self):
        df = _bars([
            {"code": "000001", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "A", "volume": 1000},
            {"code": "830001", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "B", "volume": 1000},  # 8 开头
            {"code": "430001", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "C", "volume": 1000},  # 4 开头
            {"code": "300001", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "D", "volume": 1000},  # 3 开头(创业板,保)
        ])
        out = preprocess_bars(df, _PARAMS_DEFAULT)
        codes = set(out["code"])
        assert "000001" in codes
        assert "830001" not in codes
        assert "430001" not in codes
        assert "300001" in codes  # 3 开头不是北交所

    def test_custom_pattern_preserves_4_prefix(self):
        # filter_bj_pattern=r"^8" — ma5_angle_cross 的旧行为:只滤 '8',保 '4'
        df = _bars([
            {"code": "830001", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "B", "volume": 1000},
            {"code": "430001", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "C", "volume": 1000},
        ])
        out = preprocess_bars(df, {"filter_bj_pattern": r"^8"})
        codes = set(out["code"])
        assert "830001" not in codes
        assert "430001" in codes  # 自定义 pattern 保留 '4'


# ── 停牌 ──────────────────────────────────────────────
class TestSuspendFilter:
    def test_zero_volume_filtered(self):
        df = _bars([
            {"code": "000001", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "A", "volume": 0},
            {"code": "000002", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "B", "volume": 500},
        ])
        out = preprocess_bars(df, _PARAMS_DEFAULT)
        assert "000001" not in set(out["code"])
        assert "000002" in set(out["code"])


# ── 涨停(per-code 真实日收益)─────────────────────────────
class TestLimitUpFilter:
    def _bars_2days(self):
        # 同 code 两日:第 1 日 close=10,第 2 日 close=11.5(+15%,触发 688 阈值 0.199)
        return _bars([
            {"code": "688001", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "A", "volume": 1000},
            {"code": "688001", "date": "2024-01-03", "open": 11, "high": 11.5, "low": 11, "close": 11.5, "name": "A", "volume": 1000},
        ])

    def test_default_threshold_drops_limit_up_688(self):
        out = preprocess_bars(self._bars_2days(), _PARAMS_DEFAULT)
        # 688 默认 0.199 → +15% 不到,保留
        assert len(out) == 2
        out2 = preprocess_bars(
            _bars([
                {"code": "688001", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "A", "volume": 1000},
                {"code": "688001", "date": "2024-01-03", "open": 11, "high": 12, "low": 11, "close": 12, "name": "A", "volume": 1000},  # +20% ≥ 0.199
            ]),
            _PARAMS_DEFAULT,
        )
        # +20% 触发 688 阈值 0.199 → 丢
        assert len(out2) == 1  # 只剩第 1 日(第 2 日被丢)

    def test_skip_limit_up_false_keeps_all(self):
        out = preprocess_bars(self._bars_2days(), {"skip_limit_up": False})
        assert len(out) == 2

    def test_custom_limit_table_uses_threshold(self):
        # LIMIT_TABLE={'688': 0.10} → +15% ≥ 0.10 → 丢
        out = preprocess_bars(
            self._bars_2days(),
            _PARAMS_DEFAULT,
            limit_table={"688": 0.10},
        )
        assert len(out) == 1  # 第 2 日(+15%)被丢


# ── 无假默认 + LIMIT_TABLE 覆盖(基类契约)──────────────
class TestNoFakeDefaults:
    def test_all_filters_default_true(self):
        # 缺省参数:全过滤启用(候选⑤ 消除旧 -7/15 假默认)
        df = _bars([
            {"code": "830001", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "*ST", "volume": 0},  # ST+北交+停牌
            {"code": "000001", "date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "name": "正常", "volume": 1000},
        ])
        out = preprocess_bars(df, _PARAMS_DEFAULT)
        assert "830001" not in set(out["code"])
        assert "000001" in set(out["code"])

    def test_empty_bars_returns_empty(self):
        assert preprocess_bars(pd.DataFrame(), _PARAMS_DEFAULT).empty
        assert preprocess_bars(None, _PARAMS_DEFAULT) is None


# 注:class-method 委托路径(self.preprocess → preprocess_bars 用 self.LIMIT_TABLE)
# 候选⑤ 的核心契约由上面 10 个 free function 测试覆盖(同一 preprocess_bars)。
# 类委托是单行:`return preprocess_bars(bars, self.params or {}, self.LIMIT_TABLE, self.LIMIT_MAIN_PCT)`,
# 编译 + 引擎调用 `strategy.preprocess(bars)` 即隐式覆盖;不重复单测。