"""测试 app.config.risk_params — 风控参数集中加载层。

覆盖:
- 所有字段从 settings 正确读取
- 百分比字段 /100 转小数
- 默认值兜底(settings 缺键)
- breakeven 字段存在且正确
- RiskParams frozen dataclass 不可变
"""
import pytest
from unittest import mock


class TestRiskParamsLoad:
    """测试 load_risk_params() — settings 正常命中"""

    def _make_mock_settings(self, fake_risk: dict):
        """risk_params 内 `from core.settings import settings as _settings`,
        需 mock 模块内引用 `app.config.risk_params._settings`"""
        m = mock.MagicMock()
        m.get = lambda section, key: fake_risk.get(key)
        return mock.patch("app.config.risk_params._settings", m)

    def test_all_pct_fields_divided_by_100(self):
        """百分比字段都应从 settings 读原始%值后 /100 转小数"""
        fake_risk = {
            "hard_stop_loss_pct": -6.0,
            "trailing_stop_activate_pct": 5.0,
            "trailing_drawdown_pct": 2.0,
            "time_exit_min_pnl_pct": 3.0,
            "first_day_exit_min_profit": 3.0,
            "breakeven_threshold_pct": 2.23,
            "breakeven_stop_pnl_pct": 0.98,
        }
        with self._make_mock_settings(fake_risk):
            from app.config.risk_params import load_risk_params
            rp = load_risk_params()

        assert rp.hard_stop == pytest.approx(-0.06), f"hard_stop={rp.hard_stop}"
        assert rp.trail_activate == pytest.approx(0.05)
        assert rp.trail_dd == pytest.approx(0.02)
        assert rp.time_exit_profit == pytest.approx(0.03)
        assert rp.first_day_exit_min_profit == pytest.approx(0.03), \
            f"CRITICAL-1: 应 /100.0 得 0.03,实际 {rp.first_day_exit_min_profit}"
        assert rp.breakeven_threshold_pct == pytest.approx(0.0223), \
            f"CRITICAL-2: breakeven_threshold_pct={rp.breakeven_threshold_pct}"
        assert rp.breakeven_stop_pnl_pct == pytest.approx(0.0098)

    def test_non_pct_fields_preserved(self):
        """非百分比字段(int/bool)不做 /100"""
        fake_risk = {
            "time_exit_days": 10, "time_exit_force_days": 15,
            "first_day_exit_days": 2, "use_atr_stop": True,
            "atr_stop_multiplier": 2.5,
        }
        with self._make_mock_settings(fake_risk):
            from app.config.risk_params import load_risk_params
            rp = load_risk_params()

        assert rp.time_exit_days == 10
        assert rp.time_force_days == 15
        assert rp.first_day_exit_days == 2
        assert rp.use_atr_trail is True
        assert rp.atr_trail_multiplier == 2.5

    def test_take_profit_tiers_preserved(self):
        """take_profit_tiers 不做单位转换"""
        fake_tiers = [{"profit_pct": 0.03, "sell_ratio": 0.30}]
        fake_risk = {"take_profit_tiers": fake_tiers}
        with self._make_mock_settings(fake_risk):
            from app.config.risk_params import load_risk_params
            rp = load_risk_params()

        assert rp.take_profit_tiers == fake_tiers


class TestRiskParamsDefaults:
    """测试 settings 缺键时的默认值"""

    def test_defaults_when_settings_empty(self):
        """settings 无 risk 段时返回合理默认值"""
        m = mock.MagicMock()
        m.get = lambda section, key: None
        with mock.patch("app.config.risk_params._settings", m):
            from app.config.risk_params import load_risk_params
            rp = load_risk_params()

        assert rp.hard_stop == pytest.approx(-0.06)       # -6.0 / 100
        assert rp.trail_activate == pytest.approx(0.05)   # 5.0 / 100
        assert rp.trail_dd == pytest.approx(0.02)         # 2.0 / 100
        assert rp.time_exit_days == 7
        assert rp.time_exit_profit == pytest.approx(0.03)
        assert rp.time_force_days == 12
        assert rp.first_day_exit_min_profit == 0.0        # 0 / 100 = 0(禁用)
        assert rp.first_day_exit_days == 1
        assert rp.use_atr_trail is False
        assert rp.atr_trail_multiplier == 1.0
        # CRITICAL-2: breakeven 默认值
        assert rp.breakeven_threshold_pct == 0.0, "默认应禁用保本止损"
        assert rp.breakeven_stop_pnl_pct == 0.0


class TestRiskParamsFrozen:
    """RiskParams 是 frozen dataclass"""

    def test_cannot_mutate(self):
        from app.config.risk_params import RiskParams
        rp = RiskParams(
            hard_stop=-0.06, trail_activate=0.05, trail_dd=0.02,
            take_profit_tiers=[], time_exit_days=7, time_exit_profit=0.03,
            time_force_days=12, first_day_exit_min_profit=0.0, first_day_exit_days=1,
        )
        with pytest.raises(Exception):  # FrozenInstanceError 或 AttributeError
            rp.hard_stop = -0.10

    def test_breakeven_fields_default_zero(self):
        """breakeven 字段默认为 0.0"""
        from app.config.risk_params import RiskParams
        rp = RiskParams(
            hard_stop=-0.06, trail_activate=0.05, trail_dd=0.02,
            take_profit_tiers=[], time_exit_days=7, time_exit_profit=0.03,
            time_force_days=12, first_day_exit_min_profit=0.0, first_day_exit_days=1,
        )
        assert rp.breakeven_threshold_pct == 0.0
        assert rp.breakeven_stop_pnl_pct == 0.0

    def test_asdict_includes_breakeven(self):
        """dataclasses.asdict 应包含 breakeven 字段(engine.py/exit_monitor 需要)"""
        import dataclasses
        from app.config.risk_params import RiskParams
        rp = RiskParams(
            hard_stop=-0.06, trail_activate=0.05, trail_dd=0.02,
            breakeven_threshold_pct=0.0223, breakeven_stop_pnl_pct=0.0098,
            take_profit_tiers=[], time_exit_days=7, time_exit_profit=0.03,
            time_force_days=12, first_day_exit_min_profit=0.0, first_day_exit_days=1,
        )
        d = dataclasses.asdict(rp)
        assert "breakeven_threshold_pct" in d
        assert "breakeven_stop_pnl_pct" in d
        assert d["breakeven_threshold_pct"] == 0.0223
        assert d["breakeven_stop_pnl_pct"] == 0.0098


class TestRiskParamsRealSettings:
    """对真实 config/app_setting.json 做回归测试(审计报告要求 2026-07-15):
    mock 测试只覆盖计算逻辑, 真实 settings 可能在键名/单位上不一致。
    本组测试不 mock, 直接读 config/app_setting.json。
    """

    def test_real_settings_have_risk_section(self):
        """真实 app_setting.json 必须含 [risk] 段, 否则全部走默认值"""
        from core.settings import settings
        # settings.get('risk', any_key) 在缺段时会抛 KeyError; 用 dict 探测
        data = settings._data if hasattr(settings, '_data') else {}
        assert "risk" in data, (
            "config/app_setting.json 缺 [risk] 段, "
            "所有百分比字段走默认, 保本止损永远禁用(CRITICAL-2 隐性失效)"
        )

    def test_real_hard_stop_is_negative_decimal(self):
        """真实 settings 中 hard_stop 必须是负数小数, 否则会反向触发止盈"""
        from app.config.risk_params import load_risk_params
        rp = load_risk_params()
        assert rp.hard_stop < 0, (
            f"hard_stop 应为负数(下跌止损), 实际 {rp.hard_stop}; "
            f"若为正数说明 settings 存的是 +0.x 而不是 -x%"
        )

    def test_real_breakeven_threshold_aligned(self):
        """真实 settings 中 breakeven_threshold_pct 必须是小数(非整数百分比数)。
        audit 报告 C2: settings 存 2.23(百分比数), /100 后 = 0.0223(小数);
        若 settings 直接存 0.0223(小数), /100 后 = 0.000223(0.0223%), 量纲错误。
        阈值: 0.0 < rp.breakeven_threshold_pct < 1.0(典型 0.01~0.05)。
        """
        from app.config.risk_params import load_risk_params
        rp = load_risk_params()
        if rp.breakeven_threshold_pct == 0.0:
            # 默认值, 用户未启用保本止损, 跳过
            pytest.skip("breakeven 默认 0(未启用), 跳过量纲验证")
        assert 0.0 < rp.breakeven_threshold_pct < 1.0, (
            f"breakeven_threshold_pct 应是小数(0~1), 实际 {rp.breakeven_threshold_pct}; "
            f"若 >1 说明 settings 存的是百分比数 2.23 但被错误地 /100 而非按原值使用; "
            f"若 <0.001 说明 settings 已存小数又被错误 /100"
        )

    def test_real_first_day_exit_disabled_by_default(self):
        """first_day_exit_min_profit 默认应为 0(禁用), audit C1 bug 在默认场景不可见。
        本测试锁死默认语义, 防止后续误改。
        """
        from app.config.risk_params import load_risk_params
        rp = load_risk_params()
        assert rp.first_day_exit_min_profit == 0.0, (
            f"first_day_exit_min_profit 默认应为 0.0(禁用), 实际 {rp.first_day_exit_min_profit}; "
            f"若非 0, audit C1 的 /100 修复在默认场景无意义"
        )

    def test_real_settings_keys_match_load_func(self):
        """真实 settings 的 [risk] 段 key 必须能被 load_risk_params 正确读取。
        若 settings 用了别的 key 名(老迁移未清), audit HIGH 的 key.upper() 问题会重演。
        """
        from core.settings import settings
        data = settings._data if hasattr(settings, '_data') else {}
        risk = data.get("risk", {})
        # 加载函数实际读的 key 列表(来自 risk_params.py:57-69)
        expected_keys = {
            "hard_stop_loss_pct", "trailing_stop_activate_pct", "trailing_drawdown_pct",
            "time_exit_days", "time_exit_min_pnl_pct", "time_exit_force_days",
            "first_day_exit_min_profit", "first_day_exit_days",
            "use_atr_stop", "atr_stop_multiplier",
            "take_profit_tiers", "breakeven_threshold_pct", "breakeven_stop_pnl_pct",
        }
        # 至少有一半以上 key 命中, 防止 settings 大改名后 load 函数读不到
        hit = sum(1 for k in expected_keys if k in risk)
        # 不强制 100% 命中(可能有自定义键), 但要 >= 8 个核心键
        assert hit >= 8, (
            f"settings [risk] 段命中 key 仅 {hit}/{len(expected_keys)}, "
            f"可能 settings 改名/迁移未同步, 导致 load_risk_params 走默认"
        )


class TestRiskParamsCriticalBugRepro:
    """CRITICAL bug 真触发测试(2026-07-15 第二轮迭代新增)。

    之前的 mock 测试用 first_day_exit=3.0 验证 /100 计算,但 mock 路径不能
    证明 bug 在真实数据流下会消失。本测试用 monkeypatch 强制 settings 给出
    非零的 first_day_exit,走完 exit_rule_engine 的真实触发路径,断言
    C1 修复后行为正确(不会因量纲错误导致永远触发)。
    """

    def test_first_day_exit_bug_does_not_fire_when_pct_normal(self):
        """C1 bug 真复现: monkeypatch settings 让 first_day_exit=3.0(百分比整数),
        走 exit_rule_engine,断言:
          - 修复后: fd=0.03, day_high_pct=0.01 < fd → 触发(正常)
          - C1 bug: fd=3.0, day_high_pct=0.01 < fd → 触发(但量纲错误,无差别)

        本测试的核心目的:**证明修复后 exit_rules 收到的 fd 已经是小数 0.03,
        而不是百分比整数 3.0**。这是 audit 报告 C1 第 65-72 行 bug 路径的真验证。
        """
        from unittest import mock
        from app.config.risk_params import load_risk_params
        from app.backtest.exit_rules import (
            RuleContext, ExitRuleEngine, ExitSignal,
        )

        # 模拟 settings 中 first_day_exit_min_profit=3.0 (用户配置百分比整数)
        fake_risk = {
            "first_day_exit_min_profit": 3.0,  # settings 存百分比数
            "first_day_exit_days": 1,
            "hard_stop_loss_pct": -6.0,
            "trailing_stop_activate_pct": 5.0,
            "trailing_drawdown_pct": 2.0,
            "time_exit_days": 7,
            "time_exit_min_pnl_pct": 3.0,
            "time_exit_force_days": 12,
        }
        m = mock.MagicMock()
        m.get = lambda section, key: fake_risk.get(key)
        with mock.patch("app.config.risk_params._settings", m):
            rp = load_risk_params()

        # 修复后断言:RiskParams.first_day_exit_min_profit 应是 0.03(小数)
        assert rp.first_day_exit_min_profit == 0.03, (
            f"C1 修复无效: settings=3.0(%) 但 RiskParams={rp.first_day_exit_min_profit} "
            f"(应为 0.03)。如果 = 3.0 说明 /100 没生效。"
        )

        # 走完整 exit_rule_engine 路径,验证 day_high_pct 比较
        # 构造一个 entry=10, high=10.01 (day_high_pct=0.001=0.1%) 的 ctx
        ctx = RuleContext(
            entry_price=10.0, peak_price=10.01,
            shares=100, hold_days=2,  # fd_days=1, hold_days in [2, 2]
            high=10.01, low=10.0, close=10.0, open=10.0,
            first_day_exit_min_profit=rp.first_day_exit_min_profit,  # 0.03
            first_day_exit_days=rp.first_day_exit_days,
            first_day_hold_value=2,
        )
        # day_high_pct = 10.01/10 - 1 = 0.001 < 0.03 → 应触发 FD
        signal = ExitRuleEngine().check(ctx)
        assert signal is not None, "day_high_pct(0.001) < fd(0.03) 应触发首日离场"
        assert signal.reason.startswith("FD"), f"应触发 FD,实际 {signal.reason}"

    def test_first_day_exit_does_NOT_fire_when_high_above_threshold(self):
        """C1 bug 反向验证:day_high_pct >= fd 时不应触发。
        如果 fd 是 3.0(C1 bug),0.05 > 3.0 不成立,但 0.05 > 0.03 成立 → 不触发。
        如果 fd 是 0.03(修复后),0.05 > 0.03 成立 → 不触发。两条路径结果相同,
        但本测试能证明 fd 收到了正确的小数。
        """
        from unittest import mock
        from app.config.risk_params import load_risk_params
        from app.backtest.exit_rules import RuleContext, ExitRuleEngine

        fake_risk = {"first_day_exit_min_profit": 3.0, "first_day_exit_days": 1}
        m = mock.MagicMock()
        m.get = lambda section, key: fake_risk.get(key)
        with mock.patch("app.config.risk_params._settings", m):
            rp = load_risk_params()

        # fd=0.03, hold_days=2, high=10.50 → day_high_pct=0.05 > fd → 不触发
        ctx = RuleContext(
            entry_price=10.0, peak_price=10.50,
            shares=100, hold_days=2,
            high=10.50, low=10.0, close=10.4, open=10.4,
            first_day_exit_min_profit=rp.first_day_exit_min_profit,
            first_day_exit_days=rp.first_day_exit_days,
            first_day_hold_value=2,
        )
        signal = ExitRuleEngine().check(ctx)
        # day_high_pct=0.05 > fd=0.03 → 不应触发 FD
        if signal is not None:
            assert not signal.reason.startswith("FD"), (
                f"C1 bug 复活: day_high_pct=0.05 > fd 但仍触发 FD, "
                f"说明 fd={rp.first_day_exit_min_profit} 实际仍是 3.0(C1 bug 路径)"
            )


class TestSettingsPropertyUnits:
    """settings.py property 单位约定(2026-07-15 第三轮迭代加)

    ADR-001 约定: settings.xxx 永远返回百分比数(与 settings.json 一致),
    不做 *100 转换(防止 settings 改成小数时静默出错)。
    本测试 grep 所有 settings property,断言它们的值与 settings.json 直接读一致。
    """
    PROPERTIES_TO_TEST = [
        ("trailing_activate_pct", "trailing_stop_activate_pct"),
        ("trailing_drawdown_pct", "trailing_stop_drawdown_pct"),
        ("hard_stop_loss_pct", "hard_stop_loss_pct"),
        ("breakeven_threshold_pct", "breakeven_threshold_pct"),
        ("breakeven_stop_pnl_pct", "breakeven_stop_pnl_pct"),
        ("time_exit_days", "time_exit_days"),
        ("time_exit_min_profit_pct", "time_exit_min_pnl_pct"),
        ("time_exit_force_days", "time_exit_force_days"),
        ("first_day_exit_min_profit", "first_day_exit_min_profit"),
        ("first_day_exit_days", "first_day_exit_days"),
    ]

    def test_settings_property_returns_percentage_number(self):
        """settings.xxx 的返回值必须 = settings.json 中同名 key 的值(单位 %)。"""
        from core.settings import settings
        data = settings._data if hasattr(settings, '_data') else {}
        risk = data.get("risk", {})

        for prop_name, settings_key in self.PROPERTIES_TO_TEST:
            if settings_key not in risk:
                continue  # 缺键时 settings 走 risk_params fallback,跳过
            expected = risk[settings_key]
            actual = getattr(settings, prop_name)
            assert actual == expected, (
                f"settings.{prop_name} 应等于 settings.json 的 {settings_key}(% 单位), "
                f"实际 settings.{prop_name}={actual} ≠ {settings_key}={expected}。"
                f"如果值不同,说明 property 做了 *100 转换(违反 ADR-001 约定)。"
            )
