"""风控参数集中加载层(2026-07-14)

数据流:settings(app_setting.json) → config.py 兜底
与 engine._cfg() / intraday_monitor._cfg() / exit_monitor._load_risk_params() 行为完全一致。

设计:
- 唯一真相源统一在 `app/sim_trader/config.py`(模块级常量)+ `app_setting.json` 的 [risk] 段
- 调用方拿到 RiskParams(frozen dataclass,字段名稳定),不再自己拼 dict
- 向后兼容:`app/config/schema.py:load_risk_params()` 内部委托本函数,加 DeprecationWarning

测试可 mock:frozen dataclass 可在测试中替换整对象(`monkeypatch.setattr(rp, 'hard_stop', -0.07)`)
"""
from dataclasses import dataclass

from core.settings import settings as _settings


@dataclass(frozen=True)
class RiskParams:
    """风控参数 — frozen dataclass,所有字段已转小数(0.03 表示 3%)。

    与 engine/intraday_monitor/exit_monitor 历史上拼出的 sim_params dict 字段一一对应。
    """
    hard_stop: float                   # 负数,如 -0.06
    trail_activate: float              # 正数,如 0.05
    trail_dd: float
    take_profit_tiers: list            # [{"profit_pct": 0.03, "sell_ratio": 0.30}]
    time_exit_days: int
    time_exit_profit: float
    time_force_days: int
    first_day_exit_min_profit: float
    first_day_exit_days: int
    use_atr_trail: bool = False
    atr_trail_multiplier: float = 1.0
    breakeven_threshold_pct: float = 0.0     # 保本止损激活阈值(小数),0=禁用
    breakeven_stop_pnl_pct: float = 0.0      # 保本止损位(小数)


def _g(key: str, default):
    """读 settings[risk] → default 兜底。

    旧版有 config.py 二级兜底(getattr(_sc, key.upper(), default))，但:
    1. key.upper() 与 config.py 常量名不匹配(7/10 个 key 对不上: HARD_STOP vs HARD_STOP_LOSS_PCT 等)
    2. config.py 常量本身从 load_risk_params() 派生 → 循环依赖, fallback 永远不可达
    → 2026-07-15 全项目审计后移除死代码, 直接返回 default。
    """
    val = _settings.get("risk", key)
    if val is not None:
        return val
    return default


def load_risk_params() -> RiskParams:
    """唯一推荐入口:返回 RiskParams frozen dataclass。"""
    return RiskParams(
        hard_stop=_g("hard_stop_loss_pct", -6.0) / 100.0,
        trail_activate=_g("trailing_stop_activate_pct", 5.0) / 100.0,
        trail_dd=_g("trailing_drawdown_pct", 2.0) / 100.0,
        breakeven_threshold_pct=_g("breakeven_threshold_pct", 0.0) / 100.0,
        breakeven_stop_pnl_pct=_g("breakeven_stop_pnl_pct", 0.0) / 100.0,
        take_profit_tiers=_g("take_profit_tiers", [{"profit_pct": 0.03, "sell_ratio": 0.30}]),
        time_exit_days=_g("time_exit_days", 7),
        time_exit_profit=_g("time_exit_min_pnl_pct", 3.0) / 100.0,
        time_force_days=_g("time_exit_force_days", 12),
        first_day_exit_min_profit=_g("first_day_exit_min_profit", 0.0) / 100.0,
        first_day_exit_days=_g("first_day_exit_days", 1),
        use_atr_trail=_g("use_atr_stop", False),
        atr_trail_multiplier=_g("atr_stop_multiplier", 1.0),
    )


@dataclass(frozen=True)
class PositionParams:
    """资金 + 单票仓位控制参数。

    与 RiskParams 同源:settings[risk] + settings[run] 两段平级。
    旧 sim_trader/config.py:8-10 硬编码 7 个常量中 3 个资金参数迁到这里。
    """
    initial_capital: float      # 默认本金,如 1_000_000
    position_size: float        # 单票仓位上限,如 50_000
    min_buy_amt: float          # 最小买入金额,如 5_000


@dataclass(frozen=True)
class StreakParams:
    """连亏保护 + 冷却。

    旧 sim_trader/config.py:13-15, 34 硬编码的 4 个连亏/冷却常量迁到这里。
    """
    loss_streak_halve: int      # 连亏 N 笔仓位减半
    loss_streak_pause: int      # 连亏 N 笔暂停
    pause_days: int             # 暂停自然日
    same_stock_cooldown: int    # 同股票冷却天数


def _g_run(key: str, default):
    """读 settings[run] 段 → default 兜底。

    调用方式:val = _settings.get("run", key) — 只传 2 个位置 key。
    注意 Settings.get(*keys, default=...) 的 default 是 keyword-only,
    如果用 _settings.get("run", key, default) 第三个位置会再被当作第 3 个 key,
    反而取出深一层值 → 落进 None 兜底,行为不一致。

    本函数不依赖 keyword-only default,而是在 apply 层显式 None → 回退 default,
    语义更显式,可读 logger / raise 失败信号。

    对外接口 _g_run(key, default) 永远位置传参;这里的 default 是 Python 函数参,
    与 Settings.get 的 keyword-only default 无关,二者不要混用。
    """
    val = _settings.get("run", key)
    return val if val is not None else default


def load_position_params() -> PositionParams:
    """唯一推荐入口:返回 PositionParams frozen dataclass。

    默认值与 app/sim_trader/config.py:8-10 现有硬编码一致,
    不破坏现有调用方。后续 Task 2 让 tdx_runner 改用本函数。
    """
    return PositionParams(
        initial_capital=_g_run("initial_capital", 1_000_000),
        position_size=_g_run("position_size", 50_000),
        min_buy_amt=_g_run("min_buy_amt", 5_000),
    )


def load_streak_params() -> StreakParams:
    """唯一推荐入口:返回 StreakParams frozen dataclass。

    默认值与 app/sim_trader/config.py:13-15, 34 现有硬编码一致。
    """
    return StreakParams(
        loss_streak_halve=_g_run("loss_streak_halve", 3),
        loss_streak_pause=_g_run("loss_streak_pause", 5),
        pause_days=_g_run("pause_days", 3),
        same_stock_cooldown=_g_run("same_stock_cooldown", 20),
    )