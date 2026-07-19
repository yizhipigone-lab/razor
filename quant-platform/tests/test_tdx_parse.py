"""tdx_parse 向量化解析的边界用例测试

对照旧 tdx_runner 逐行解析语义(2026-07-18 性能改造):
- 信号判定修复口径: "nan" → 非信号; "0"/"0.0"/""/None/非法串 → 非信号; 其余 → 信号
- OHLC 无效行(NaN/<=0): 日线逐行 fallback close; 日内整股翻转(首行脏数据后该股永久 fallback)
- code 去后缀、日期过滤、close 缺失行不进 prices
"""
import numpy as np
import pandas as pd
import pytest

from app.backtest.tdx_parse import parse_daily, parse_intraday, _signal_mask


def _df(rows):
    """rows: list of (code, date, signal_value, open, high, low, close)"""
    return pd.DataFrame(rows, columns=[
        "code", "date", "signal_value", "open", "high", "low", "close"])


class TestSignalMask:
    @pytest.mark.parametrize("val,expected", [
        ("1", True), ("100", True), ("0.5", True), ("-1", True), (" 1 ", True),
        ("0", False), ("0.0", False), ("0.00", False), ("", False),
        (None, False), ("abc", False),
        ("nan", False), ("NaN", False), ("-nan", False),  # 修复口径: NaN 非信号
    ])
    def test_signal_values(self, val, expected):
        df = _df([("000001.SZ", "20250501", val, 1, 1, 1, 1)])
        assert _signal_mask(df).iloc[0] == expected


class TestParseDaily:
    def test_basic(self):
        df = _df([
            ("000001.SZ", "20250501", "1", 10.0, 11.0, 9.0, 10.5),
            ("000001.SZ", "20250502", "0", 10.5, 11.5, 10.0, 11.0),
            ("600001.SH", "20250501", "0.5", 20.0, 21.0, 19.0, 20.5),
        ])
        sig, px = parse_daily(df)
        # 信号: 只存非零日期, code 去后缀
        assert set(sig.keys()) == {"000001", "600001"}
        assert list(sig["000001"].keys()) == ["2025-05-01"]
        assert sig["600001"]["2025-05-01"] == "0.5"
        # 价格: 全行都有, key 为 ISO 日期
        assert px["2025-05-01"]["000001"] == {"close": 10.5, "high": 11.0, "low": 9.0, "open": 10.0}
        assert px["2025-05-02"]["000001"]["close"] == 11.0
        assert px["2025-05-01"]["600001"]["low"] == 19.0

    def test_invalid_ohlc_row_fallback_per_row(self):
        """日线: 无效 OHLC 行逐行 fallback, 不翻转整股"""
        df = _df([
            ("000001.SZ", "20250501", "0", 10.0, 11.0, 9.0, 10.5),
            ("000001.SZ", "20250502", "0", 0.0, 0.0, 0.0, 11.0),      # 无效行
            ("000001.SZ", "20250503", "0", 11.0, 12.0, 10.5, 11.5),   # 后续行不受影响
        ])
        _, px = parse_daily(df)
        assert px["2025-05-02"]["000001"] == {"close": 11.0, "high": 11.0, "low": 11.0, "open": 11.0}
        assert px["2025-05-03"]["000001"]["low"] == 10.5  # 日线逐行, 不翻转

    def test_nan_ohlc_fallback(self):
        df = _df([
            ("000001.SZ", "20250501", "0", np.nan, np.nan, np.nan, 10.5),
        ])
        _, px = parse_daily(df)
        assert px["2025-05-01"]["000001"]["low"] == 10.5

    def test_close_nan_row_excluded(self):
        """close 缺失的行不进 prices(等价旧 df_to_signals_prices 的 sub 选择)"""
        df = _df([
            ("000001.SZ", "20250501", "0", 10.0, 11.0, 9.0, np.nan),
        ])
        _, px = parse_daily(df)
        assert "2025-05-01" not in px

    def test_empty(self):
        assert parse_daily(None) == ({}, {})
        assert parse_daily(_df([])) == ({}, {})


class TestParseIntraday:
    def test_flip_per_code(self):
        """日内: 首行脏数据后该股永久翻转(复刻旧 has_ohlc 整股翻转语义)"""
        df = _df([
            ("000001.SZ", "20250501", "0", 10.0, 11.0, 9.0, 10.5),
            ("000001.SZ", "20250502", "0", 0.0, 0.0, 0.0, 11.0),      # 无效行 → 翻转
            ("000001.SZ", "20250503", "0", 11.0, 12.0, 10.5, 11.5),   # 后续行也 fallback
            ("600001.SH", "20250501", "0", 20.0, 21.0, 19.0, 20.5),   # 其他股不受影响
        ])
        _, px = parse_intraday(df)
        assert px["2025-05-01"]["000001"]["low"] == 9.0     # 翻转前正常
        assert px["2025-05-02"]["000001"]["low"] == 11.0    # 无效行 fallback
        assert px["2025-05-03"]["000001"]["low"] == 11.5    # 翻转后永久 fallback (low=close)
        assert px["2025-05-01"]["600001"]["low"] == 19.0    # 其他股正常

    def test_signals_only_nonzero_dates(self):
        df = _df([
            ("000001.SZ", "20250501", "0", 10.0, 11.0, 9.0, 10.5),
            ("000001.SZ", "20250502", "1", 10.5, 11.5, 10.0, 11.0),
        ])
        sig, _ = parse_intraday(df)
        assert list(sig["000001"].keys()) == ["2025-05-02"]


class TestLegacyEquivalence:
    """与旧逐行解析逻辑(测试内复刻)在混合脏数据上逐字段一致"""

    def _legacy_daily(self, signals, prices):
        """复刻 _run_daily_backtest 的解析循环(7-16 后版本, 无 low parquet fallback
        — 旧 fallback 因日期格式不匹配恒失败, low=close)"""
        sig_by_code = {}
        for code, d in signals.items():
            code_num = code.split(".")[0]
            code_sigs = {}
            for dt, zp in zip(d["Date"], d["ZP"]):
                s = str(zp).strip()
                is_sig = s not in ("", "0", "0.0")
                if is_sig:
                    try:
                        is_sig = float(s) != 0.0
                    except (ValueError, TypeError):
                        is_sig = False
                # 修复口径: nan 非信号(旧实现 float("nan")!=0 为 True, 此处按新口径生成对照)
                if is_sig and s.lower() in ("nan", "-nan"):
                    is_sig = False
                if is_sig:
                    code_sigs[f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"] = zp
            if code_sigs:
                sig_by_code[code_num] = code_sigs
        prices_by_date = {}
        for code, d in prices.items():
            code_num = code.split(".")[0]
            for i, dt in enumerate(d["Date"]):
                close = d["Close"][i]
                if close is None or (isinstance(close, float) and np.isnan(close)):
                    continue
                h, l, o = d["High"][i], d["Low"][i], d["Open"][i]
                def _bad(v):
                    return v is None or (isinstance(v, float) and np.isnan(v)) or v <= 0
                if _bad(h) or _bad(l) or _bad(o):
                    h = l = o = close
                iso = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"
                prices_by_date.setdefault(iso, {})[code_num] = {
                    "close": close, "high": h, "low": l, "open": o}
        return sig_by_code, prices_by_date

    def test_daily_equivalence(self):
        rows = [
            ("000001.SZ", "20250501", "1", 10.0, 11.0, 9.0, 10.5),
            ("000001.SZ", "20250502", "0", np.nan, np.nan, np.nan, 11.0),
            ("000001.SZ", "20250503", "0.5", 11.0, 12.0, 10.5, 11.5),
            ("600001.SH", "20250501", "nan", 20.0, 21.0, 19.0, 20.5),
            ("600001.SH", "20250502", "0.00", 20.5, 21.5, 20.0, np.nan),
            ("600001.SH", "20250503", "abc", 21.0, 22.0, 20.5, 21.5),
        ]
        df = _df(rows)
        sig_new, px_new = parse_daily(df)
        signals, prices = {}, {}
        for r in rows:
            signals.setdefault(r[0], {"Date": [], "ZP": []})
            signals[r[0]]["Date"].append(r[1])
            signals[r[0]]["ZP"].append(r[2])
            prices.setdefault(r[0], {"Date": [], "Close": [], "High": [], "Low": [], "Open": []})
            prices[r[0]]["Date"].append(r[1])
            prices[r[0]]["Open"].append(r[3])
            prices[r[0]]["High"].append(r[4])
            prices[r[0]]["Low"].append(r[5])
            prices[r[0]]["Close"].append(r[6])
        sig_old, px_old = self._legacy_daily(signals, prices)
        assert sig_new == sig_old
        assert px_new == px_old
