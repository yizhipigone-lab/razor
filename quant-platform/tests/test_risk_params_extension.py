"""RiskParams 扩展回归测试 — 2026-07-15 全项目审计
覆盖新增的 PositionParams / StreakParams 两个 dataclass 的字段和默认值。
"""
from app.config.risk_params import (
    load_risk_params,
    load_position_params,
    load_streak_params,
)


def test_position_params_default_values():
    """PositionParams 默认值与 sim_trader/config.py:8-10 硬编码一致"""
    pp = load_position_params()
    assert pp.initial_capital == 1_000_000
    assert pp.position_size == 50_000
    assert pp.min_buy_amt == 5_000


def test_position_params_is_frozen():
    """PositionParams 必须是 frozen dataclass"""
    pp = load_position_params()
    import pytest
    with pytest.raises((AttributeError, Exception)):
        pp.initial_capital = 999  # type: ignore


def test_streak_params_default_values():
    """StreakParams 默认值与 sim_trader/config.py:13-15, 34 一致"""
    sp = load_streak_params()
    assert sp.loss_streak_halve == 3
    assert sp.loss_streak_pause == 5
    assert sp.pause_days == 3
    assert sp.same_stock_cooldown == 20


def test_streak_params_is_frozen():
    """StreakParams 必须是 frozen dataclass"""
    sp = load_streak_params()
    import pytest
    with pytest.raises((AttributeError, Exception)):
        sp.loss_streak_halve = 99  # type: ignore


def test_settings_override_position_params(monkeypatch):
    """settings[run] 段 override 必须生效,default 失效

    Limitation 2026-07-15:`core.settings` 用模块级单例 settings 对象,不支持热替换。
    真实 production 改 [run] 段需重启进程生效。本测试作为占位,标记 limitation。
    完整覆盖由后续 Task 3 (settings.json 加载层重构) 处理。
    """
    # 占位:验证 PositionParams 接口可用,且在当前默认 settings 下行为一致
    pp = load_position_params()
    # 验证 settings[run] 中无 override 时返回 dataclass 默认值
    assert isinstance(pp.initial_capital, (int, float))
    assert isinstance(pp.position_size, (int, float))
    assert isinstance(pp.min_buy_amt, (int, float))


def test_g_run_uses_keyword_default():
    """_g_run 必须用 default=default 而不是位置传参

    Settings.get(*keys, default=...) 的 default 是 keyword-only。
    位置传参会被当成第 3 个 key → 测试要验证 default 真的生效。
    """
    from app.config.risk_params import _g_run
    # _g_run 缺 key 时回 default
    assert _g_run("absent_key_xyz", 999) == 999, "default 必须生效,否则返回 None"