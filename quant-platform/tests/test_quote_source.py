"""QuoteSource 深 module 的单元测试(候选 ①)。

驱动 app/data_manager/quote_source.py 的 orchestrator 契约:
- per-code 逐只降级(QMT→TDX→腾讯→Parquet)
- 每个请求 code 必有一行;缺价 NaN + source='missing'
- last_close = lastClose→preClose→NaN,严禁用现价伪造(守 CLAUDE.md:26)
- source 列标该行由哪个 adapter 解析
- 列契约固定

用 FakeAdapter(in-memory)注入,不碰网络/文件。真实 adapter 在各自的集成测试里覆盖。
"""
import math

import pandas as pd
import pytest

from app.data_manager.quote_source import (
    CONTRACT_COLUMNS,
    QuoteSource,
    QmtHttpAdapter,
    TdxAdapter,
    TencentAdapter,
    ParquetAdapter,
    get_realtime_quotes,
)


# ── 测试用 FakeAdapter ──────────────────────────────────────────
class FakeAdapter:
    """内存 adapter:按预设 mapping 返回原始行情 dict。

    raw dict 键: price(必填>0) / lastClose / preClose / open / high / low / volume / amount。
    缺键 = 该字段不可用。不包含的 code → 省略(表示该 adapter 解析不到)。
    """

    def __init__(self, name: str, mapping: dict):
        self.name = name
        self._mapping = mapping

    def fetch(self, codes):
        out = {}
        for c in codes:
            if c in self._mapping:
                raw = self._mapping[c]
                if float(raw.get("price", 0) or 0) > 0:
                    out[c] = dict(raw)
        return out


def _row(df: pd.DataFrame, code: str) -> pd.Series:
    """取指定 code 的那一行(按 code 列过滤)。"""
    hits = df[df["code"] == code]
    assert len(hits) == 1, f"期望 {code} 恰好一行,实际 {len(hits)} 行"
    return hits.iloc[0]


# ── 列契约 / 空输入 ─────────────────────────────────────────────
class TestContract:
    def test_empty_codes_returns_empty_df_with_contract_columns(self):
        src = QuoteSource(adapters=[])
        df = src.get_realtime_quotes([])
        assert df.empty
        assert list(df.columns) == CONTRACT_COLUMNS

    def test_contract_columns_exact(self):
        # 列必须固定:调用方依赖这套契约
        assert CONTRACT_COLUMNS == [
            "code", "open", "high", "low", "price",
            "volume", "amount", "last_close", "change_pct", "source",
        ]


# ── 降级 / 优先级 ───────────────────────────────────────────────
class TestFallback:
    def test_single_code_from_only_adapter(self):
        qmt = FakeAdapter("qmt", {"000001": {"price": 10.5, "lastClose": 10.0}})
        src = QuoteSource(adapters=[qmt])
        df = src.get_realtime_quotes(["000001"])
        assert len(df) == 1
        assert _row(df, "000001")["source"] == "qmt"
        assert _row(df, "000001")["price"] == pytest.approx(10.5)

    def test_priority_first_adapter_wins(self):
        # 两个 adapter 都有该 code → 高优先级(qmt)胜出
        qmt = FakeAdapter("qmt", {"000001": {"price": 10.5, "lastClose": 10.0}})
        tdx = FakeAdapter("tdx", {"000001": {"price": 99.0, "lastClose": 98.0}})
        src = QuoteSource(adapters=[qmt, tdx])
        df = src.get_realtime_quotes(["000001"])
        assert _row(df, "000001")["source"] == "qmt"
        assert _row(df, "000001")["price"] == pytest.approx(10.5)

    def test_per_code_fallback_mixed_sources(self):
        # A 在 qmt,B 不在 qmt 但在 parquet → 各自落到能拿到的最高源
        qmt = FakeAdapter("qmt", {"000001": {"price": 10.5, "lastClose": 10.0}})
        parquet = FakeAdapter(
            "parquet",
            {"000002": {"price": 20.0, "lastClose": 20.0}},  # close 冒充(将被规则否决)
        )
        src = QuoteSource(adapters=[qmt, parquet])
        df = src.get_realtime_quotes(["000001", "000002"])
        assert _row(df, "000001")["source"] == "qmt"
        assert _row(df, "000002")["source"] == "parquet"

    def test_fallback_skips_adapter_without_code(self):
        # 中间 adapter 没有该 code,跳过它落到后面的 adapter
        empty = FakeAdapter("tdx", {})
        parquet = FakeAdapter("parquet", {"000003": {"price": 5.0}})
        src = QuoteSource(adapters=[empty, parquet])
        df = src.get_realtime_quotes(["000003"])
        assert _row(df, "000003")["source"] == "parquet"


# ── 缺价 / missing ──────────────────────────────────────────────
class TestMissing:
    def test_no_adapter_has_code_gets_nan_row_source_missing(self):
        src = QuoteSource(adapters=[FakeAdapter("qmt", {})])
        df = src.get_realtime_quotes(["999999"])
        r = _row(df, "999999")
        assert r["source"] == "missing"
        assert pd.isna(r["price"])
        assert pd.isna(r["last_close"])

    def test_every_requested_code_has_a_row(self):
        # 请求 3 只,只有 1 只能定价 → 仍 3 行
        qmt = FakeAdapter("qmt", {"000001": {"price": 10.0, "lastClose": 9.5}})
        src = QuoteSource(adapters=[qmt])
        df = src.get_realtime_quotes(["000001", "000002", "000003"])
        assert len(df) == 3
        assert set(df["code"]) == {"000001", "000002", "000003"}

    def test_input_order_preserved(self):
        qmt = FakeAdapter("qmt", {
            "000001": {"price": 1.0, "lastClose": 1.0},
            "000002": {"price": 2.0, "lastClose": 2.0},
            "000003": {"price": 3.0, "lastClose": 3.0},
        })
        src = QuoteSource(adapters=[qmt])
        df = src.get_realtime_quotes(["000003", "000001", "000002"])
        assert list(df["code"]) == ["000003", "000001", "000002"]


# ── 昨收(last_close)——净值失真旧痛点 ──────────────────────────
class TestLastClose:
    def test_uses_lastclose_when_present(self):
        qmt = FakeAdapter("qmt", {"000001": {"price": 11.0, "lastClose": 10.0}})
        src = QuoteSource(adapters=[qmt])
        r = _row(src.get_realtime_quotes(["000001"]), "000001")
        assert r["last_close"] == pytest.approx(10.0)

    def test_falls_back_to_preclose_when_no_lastclose(self):
        qmt = FakeAdapter("qmt", {"000001": {"price": 11.0, "preClose": 10.2}})
        src = QuoteSource(adapters=[qmt])
        r = _row(src.get_realtime_quotes(["000001"]), "000001")
        assert r["last_close"] == pytest.approx(10.2)

    def test_lastclose_zero_falls_back_to_preclose(self):
        # 开盘初 lastClose 可能为 0,应退到 preClose
        qmt = FakeAdapter("qmt", {"000001": {"price": 11.0, "lastClose": 0, "preClose": 10.1}})
        src = QuoteSource(adapters=[qmt])
        r = _row(src.get_realtime_quotes(["000001"]), "000001")
        assert r["last_close"] == pytest.approx(10.1)

    def test_no_lastclose_no_preclose_yields_nan_not_price(self):
        # 核心:严禁用现价冒充昨收(CLAUDE.md:26)
        qmt = FakeAdapter("qmt", {"000001": {"price": 11.0}})  # 只有现价
        src = QuoteSource(adapters=[qmt])
        r = _row(src.get_realtime_quotes(["000001"]), "000001")
        assert pd.isna(r["last_close"]), "last_close 不得用现价伪造"
        assert r["price"] == pytest.approx(11.0)

    def test_parquet_close_does_not_count_as_lastclose(self):
        # Parquet adapter 可能传 lastClose=close(旧行为),orchestrator 不得信任:
        # 这里模拟 Parquet 只给 close 当 lastClose → 但没有真实昨收来源标识,
        # 契约上 adapter 应不传 lastClose。本测试断言:adapter 不传 lastClose 时 = NaN。
        parquet = FakeAdapter("parquet", {"000001": {"price": 10.0}})  # 不传 lastClose
        src = QuoteSource(adapters=[parquet])
        r = _row(src.get_realtime_quotes(["000001"]), "000001")
        assert pd.isna(r["last_close"])


# ── change_pct ──────────────────────────────────────────────────
class TestChangePct:
    def test_computed_when_last_close_present(self):
        qmt = FakeAdapter("qmt", {"000001": {"price": 11.0, "lastClose": 10.0}})
        src = QuoteSource(adapters=[qmt])
        r = _row(src.get_realtime_quotes(["000001"]), "000001")
        assert r["change_pct"] == pytest.approx(10.0)  # (11-10)/10*100

    def test_nan_when_no_last_close(self):
        # 没有真实昨收 → change_pct 也 NaN(诚实,不填 0)
        qmt = FakeAdapter("qmt", {"000001": {"price": 11.0}})
        src = QuoteSource(adapters=[qmt])
        r = _row(src.get_realtime_quotes(["000001"]), "000001")
        assert pd.isna(r["change_pct"])

    def test_nan_for_missing_row(self):
        src = QuoteSource(adapters=[FakeAdapter("qmt", {})])
        r = _row(src.get_realtime_quotes(["999999"]), "999999")
        assert pd.isna(r["change_pct"])


# ── OHLC 兜底 ───────────────────────────────────────────────────
class TestOhlc:
    def test_open_high_low_default_to_price_when_absent(self):
        # 腾讯式:只给现价,OHLC 退到现价(保留现行行为,不破坏调用方)
        tencent = FakeAdapter("tencent", {"000001": {"price": 10.0, "lastClose": 9.5}})
        src = QuoteSource(adapters=[tencent])
        r = _row(src.get_realtime_quotes(["000001"]), "000001")
        assert r["open"] == pytest.approx(10.0)
        assert r["high"] == pytest.approx(10.0)
        assert r["low"] == pytest.approx(10.0)

    def test_volume_amount_default_zero_when_absent(self):
        tencent = FakeAdapter("tencent", {"000001": {"price": 10.0}})
        src = QuoteSource(adapters=[tencent])
        r = _row(src.get_realtime_quotes(["000001"]), "000001")
        assert r["volume"] == 0
        assert r["amount"] == 0


# ── 模块级 port(单例,空输入不触网)──────────────────────────────
class TestModuleLevelPort:
    def test_module_level_get_returns_empty_df_for_empty_input(self):
        df = get_realtime_quotes([])
        assert df.empty
        assert list(df.columns) == CONTRACT_COLUMNS


# ── 真实 adapter 的翻译逻辑(纯函数,不触网)──────────────────────
# 每个 adapter 拆成 I/O(fetch)+ 纯翻译(translate/parse_*),后者单测。
# 翻译逻辑逐字复刻自现有 engine.py / qmt.py,保证 Phase 2 委托后行为不变。
class TestAdapterTranslation:
    def test_qmt_translates_gateway_dict(self):
        # qmt_gateway.get_realtime_quotes 返回 {code: {lastPrice,lastClose,open,high,low,volume}}
        gw = {"000001": {"lastPrice": 10.5, "lastClose": 10.0, "open": 10.1,
                         "high": 10.6, "low": 9.9, "volume": 1000}}
        raw = QmtHttpAdapter.translate(gw)
        assert raw["000001"]["price"] == pytest.approx(10.5)
        assert raw["000001"]["lastClose"] == pytest.approx(10.0)
        assert raw["000001"]["volume"] == pytest.approx(1000)

    def test_qmt_fetch_uses_live_trader_only(self, monkeypatch):
        # QmtHttpAdapter.fetch 调 get_live_trader_quotes(纯 QMT);老 get_realtime_quotes 已删(Phase 4)
        from app.trader.gateways import qmt as qmt_mod
        calls = {}

        def _fake_live(codes):
            calls["live"] = list(codes)
            return {"000001": {"lastPrice": 10.0, "lastClose": 9.5}}

        monkeypatch.setattr(qmt_mod.qmt_gateway, "get_live_trader_quotes", _fake_live)
        assert not hasattr(qmt_mod.qmt_gateway, "get_realtime_quotes"), "老带兜底方法应已删"
        raw = QmtHttpAdapter().fetch(["000001"])
        assert "000001" in raw
        assert calls.get("live") == ["000001"]

    def test_parquet_row_to_raw_has_no_lastclose(self):
        # Parquet 只有 close,无真实昨收 → 不传 lastClose(orchestrator 给 NaN,守 CLAUDE.md:26)
        row = {"close": 10.0, "open": 9.8, "high": 10.2, "low": 9.7, "volume": 100}
        raw = ParquetAdapter.row_to_raw(row)
        assert raw["price"] == pytest.approx(10.0)
        assert not raw.get("lastClose")  # None / 0 / 缺失都算

    def test_tencent_parse_s_response(self):
        # 复刻 engine.py:303-335 的 s_ 格式索引:
        # parts[2]=code,[3]=price,[4]=chg,[5]=chgpct,[6]=vol;last_close=price-chg
        text = 'v_s_sh600000="0~0~600000~10.50~0.30~2.94~1000~";'
        raw = TencentAdapter.parse_response(text, {"600000": "600000"})
        assert raw["600000"]["price"] == pytest.approx(10.50)
        assert raw["600000"]["lastClose"] == pytest.approx(10.20)  # 10.50 - 0.30
        assert raw["600000"]["volume"] == pytest.approx(1000)

    def test_tencent_ignores_malformed_lines(self):
        text = 'garbage;line with no equals;v_s_sh600000="0~0~600000~10.50~0.30~2.94~1000~";'
        raw = TencentAdapter.parse_response(text, {"600000": "600000"})
        assert "600000" in raw  # 只解析合法那行,其余跳过不炸

    def test_tencent_unknown_code_uses_raw_code(self):
        text = 'v_s_sh600000="0~0~600000~10.50~0.30~2.94~1000~";'
        raw = TencentAdapter.parse_response(text, {})  # lookup 空 → 用裸 code
        assert "600000" in raw

    def test_tdx_parse_quotes(self):
        # 复刻 engine.py:269-285 的 TdxHq.get_security_quotes 返回结构
        quotes = [{"code": "600000", "price": 10.5, "last_close": 10.2,
                   "open": 10.3, "high": 10.6, "low": 10.1, "vol": 1000, "amount": 10500}]
        raw = TdxAdapter.parse_quotes(quotes, {"600000": "600000"})
        assert raw["600000"]["price"] == pytest.approx(10.5)
        assert raw["600000"]["lastClose"] == pytest.approx(10.2)
        assert raw["600000"]["volume"] == pytest.approx(1000)

    def test_tdx_maps_to_orig_code(self):
        quotes = [{"code": "600000", "price": 10.5, "last_close": 10.2}]
        raw = TdxAdapter.parse_quotes(quotes, {"600000": "600000.SH"})
        assert "600000.SH" in raw  # 用 tdx_to_orig 映射回带后缀的 code


# ── 缓存 + 熔断(grilling Q8:orchestrator 级 3s 缓存 / 30s 熔断)────────
class _Counting:
    def __init__(self, name, mapping=None, fail=False):
        self.name = name
        self.calls = 0
        self._mapping = mapping or {}
        self._fail = fail

    def fetch(self, codes):
        self.calls += 1
        if self._fail:
            raise RuntimeError(f"{self.name} down")
        return {c: dict(self._mapping[c]) for c in codes if c in self._mapping and float(self._mapping[c].get("price", 0)) > 0}


class TestCacheAndBreaker:
    def test_second_call_within_ttl_hits_cache(self, monkeypatch):
        clock = [1000.0]
        ad = _Counting("qmt", {"000001": {"price": 10.0, "lastClose": 9.0}})
        src = QuoteSource(adapters=[ad], cache_ttl=3.0, _clock=lambda: clock[0])
        src.get_realtime_quotes(["000001"])
        src.get_realtime_quotes(["000001"])  # TTL 内
        assert ad.calls == 1, "TTL 内第二次应命中缓存,不重新 fetch"

    def test_call_after_ttl_refetches(self):
        clock = [1000.0]
        ad = _Counting("qmt", {"000001": {"price": 10.0, "lastClose": 9.0}})
        src = QuoteSource(adapters=[ad], cache_ttl=3.0, _clock=lambda: clock[0])
        src.get_realtime_quotes(["000001"])
        clock[0] += 4.0  # 超过 TTL
        src.get_realtime_quotes(["000001"])
        assert ad.calls == 2, "TTL 过期后应重新 fetch"

    def test_breaker_skips_failing_adapter(self):
        clock = [1000.0]
        flaky = _Counting("qmt", fail=True)
        ok = _Counting("parquet", {"000001": {"price": 10.0}, "000002": {"price": 20.0}})
        src = QuoteSource(adapters=[flaky, ok], breaker_cooldown=30.0, _clock=lambda: clock[0])
        r1 = _row(src.get_realtime_quotes(["000001"]), "000001")
        assert r1["source"] == "parquet"  # qmt 挂 → 落到 parquet
        assert flaky.calls == 1
        # 第二次(熔断窗内):qmt 应被跳过,不再 fetch
        r2 = _row(src.get_realtime_quotes(["000002"]), "000002")
        assert r2["source"] == "parquet"
        assert flaky.calls == 1, "熔断期内不应再调 failing adapter"

    def test_breaker_recovers_after_cooldown(self):
        clock = [1000.0]
        flaky = _Counting("qmt", fail=True)
        ok = _Counting("parquet", {"000001": {"price": 10.0}})
        src = QuoteSource(adapters=[flaky, ok], breaker_cooldown=30.0, _clock=lambda: clock[0])
        src.get_realtime_quotes(["000001"])
        assert flaky.calls == 1
        clock[0] += 31.0  # 超过熔断窗
        src.get_realtime_quotes(["000009"])  # 新 code,不走缓存
        assert flaky.calls == 2, "熔断窗过后应重新尝试"

    def test_empty_result_does_not_trip_breaker(self):
        # adapter 合法返回空(没数据)≠ 故障,不该熔断
        clock = [1000.0]
        empty_then_full = _EmptyThenFull("qmt")
        src = QuoteSource(adapters=[empty_then_full], _clock=lambda: clock[0])
        src.get_realtime_quotes(["000001"])  # 第一次空
        clock[0] += 4.0  # 超过 TTL,强制重新走 adapter
        src.get_realtime_quotes(["000001"])  # 没被熔断 → 应再调一次
        # 两次都调了(没被熔断)
        assert empty_then_full.calls == 2


class _EmptyThenFull:
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def fetch(self, codes):
        self.calls += 1
        return {}  # 始终合法空
