"""测试 app.live_trader.signal_picker — 实盘自给自足选股器。

覆盖关键路径:
- TDX 返回 status != "ok" → SignalScreenError
- matched 为空 → 空列表返回
- QMT 为 None / 取价失败 → 降级(信号标的为"无实时价"被剔除)
- lastPrice 为 0/None/非数字 → 正确过滤
- 正常选股+配价流程
"""
import pytest
from unittest import mock


class FakeTdxBridge:
    """可控的 TDX 选股结果"""
    def __init__(self, status="ok", matched=None, message=""):
        self._status = status
        self._matched = matched or []
        self._message = message

    def execute_screen(self, end_time, lookback_days, formula_name):
        return {
            "status": self._status,
            "matched": list(self._matched),
            "message": self._message,
        }


class FakeQmt:
    """可控的 QMT 行情"""
    def __init__(self, quotes=None):
        self._quotes = quotes or {}

    def get_realtime_quotes(self, codes):
        return {c: self._quotes.get(c, {}) for c in codes}


def _make_signal_picker(qmt=None, config=None):
    """构造 SignalPicker(不触发真实 TDX/QMT)"""
    from app.live_trader.signal_picker import SignalPicker
    return SignalPicker(qmt=qmt, config=config)


class TestSignalPickerErrors:
    """TDX 异常路径"""

    def test_tdx_not_ok_raises(self):
        """TDX 返回 status != ok → SignalScreenError"""
        picker = _make_signal_picker()
        with mock.patch(
            "app.tqsdk.bridge.TdxBridge",
            return_value=FakeTdxBridge(status="error", message="公式语法错误"),
        ):
            from app.live_trader.signal_picker import SignalScreenError
            with pytest.raises(SignalScreenError, match="非 ok"):
                picker.screen_and_price("20260715")

    def test_tdx_no_matched_returns_empty(self):
        """matched 为空 → 空列表,不报错"""
        picker = _make_signal_picker()
        with mock.patch(
            "app.tqsdk.bridge.TdxBridge",
            return_value=FakeTdxBridge(status="ok", matched=[]),
        ):
            signals, fname, meta = picker.screen_and_price("20260715")
        assert signals == []
        assert meta["matched_count"] == 0


class TestSignalPickerQmtDegradation:
    """QMT 降级路径"""

    def test_qmt_none_all_skipped(self):
        """QMT 为 None → 所有信号的 price 为 0 → 全部被跳过"""
        codes = ["600000.SH", "000001.SZ"]
        picker = _make_signal_picker(qmt=None)
        with mock.patch(
            "app.tqsdk.bridge.TdxBridge",
            return_value=FakeTdxBridge(status="ok", matched=codes),
        ):
            signals, fname, meta = picker.screen_and_price("20260715")
        assert signals == []
        assert meta["matched_count"] == 2
        assert meta["priced_count"] == 0
        assert len(meta["skipped"]) == 2

    def test_qmt_returns_zero_price_filtered(self):
        """lastPrice=0 被正确过滤"""
        codes = ["600000.SH"]
        qmt = FakeQmt({"600000.SH": {"lastPrice": 0}})
        picker = _make_signal_picker(qmt=qmt)
        with mock.patch(
            "app.tqsdk.bridge.TdxBridge",
            return_value=FakeTdxBridge(status="ok", matched=codes),
        ):
            signals, fname, meta = picker.screen_and_price("20260715")
        assert signals == []
        assert meta["priced_count"] == 0

    def test_qmt_returns_none_price_filtered(self):
        """lastPrice=None 被正确过滤"""
        codes = ["600000.SH"]
        qmt = FakeQmt({"600000.SH": {"lastPrice": None}})
        picker = _make_signal_picker(qmt=qmt)
        with mock.patch(
            "app.tqsdk.bridge.TdxBridge",
            return_value=FakeTdxBridge(status="ok", matched=codes),
        ):
            signals, fname, meta = picker.screen_and_price("20260715")
        assert signals == []
        assert meta["priced_count"] == 0

    def test_qmt_returns_non_numeric_price_filtered(self):
        """lastPrice="N/A" 被正确过滤"""
        codes = ["600000.SH"]
        qmt = FakeQmt({"600000.SH": {"lastPrice": "N/A"}})
        picker = _make_signal_picker(qmt=qmt)
        with mock.patch(
            "app.tqsdk.bridge.TdxBridge",
            return_value=FakeTdxBridge(status="ok", matched=codes),
        ):
            signals, fname, meta = picker.screen_and_price("20260715")
        assert signals == []
        assert meta["priced_count"] == 0

    def test_qmt_exception_graceful_degradation(self):
        """QMT 抛异常 → 不崩溃,所有信号标为无实时价"""
        codes = ["600000.SH"]
        qmt_bad = mock.MagicMock()
        qmt_bad.get_realtime_quotes.side_effect = RuntimeError("连接断开")
        picker = _make_signal_picker(qmt=qmt_bad)
        with mock.patch(
            "app.tqsdk.bridge.TdxBridge",
            return_value=FakeTdxBridge(status="ok", matched=codes),
        ):
            signals, fname, meta = picker.screen_and_price("20260715")
        assert signals == []
        assert meta["priced_count"] == 0


class TestSignalPickerNormal:
    """正常选股+配价流程"""

    def test_normal_flow_prices_signals(self):
        """正常:TDX 选到 2 只,QMT 都有价"""
        codes = ["600000.SH", "000001.SZ"]
        qmt = FakeQmt({
            "600000.SH": {"lastPrice": 12.34},
            "000001.SZ": {"lastPrice": 5.67},
        })
        picker = _make_signal_picker(qmt=qmt)
        with mock.patch(
            "app.tqsdk.bridge.TdxBridge",
            return_value=FakeTdxBridge(status="ok", matched=codes),
        ):
            signals, fname, meta = picker.screen_and_price("20260715")

        assert len(signals) == 2
        assert meta["matched_count"] == 2
        assert meta["priced_count"] == 2
        assert len(meta["skipped"]) == 0
        prices = {s.code: s.price for s in signals}
        assert prices["600000.SH"] == 12.34
        assert prices["000001.SZ"] == 5.67

    def test_partial_pricing(self):
        """部分有价:有价的入 signals,无价的入 skipped"""
        codes = ["600000.SH", "000001.SZ"]
        qmt = FakeQmt({"600000.SH": {"lastPrice": 12.34}})  # 000001 缺
        picker = _make_signal_picker(qmt=qmt)
        with mock.patch(
            "app.tqsdk.bridge.TdxBridge",
            return_value=FakeTdxBridge(status="ok", matched=codes),
        ):
            signals, fname, meta = picker.screen_and_price("20260715")

        assert len(signals) == 1
        assert signals[0].code == "600000.SH"
        assert meta["priced_count"] == 1
        assert len(meta["skipped"]) == 1

    def test_formula_name_passthrough(self):
        """formula_name 透传正确"""
        picker = _make_signal_picker()
        with mock.patch(
            "app.tqsdk.bridge.TdxBridge",
            return_value=FakeTdxBridge(status="ok", matched=["600000.SH"]),
        ):
            with mock.patch(
                "app.tqsdk.bridge._get_formula_name",
                return_value="MY_FORMULA",
            ):
                _, fname, _ = picker.screen_and_price("20260715")
        assert fname == "MY_FORMULA"
