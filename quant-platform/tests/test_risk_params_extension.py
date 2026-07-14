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
    """settings[run] 段 override 必须生效,default 失效。

    实现机制:settings 是单例,加载时一次性把 config/app_setting.json 读入 _data。
    本测试用 monkeypatch.setitem 把 [run] 段三个 key 注入 _data,验证
    load_position_params() 立刻读到新值(无需重启进程)。

    注意:core/settings.py 的 settings.set() 也会改 _data 但会持久化到磁盘 ——
    本测试用 monkeypatch 而不是 settings.set(),避免修改真实配置文件。
    测试结束 monkeypatch 自动还原 _data,不影响其他测试和后续运行时。
    """
    from core.settings import settings as _live_settings

    # 默认值基线
    baseline = load_position_params()
    assert baseline.initial_capital == 1_000_000
    assert baseline.position_size == 50_000
    assert baseline.min_buy_amt == 5_000

    # 注入 [run] 段 override(走 settings._data dict, 不落盘)
    if "run" not in _live_settings._data or _live_settings._data["run"] is None:
        monkeypatch.setitem(_live_settings._data, "run", {})
    monkeypatch.setitem(_live_settings._data["run"], "initial_capital", 2_000_000)
    monkeypatch.setitem(_live_settings._data["run"], "position_size", 100_000)
    monkeypatch.setitem(_live_settings._data["run"], "min_buy_amt", 8_000)

    # 重新加载,override 必须生效,旧默认值失效
    overridden = load_position_params()
    assert overridden.initial_capital == 2_000_000, (
        f"settings[run].initial_capital=2_000_000 必须生效, "
        f"实际 {overridden.initial_capital}"
    )
    assert overridden.position_size == 100_000, (
        f"settings[run].position_size=100_000 必须生效, "
        f"实际 {overridden.position_size}"
    )
    assert overridden.min_buy_amt == 8_000, (
        f"settings[run].min_buy_amt=8_000 必须生效, "
        f"实际 {overridden.min_buy_amt}"
    )

    # 还得验证:override 与 dataclass 旧字段独立 — baseline 副本字段还是 1_000_000
    assert baseline.initial_capital == 1_000_000, (
        "旧 PositionParams 实例应不受 override 影响(已构造就 frozen)"
    )

    # 还原验证:monkeypatch 撤销 _data[run] 的 setitem 后,新 load 应回到 default
    monkeypatch.delitem(_live_settings._data["run"], "initial_capital", raising=False)
    monkeypatch.delitem(_live_settings._data["run"], "position_size", raising=False)
    monkeypatch.delitem(_live_settings._data["run"], "min_buy_amt", raising=False)
    restored = load_position_params()
    assert restored.initial_capital == 1_000_000
    assert restored.position_size == 50_000
    assert restored.min_buy_amt == 5_000


def test_settings_override_streak_params(monkeypatch):
    """settings[run] 段 override 必须作用于 StreakParams。

    与 test_settings_override_position_params 同机制,确保两个 dataclass
    都走相同的 _g_run override 路径。
    """
    from core.settings import settings as _live_settings

    baseline = load_streak_params()
    assert baseline.loss_streak_halve == 3
    assert baseline.loss_streak_pause == 5
    assert baseline.pause_days == 3
    assert baseline.same_stock_cooldown == 20

    if "run" not in _live_settings._data or _live_settings._data["run"] is None:
        monkeypatch.setitem(_live_settings._data, "run", {})
    monkeypatch.setitem(_live_settings._data["run"], "loss_streak_halve", 7)
    monkeypatch.setitem(_live_settings._data["run"], "loss_streak_pause", 10)
    monkeypatch.setitem(_live_settings._data["run"], "pause_days", 5)
    monkeypatch.setitem(_live_settings._data["run"], "same_stock_cooldown", 30)

    overridden = load_streak_params()
    assert overridden.loss_streak_halve == 7
    assert overridden.loss_streak_pause == 10
    assert overridden.pause_days == 5
    assert overridden.same_stock_cooldown == 30

    # 还原后必须回到 default
    for k in ("loss_streak_halve", "loss_streak_pause", "pause_days", "same_stock_cooldown"):
        monkeypatch.delitem(_live_settings._data["run"], k, raising=False)
    restored = load_streak_params()
    assert restored.loss_streak_halve == 3
    assert restored.loss_streak_pause == 5
    assert restored.pause_days == 3
    assert restored.same_stock_cooldown == 20


def test_g_run_uses_default_when_key_missing():
    """_g_run 缺 key 时必须返回调用方传入的 default,不是 None。

    实现:_g_run 内显式 `val if val is not None else default` 兜底。
    这里用 magic mock 喂给 _settings.get 一个永远返回 None 的桩,
    验证 _g_run 真的用 default 兜底,而不是返回 settings 拿到的 None。
    """
    from unittest import mock
    from app.config.risk_params import _g_run

    # 桩:_settings.get("run", any_key) → None
    fake_settings = mock.MagicMock()
    fake_settings.get = lambda *args: None
    with mock.patch("app.config.risk_params._settings", fake_settings):
        # 缺 key 时 default 必须生效
        assert _g_run("absent_key_xyz", 999) == 999
        assert _g_run("another_missing", "fallback_str") == "fallback_str"
        # float default 也得兜住
        assert _g_run("third_missing", 1.5) == 1.5