"""风控参数单位约定全链路对照表 + 往返测试（活文档）。

> 依据：ADR-001 + 全局 audit-verification.md 原则 7（单位/量纲系统要建全链路对照表）。
> 2026-07-20 多轮审计漏查 /100 bug 后补：用一张表锁死每个风控字段在
> settings(百分数) → RiskParams(小数) 这条链路上的约定, 防止再被改错。

本项目并存多套单位约定（审计时务必逐字段对照, 不能假设）：
  - settings.risk 段: 百分数 (hard_stop_loss_pct: -6.0 表示 -6%)
  - settings.risk.take_profit_tiers 内层: 小数 (profit_pct: 0.03 表示 3%) ← 例外
  - RiskParams / 引擎: 小数 (hard_stop: -0.06)
  - 回测配置 cfg(backtest 段): 小数
  - 搜索空间(optimizer.search_space): 百分数 (tp_ratio 例外是小数)
  - simulate_one_trade params_override: 混合 (pct 字段小数, tp*_profit 百分数)

本测试只锁 settings → RiskParams 这条后端链路。
前端 → settings 的往返由 Playwright 端到端测试覆盖(tests/e2e/)。
"""
from unittest import mock
import pytest

from app.config.risk_params import load_risk_params


# ── 单位约定全链路对照表（审计依据, 改字段单位必同步改这里）──
# (settings_key, 期望单位, RiskParams 字段名, load 后期望单位)
#   pct    = settings 存百分数, load 时 /100 得小数
#   int    = settings 存整数, load 不转换
#   tiers  = settings 存小数 list, load 不转换
RISK_FIELD_CONVENTIONS = [
    # ── pct 字段: settings 百分数 → RiskParams 小数 ──
    ("hard_stop_loss_pct",         "pct", "hard_stop",                  -6.0, -0.06),
    ("trailing_stop_activate_pct", "pct", "trail_activate",              5.0,  0.05),
    ("trailing_stop_drawdown_pct", "pct", "trail_dd",                    2.0,  0.02),
    ("time_exit_min_profit_pct",   "pct", "time_exit_profit",            3.0,  0.03),
    # 以下两个是 2026-07-20 修过 bug 的字段, 显式锁死
    ("first_day_exit_min_profit",  "pct", "first_day_exit_min_profit",   3.0,  0.03),
    ("breakeven_threshold_pct",    "pct", "breakeven_threshold_pct",     2.0,  0.02),
    ("breakeven_stop_pnl_pct",     "pct", "breakeven_stop_pnl_pct",      1.0,  0.01),
    # ── int 字段: 不转换 ──
    ("time_exit_days",             "int", "time_exit_days",              7,    7),
    ("time_exit_force_days",       "int", "time_force_days",            12,   12),
    ("first_day_exit_days",        "int", "first_day_exit_days",         1,    1),
]


def _patch_risk(fake_risk: dict):
    """mock risk_params._settings, 只让 fake_risk 里的 key 命中, 其余走默认。"""
    m = mock.MagicMock()
    m.get = lambda section, key: fake_risk.get(key)
    return mock.patch("app.config.risk_params._settings", m)


class TestUnitConventionTable:
    """对照表本身要自洽: 每行 (settings 百分数, load 后小数) 关系正确。"""

    @pytest.mark.parametrize("settings_key,unit,rp_key,raw,loaded",
                             RISK_FIELD_CONVENTIONS)
    def test_each_field_loads_with_correct_unit(self, settings_key, unit,
                                                 rp_key, raw, loaded):
        """settings 存 raw → load_risk_params 后 getattr(rp, rp_key) == loaded。
        pct 字段: raw(百分数) /100 = loaded(小数); int 字段: 不变。"""
        with _patch_risk({settings_key: raw}):
            rp = load_risk_params()
        actual = getattr(rp, rp_key)
        assert actual == pytest.approx(loaded), (
            f"{settings_key}: settings={raw} 应 load 成 {loaded}, 实际 {actual}. "
            f"若 pct 字段实际==raw(没/100) 或 int 字段实际!=raw(误转), 都是单位 bug。"
        )


class TestBuggyFieldsLocked:
    """2026-07-20 修过 /100 bug 的字段, 单独点名锁死, 防回归。"""

    def test_first_day_exit_settings_pct_to_decimal(self):
        """前端 loadSettings/saveRiskSettings 与 settings 约定为百分数,
        load_risk_params 必须 /100。曾误判方向(差点删 /100), 此测试守住。"""
        with _patch_risk({"first_day_exit_min_profit": 3.0}):
            rp = load_risk_params()
        assert rp.first_day_exit_min_profit == pytest.approx(0.03)

    def test_breakeven_both_fields_pct_to_decimal(self):
        """breakeven_threshold/stop: settings 百分数 → 引擎小数。
        exit_rules rule_breakeven_stop 用 entry*(1+breakeven_stop), 必须小数。"""
        with _patch_risk({"breakeven_threshold_pct": 2.0,
                          "breakeven_stop_pnl_pct": 0.5}):
            rp = load_risk_params()
        assert rp.breakeven_threshold_pct == pytest.approx(0.02)
        assert rp.breakeven_stop_pnl_pct == pytest.approx(0.005)


class TestSettingsRoundtripNoConversion:
    """settings 模块本身不做单位转换: 存什么读什么。
    前端存百分数, settings 就存百分数(不 /100), load 时才 /100。
    """

    def test_settings_get_returns_what_set(self):
        """core.settings 往返不转换(用临时 key, 不污染 risk 段真实配置)。"""
        from core.settings import settings
        test_key = "_unit_convention_test_probe"
        test_section = "_test_only"
        try:
            settings.set(test_section, test_key, -60, save=False)
            assert settings.get(test_section, test_key) == -60, \
                "settings 往返不应做任何缩放(存 -60 读必 -60)"
        finally:
            # 清理内存, 不落盘
            try:
                if hasattr(settings, "_data"):
                    settings._data.pop(test_section, None)
            except Exception:
                pass
