"""live_bar_stitcher.LiveBarStitcher 单元测试(候选⑥)

锁定 LiveBarStitcher 深 module 契约:两类调用点(record 单 code + bars 多 code),
共享核心 _build_live_bar / _merge_into_existing。
"""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.data_manager.live_bar_stitcher import LiveBarStitcher


# ===== Fixtures =====

@pytest.fixture
def stitcher():
    return LiveBarStitcher()


@pytest.fixture
def quote_dict():
    """模拟 quote_source.DataFrame 行转 dict"""
    return {
        "price": 11.5,
        "open": 11.0,
        "high": 11.8,
        "low": 10.9,
        "volume": 50000.0,
        "amount": 575000.0,
        "last_close": 10.5,
    }


# ===== 1. 单 code records 缝合(API 端点) =====

class TestStitchRecordsSingleCode:
    def test_no_quote_returns_false(self, stitcher):
        records = [{"date": "2026-07-10", "open": 10, "high": 11,
                    "low": 10, "close": 10.5, "volume": 1000}]
        with patch.object(stitcher, "fetch_quotes", return_value={}):
            result = stitcher.stitch_record(records, "600000.SH",
                                             today_str="2026-07-13")
        assert result is False
        assert len(records) == 1  # 未追加

    def test_append_new_today_bar(self, stitcher, quote_dict):
        """records 最后一天不是 today 时,追加新 today bar"""
        records = [
            {"date": "2026-07-10", "open": 10, "high": 11,
             "low": 10, "close": 10.5, "volume": 1000},
        ]
        with patch.object(stitcher, "fetch_quotes",
                          return_value={"600000.SH": quote_dict}):
            result = stitcher.stitch_record(records, "600000.SH",
                                             today_str="2026-07-13")
        assert result is True
        assert len(records) == 2
        today_bar = records[-1]
        assert today_bar["date"] == "2026-07-13"
        assert today_bar["close"] == 11.5
        assert today_bar["pre_close"] == 10.5
        # pct_chg = (11.5 - 10.5) / 10.5 * 100 = 9.52
        assert today_bar["pct_chg"] == pytest.approx(9.52, rel=0.01)
        # vol_active = today_vol(50k) > yest_vol(1k) → True
        assert today_bar["vol_active"] is True

    def test_merge_into_existing_today_bar(self, stitcher, quote_dict):
        """records 最后一天已经是 today 时,更新最后一条(high 取 max / low 取 min)"""
        records = [
            {"date": "2026-07-10", "open": 10, "high": 11,
             "low": 10, "close": 10.5, "volume": 1000},
            {"date": "2026-07-13", "open": 11.0, "high": 11.3,
             "low": 10.6, "close": 11.2, "volume": 30000},
        ]
        # 高取更高(low max),低取更低(low min)
        # high 11.8 > 11.3 → bar.high = 11.8
        # low 10.9 > 10.6 → bar.low 仍是 10.6(更深的低在 bar)
        with patch.object(stitcher, "fetch_quotes",
                          return_value={"600000.SH": quote_dict}):
            result = stitcher.stitch_record(records, "600000.SH",
                                             today_str="2026-07-13")
        assert result is True
        assert len(records) == 2
        today_bar = records[-1]
        assert today_bar["high"] == 11.8  # max(11.3, 11.8)
        assert today_bar["low"] == 10.6  # min(10.6, 10.9) = 10.6
        assert today_bar["close"] == 11.5
        assert today_bar["volume"] == 50000

    def test_vol_active_when_yest_zero(self, stitcher, quote_dict):
        """yest_vol == 0 时,vol_active 必须为 False(避免虚假信号)"""
        records = [{"date": "2026-07-10", "open": 10, "high": 11,
                    "low": 10, "close": 10.5, "volume": 0}]
        with patch.object(stitcher, "fetch_quotes",
                          return_value={"600000.SH": quote_dict}):
            stitcher.stitch_record(records, "600000.SH",
                                   today_str="2026-07-13")
        assert records[-1]["vol_active"] is False

    def test_q6_pct_chg_zero_when_no_pre_close(self, stitcher):
        """last_close 缺失/0 时,pct_chg=0 (Q6 守约:严禁用现价伪造 last_close)"""
        quote_no_pre = {
            "price": 11.5, "open": 11.0, "high": 11.8, "low": 10.9,
            "volume": 50000.0, "amount": 0, "last_close": 0,
        }
        records = []
        with patch.object(stitcher, "fetch_quotes",
                          return_value={"600000.SH": quote_no_pre}):
            stitcher.stitch_record(records, "600000.SH",
                                   today_str="2026-07-13")
        assert records[-1]["pct_chg"] == 0
        assert records[-1]["pre_close"] == 0


# ===== 2. 多 code DataFrame 缝合(sim_trader) =====

class TestStitchBarsMultiCode:
    def _make_bars(self, today):
        """today 是要查询的目标日期;history 用前一日避免与 today 重名冲突"""
        history = date(2026, 7, 9) if today == date(2026, 7, 13) else today - \
            pd.Timedelta(days=1).to_pytimedelta() if hasattr(pd.Timedelta, "to_pytimedelta") else None
        # 简化:history 固定 07-09(避免 today == history 撞行覆盖)
        history = date(2026, 7, 9)
        return pd.DataFrame([
            {"code": "600000.SH", "date": today, "open": 10, "high": 11,
             "low": 10, "close": 10.5, "volume": 1000},
            {"code": "000001.SZ", "date": today, "open": 20, "high": 21,
             "low": 20, "close": 20.5, "volume": 2000},
            {"code": "600000.SH", "date": history, "open": 9,
             "high": 10, "low": 9, "close": 9.5, "volume": 500},
        ])

    def test_stitch_bars_returns_bars_and_snapshot(self, stitcher):
        bars = self._make_bars(date(2026, 7, 13))
        # 提供两只 code 的 quotes(无 quote 不替换,只在 snapshot 中填有效的)
        quotes = {
            "600000.SH": {
                "price": 11.5, "open": 11.0, "high": 11.8, "low": 10.9,
                "volume": 50000.0, "amount": 0, "last_close": 10.5,
            },
            "000001.SZ": {
                "price": 21.0, "open": 20.5, "high": 21.3, "low": 20.4,
                "volume": 30000.0, "amount": 0, "last_close": 20.5,
            },
        }
        with patch.object(stitcher, "fetch_quotes", return_value=quotes):
            with patch("app.data_manager.live_bar_stitcher.date") as mock_date:
                mock_date.today.return_value = date(2026, 7, 13)
                new_bars, snapshot = stitcher.stitch_bars(bars, date(2026, 7, 13))
        # snapshot 包含两 code
        assert "600000.SH" in snapshot
        assert "000001.SZ" in snapshot
        assert snapshot["600000.SH"]["close"] == 11.5
        # bars 应当替换今日两行
        today_rows = new_bars[new_bars["date"] == date(2026, 7, 13)]
        assert len(today_rows) == 2
        today_600 = today_rows[today_rows["code"] == "600000.SH"].iloc[0]
        assert today_600["close"] == 11.5

    def test_non_today_returns_history_snapshot(self, stitcher):
        """非今日 → 直接从 history 取 today snapshot,不调 quote"""
        bars = self._make_bars(date(2026, 7, 10))
        with patch("app.data_manager.live_bar_stitcher.date") as mock_date:
            mock_date.today.return_value = date(2026, 7, 13)  # 现实 13 日
            new_bars, snapshot = stitcher.stitch_bars(bars, date(2026, 7, 10))
        # 10 日的 snapshot
        assert "600000.SH" in snapshot
        assert snapshot["600000.SH"]["close"] == 10.5

    def test_empty_quotes_returns_history_snapshot(self, stitcher):
        bars = self._make_bars(date(2026, 7, 13))
        with patch.object(stitcher, "fetch_quotes", return_value={}):
            with patch("app.data_manager.live_bar_stitcher.date") as mock_date:
                mock_date.today.return_value = date(2026, 7, 13)
                new_bars, snapshot = stitcher.stitch_bars(bars, date(2026, 7, 13))
        assert "600000.SH" in snapshot
        assert snapshot["600000.SH"]["close"] == 10.5  # history 取


# ===== 3. fetch_quotes 共享 fetch =====

class TestFetchQuotes:
    def test_fetch_quotes_delegates_to_quote_source(self, stitcher):
        # patch source module due to lazy import inside fetch_quotes
        with patch("app.data_manager.quote_source.get_realtime_quotes") as mock_grt:
            mock_grt.return_value = pd.DataFrame([
                {"code": "600000.SH", "price": 11.5, "open": 11.0,
                 "high": 11.8, "low": 10.9, "volume": 50000.0,
                 "amount": 0, "last_close": 10.5},
            ])
            result = stitcher.fetch_quotes(["600000.SH"])
        assert "600000.SH" in result
        assert result["600000.SH"]["price"] == 11.5

    def test_fetch_quotes_filters_no_price(self, stitcher):
        with patch("app.data_manager.quote_source.get_realtime_quotes") as mock_grt:
            mock_grt.return_value = pd.DataFrame([
                {"code": "600000.SH", "price": 11.5, "open": 11.0,
                 "high": 11.8, "low": 10.9, "volume": 50000.0,
                 "amount": 0, "last_close": 10.5},
                {"code": "000001.SZ", "price": 0, "open": 0,
                 "high": 0, "low": 0, "volume": 0, "amount": 0,
                 "last_close": 0},
            ])
            result = stitcher.fetch_quotes(["600000.SH", "000001.SZ"])
        # 缺价的 code 被过滤
        assert "600000.SH" in result
        assert "000001.SZ" not in result


# ===== 4. 行为等价的回归 =====
# (system.py:160-212 的 inline 实现 vs LiveBarStitcher 应该有相同语义)

class TestRegression:
    def test_system_py_pct_chg_formula_preserved(self, stitcher, quote_dict):
        """(last_price - pre_close) / pre_close * 100  四舍五入 2 位"""
        quote_dict["price"] = 11.55
        quote_dict["last_close"] = 10.0
        records = []
        with patch.object(stitcher, "fetch_quotes",
                          return_value={"600000.SH": quote_dict}):
            stitcher.stitch_record(records, "600000.SH",
                                   today_str="2026-07-13")
        # (11.55 - 10.0) / 10.0 * 100 = 15.5 → round 2 = 15.5
        assert records[-1]["pct_chg"] == 15.5

    def test_system_py_open_high_low_fallback(self, stitcher):
        """open/high/low 缺失时回退用 last_price"""
        quote = {
            "price": 11.5,
            "open": 0, "high": 0, "low": 0,
            "volume": 50000.0, "amount": 0, "last_close": 10.5,
        }
        records = []
        with patch.object(stitcher, "fetch_quotes",
                          return_value={"600000.SH": quote}):
            stitcher.stitch_record(records, "600000.SH",
                                   today_str="2026-07-13")
        assert records[-1]["open"] == 11.5  # fallback to price
        assert records[-1]["high"] == 11.5
        assert records[-1]["low"] == 11.5
