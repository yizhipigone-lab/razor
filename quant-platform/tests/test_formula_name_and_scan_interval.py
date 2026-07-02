"""测试策略名动态化 + 离场扫描间隔可配置

覆盖：
- _get_formula_name() 三级优先级 (override > settings > default)
- set_scan_interval() 边界值
- get_scan_interval() 读取
"""
import sys
import os
import pytest

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ─── _get_formula_name 测试 ────────────────────────────────


class TestGetFormulaName:
    """测试 app/tqsdk/bridge._get_formula_name() 三级优先级"""

    def test_override_takes_priority(self):
        """override 参数优先级最高，忽略 settings"""
        from app.tqsdk.bridge import _get_formula_name
        result = _get_formula_name(override="MY_CUSTOM")
        assert result == "MY_CUSTOM"

    def test_override_stripped(self):
        """override 会 strip 空白"""
        from app.tqsdk.bridge import _get_formula_name
        result = _get_formula_name(override="  SPACED  ")
        assert result == "SPACED"

    def test_override_empty_falls_to_settings(self):
        """空字符串 override 不生效，走 settings"""
        from app.tqsdk.bridge import _get_formula_name
        # 空字符串 → override 被跳过，走 settings
        result = _get_formula_name(override="")
        # 无论 settings 里配什么，不应该返回空字符串
        assert result  # 非空
        assert isinstance(result, str)

    def test_override_whitespace_falls_to_settings(self):
        """纯空白 override 不生效，走 settings"""
        from app.tqsdk.bridge import _get_formula_name
        result = _get_formula_name(override="   ")
        assert result  # 非空

    def test_default_returns_string(self):
        """无 override 时返回非空字符串"""
        from app.tqsdk.bridge import _get_formula_name
        result = _get_formula_name()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_default_is_quantqq_when_no_settings(self):
        """settings 中无 formula_name 时默认返回 QUANTQQ"""
        from core.settings import settings
        # 临时清空 tqsdk.formula_name
        original = settings.get("tqsdk", "formula_name", default=None)
        try:
            if "tqsdk" in settings._data and "formula_name" in settings._data.get("tqsdk", {}):
                del settings._data["tqsdk"]["formula_name"]
            from app.tqsdk.bridge import _get_formula_name
            result = _get_formula_name()
            assert result == "QUANTQQ"
        finally:
            # 还原
            if original is not None:
                settings._data.setdefault("tqsdk", {})["formula_name"] = original

    def test_settings_value_overrides_default(self):
        """settings 中的 formula_name 优先于 QUANTQQ 默认值"""
        from core.settings import settings
        original = settings.get("tqsdk", "formula_name", default=None)
        try:
            settings._data.setdefault("tqsdk", {})["formula_name"] = "TEST_STRATEGY_123"
            from app.tqsdk.bridge import _get_formula_name
            result = _get_formula_name()
            assert result == "TEST_STRATEGY_123"
        finally:
            # 还原
            if original is not None:
                settings._data["tqsdk"]["formula_name"] = original
            else:
                settings._data.get("tqsdk", {}).pop("formula_name", None)

    def test_override_beats_settings(self):
        """override 优先级高于 settings"""
        from core.settings import settings
        original = settings.get("tqsdk", "formula_name", default=None)
        try:
            settings._data.setdefault("tqsdk", {})["formula_name"] = "SETTINGS_NAME"
            from app.tqsdk.bridge import _get_formula_name
            result = _get_formula_name(override="OVERRIDE_NAME")
            assert result == "OVERRIDE_NAME"
        finally:
            if original is not None:
                settings._data["tqsdk"]["formula_name"] = original
            else:
                settings._data.get("tqsdk", {}).pop("formula_name", None)


# ─── set_scan_interval / get_scan_interval 测试 ──────────


class TestScanInterval:
    """测试 LiveScheduler.set_scan_interval() / get_scan_interval()"""

    @pytest.fixture
    def scheduler(self):
        """创建一个最小化的 LiveScheduler 实例"""
        from app.live_trader.scheduler import LiveScheduler
        # LiveScheduler.__init__ 接受 config 和可选组件
        # 用一个简单对象模拟 config
        class MockConfig:
            exit_scan_interval_sec = 60.0

        s = LiveScheduler(config=MockConfig())
        return s

    def test_default_interval(self, scheduler):
        """默认间隔应为 60 秒"""
        assert scheduler.get_scan_interval() == 60.0

    def test_set_valid_interval(self, scheduler):
        """设置合法间隔值"""
        scheduler.set_scan_interval(30.0)
        assert scheduler.get_scan_interval() == 30.0

    def test_set_minimum_boundary(self, scheduler):
        """设置最小边界值 10"""
        scheduler.set_scan_interval(10.0)
        assert scheduler.get_scan_interval() == 10.0

    def test_set_maximum_boundary(self, scheduler):
        """设置最大边界值 300"""
        scheduler.set_scan_interval(300.0)
        assert scheduler.get_scan_interval() == 300.0

    def test_below_minimum_clamped(self, scheduler):
        """低于 10 的值被钳位到 10"""
        scheduler.set_scan_interval(5.0)
        assert scheduler.get_scan_interval() == 10.0

    def test_above_maximum_clamped(self, scheduler):
        """高于 300 的值被钳位到 300"""
        scheduler.set_scan_interval(500.0)
        assert scheduler.get_scan_interval() == 300.0

    def test_zero_clamped(self, scheduler):
        """0 被钳位到 10"""
        scheduler.set_scan_interval(0.0)
        assert scheduler.get_scan_interval() == 10.0

    def test_negative_clamped(self, scheduler):
        """负数被钳位到 10"""
        scheduler.set_scan_interval(-10.0)
        assert scheduler.get_scan_interval() == 10.0

    def test_string_converted(self, scheduler):
        """字符串数字被转换为 float"""
        scheduler.set_scan_interval("30")
        assert scheduler.get_scan_interval() == 30.0

    def test_very_small_above_minimum(self, scheduler):
        """刚好超过最小边界的值"""
        scheduler.set_scan_interval(10.1)
        assert scheduler.get_scan_interval() == 10.1
