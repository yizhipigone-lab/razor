"""worker 直写 parquet 改造(2026-07-18 P1-1/P1-2)的单元测试

stub tqcenter 后导入 worker 模块, 验证:
- _market_data_to_long: DataFrame → 长表向量化转换(替代逐格 df.loc)
- _write_range_parquet: 产物与旧 result_cache._signals_prices_to_rows 语义等价
"""
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# worker 顶层 `from tqcenter import tq`, 测试环境 stub 掉
_fake_tqcenter = types.ModuleType("tqcenter")
_fake_tqcenter.tq = types.SimpleNamespace(initialize=lambda *a, **k: None)
sys.modules.setdefault("tqcenter", _fake_tqcenter)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "tqsdk" / "worker"))
import tqsdk_bridge_worker as worker  # noqa: E402

from app.tqsdk import result_cache  # noqa: E402


def _mk():
    """合成 get_market_data 返回: index=date, columns=code(带后缀)"""
    idx = pd.to_datetime(["2025-05-01", "2025-05-02", "2025-05-03"])
    cols = ["000001.SZ", "600001.SH"]
    close = pd.DataFrame([[10.5, 20.5], [11.0, np.nan], [11.5, 21.5]], index=idx, columns=cols)
    high = pd.DataFrame([[11.0, 21.0], [11.5, 21.5], [12.0, 22.0]], index=idx, columns=cols)
    low = pd.DataFrame([[9.0, 19.0], [0.0, 19.5], [10.5, 20.5]], index=idx, columns=cols)  # 含 0
    open_ = pd.DataFrame([[10.0, 20.0], [10.5, 20.5], [11.0, 21.0]], index=idx, columns=cols)
    return {"Close": close, "High": high, "Low": low, "Open": open_}


class TestMarketDataToLong:
    def test_basic(self):
        long = worker._market_data_to_long(_mk())
        assert set(long.columns) == {"date", "code", "close", "high", "low", "open"}
        assert set(long["date"]) == {"20250501", "20250502", "20250503"}
        row = long[(long["code"] == "000001.SZ") & (long["date"] == "20250501")].iloc[0]
        assert row["close"] == 10.5 and row["low"] == 9.0

    def test_missing_field(self):
        mk = _mk()
        del mk["Open"]
        long = worker._market_data_to_long(mk)
        assert "open" not in long.columns
        assert "close" in long.columns

    def test_empty(self):
        assert worker._market_data_to_long({}) is None


class TestWriteRangeParquet:
    def _signals(self):
        return {
            "000001.SZ": {"Date": ["20250501", "20250502", "20250503"],
                           "ZP": ["1", "0", "0.5"]},
            "600001.SH": {"Date": ["20250501", "20250503"],
                           "ZP": ["0", "1"]},
            "000002.SZ": {"Date": ["20250501"], "ZP": ["0"]},  # 无价格数据
        }

    def test_schema_and_values(self, tmp_path):
        frame = worker._market_data_to_long(_mk())
        path, n = worker._write_range_parquet(self._signals(), [frame], "ZP")
        df = pd.read_parquet(path)
        assert list(df.columns) == ["code", "date", "signal_var", "signal_value",
                                     "open", "high", "low", "close"]
        assert n == 6  # 3 + 2 + 1
        # 值检查
        r = df[(df["code"] == "000001.SZ") & (df["date"] == "20250501")].iloc[0]
        assert r["signal_value"] == "1" and r["close"] == 10.5 and r["low"] == 9.0
        # low=0 → NaN (等价旧 "0" → _safe_float None)
        r2 = df[(df["code"] == "000001.SZ") & (df["date"] == "20250502")].iloc[0]
        assert np.isnan(r2["low"])
        # close NaN 保留 NaN
        r3 = df[(df["code"] == "600001.SH") & (df["date"] == "20250503")].iloc[0]
        assert r3["close"] == 21.5
        # 无价格数据的股 → OHLC 全 NaN
        r4 = df[df["code"] == "000002.SZ"].iloc[0]
        assert np.isnan(r4["close"]) and np.isnan(r4["low"])

    def test_equivalence_with_legacy_rows(self):
        """与旧 _signals_prices_to_rows(signals, prices_dict) 产物逐行等价"""
        signals = self._signals()
        # 旧 prices dict 格式 (worker 旧版字符串列表, NaN/0 → "0")
        prices = {
            "000001": {"Date": ["20250501", "20250502", "20250503"],
                        "Close": ["10.5", "11.0", "11.5"],
                        "High": ["11.0", "11.5", "12.0"],
                        "Low": ["9.0", "0", "10.5"],
                        "Open": ["10.0", "10.5", "11.0"]},
            "600001": {"Date": ["20250501", "20250503"],
                        "Close": ["20.5", "21.5"],
                        "High": ["21.0", "22.0"],
                        "Low": ["19.0", "20.5"],
                        "Open": ["20.0", "21.0"]},
        }
        legacy_rows = result_cache._signals_prices_to_rows(signals, prices)
        legacy_df = pd.DataFrame(legacy_rows)

        frame = worker._market_data_to_long(_mk())
        path, _ = worker._write_range_parquet(signals, [frame], "ZP")
        new_df = pd.read_parquet(path)

        key = ["code", "date"]
        old = legacy_df.set_index(key).sort_index()
        new = new_df.set_index(key).sort_index()
        assert set(old.index) == set(new.index)
        for idx in old.index:
            o, n = old.loc[idx], new.loc[idx]
            assert str(o["signal_value"]) == str(n["signal_value"]), idx
            assert str(o["signal_var"]) == str(n["signal_var"]), idx
            for col in ("open", "high", "low", "close"):
                ov, nv = o[col], n[col]
                o_nan = ov is None or (isinstance(ov, float) and np.isnan(ov))
                n_nan = isinstance(nv, float) and np.isnan(nv)
                if o_nan or n_nan:
                    assert o_nan and n_nan, f"{idx} {col}: {ov} vs {nv}"
                else:
                    assert abs(float(ov) - float(nv)) < 1e-9, f"{idx} {col}: {ov} vs {nv}"

    def test_no_price_frames(self):
        path, n = worker._write_range_parquet(self._signals(), [], "ZP")
        df = pd.read_parquet(path)
        assert n == 6
        assert df["close"].isna().all()

    def test_missing_ohlc_fields_no_crash(self):
        """老版 TDX 只返回 Close 时不得 KeyError(HIGH 回归修复)"""
        mk = _mk()
        for f in ("High", "Low", "Open"):
            del mk[f]
        frame = worker._market_data_to_long(mk)
        path, n = worker._write_range_parquet(self._signals(), [frame], "ZP")
        df = pd.read_parquet(path)
        assert n == 6
        assert list(df.columns) == ["code", "date", "signal_var", "signal_value",
                                     "open", "high", "low", "close"]
        # close 有值, high/low/open 全 NaN
        r = df[(df["code"] == "000001.SZ") & (df["date"] == "20250501")].iloc[0]
        assert r["close"] == 10.5
        assert np.isnan(r["high"]) and np.isnan(r["low"]) and np.isnan(r["open"])
