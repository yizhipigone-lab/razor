"""Characterization tests for app.data_manager.engine.get_realtime_quote。

目的:Phase 2 委托到 quote_source 之前,锁定 engine.get_realtime_quote 的【现有】行为。
全程 mock 网络(qmt_gateway / TDX / 腾讯),不依赖运行中的应用。

委托后,以下几条会【故意】失败,对应设计 Q5/Q6 的契约变更:
  - + 'source' 列(9 列 → 10 列)
  - last_close 缺失时:price 冒充 → NaN(Q6,守 CLAUDE.md:26)
  - 全失败时:空 DF → 每只 code 一行 missing(Q5)
这些故意失败会在委托后逐条复核,确认是预期变更后把测试改到新契约。
"""
import pytest
import pandas as pd

from app.data_manager import engine
from app.trader.gateways import qmt as qmt_mod
import app.data_manager.quote_source as _qs


@pytest.fixture(autouse=True)
def _reset_default_quote_source():
    """每个测试重置默认 QuoteSource 单例,避免 3s 缓存跨测试污染。"""
    _qs._default_source = None
    yield
    _qs._default_source = None


def _patch_qmt(monkeypatch, mapping):
    """让 engine 的 QMT 分支返回 mapping(dict);mapping={} 表示 QMT 无数据。
    注:QmtHttpAdapter 走 qmt_gateway.get_live_trader_quotes(纯 QMT),故 patch 它。"""
    monkeypatch.setattr(qmt_mod.qmt_gateway, "get_live_trader_quotes", lambda codes: dict(mapping))


def _block_tdx_and_tencent(monkeypatch):
    """让 TDX 连不上、腾讯 HTTP 抛错,迫使 engine 走到"全失败"分支。"""
    class _FakeTdx:
        def __init__(self, *a, **k): pass
        def connect(self, *a, **k): return False
        def disconnect(self): pass
    monkeypatch.setattr("pytdx2.hq.TdxHq_API", _FakeTdx)

    def _boom(*a, **k):
        raise Exception("blocked by test")
    monkeypatch.setattr("requests.get", _boom)


class TestEngineQuoteCharacterization:
    def test_qmt_path_now_has_source_column(self, monkeypatch):
        # Q5:委托后新增 source 列(9 → 10 列)
        _patch_qmt(monkeypatch, {"000001": {"lastPrice": 10.5, "lastClose": 10.0}})
        df = engine.get_realtime_quote(["000001"])
        assert "price" in df.columns
        assert "last_close" in df.columns
        assert "source" in df.columns  # Q5 新增

    def test_qmt_price_from_lastprice(self, monkeypatch):
        _patch_qmt(monkeypatch, {"000001": {"lastPrice": 10.5, "lastClose": 10.0}})
        df = engine.get_realtime_quote(["000001"])
        assert df.iloc[0]["price"] == pytest.approx(10.5)

    def test_qmt_lastclose_uses_lastclose(self, monkeypatch):
        _patch_qmt(monkeypatch, {"000001": {"lastPrice": 11.0, "lastClose": 10.0}})
        df = engine.get_realtime_quote(["000001"])
        assert df.iloc[0]["last_close"] == pytest.approx(10.0)

    def test_qmt_lastclose_fallback_preclose(self, monkeypatch):
        # lastClose=0 → 退 preClose(这条契约委托后仍成立)
        _patch_qmt(monkeypatch, {"000001": {"lastPrice": 11.0, "lastClose": 0, "preClose": 10.2}})
        df = engine.get_realtime_quote(["000001"])
        assert df.iloc[0]["last_close"] == pytest.approx(10.2)

    def test_qmt_lastclose_nan_when_missing(self, monkeypatch):
        # Q6:缺昨收 → NaN(不再用现价冒充,守 CLAUDE.md:26)
        _patch_qmt(monkeypatch, {"000001": {"lastPrice": 11.0}})  # 无 lastClose/preClose
        df = engine.get_realtime_quote(["000001"])
        assert pd.isna(df.iloc[0]["last_close"])
        assert df.iloc[0]["price"] == pytest.approx(11.0)

    def test_all_fail_returns_missing_row(self, monkeypatch):
        # Q5:全失败 → 每只 code 一行 source='missing'(不再空 DF)
        _patch_qmt(monkeypatch, {})
        _block_tdx_and_tencent(monkeypatch)
        df = engine.get_realtime_quote(["999999"])
        assert len(df) == 1
        assert df.iloc[0]["code"] == "999999"
        assert df.iloc[0]["source"] == "missing"
        assert pd.isna(df.iloc[0]["price"])
