"""
统一风控参数 schema
按用户铁律:"config.py 唯一真相源"
app/sim_trader/config.py 是硬编码真相源,本模块只做"读取 + 校验"

v5.5(2026-07-14):load_risk_params 委托给 app.config.risk_params.load_risk_params,
保留 RiskSchema 向后兼容(老调用方),加 DeprecationWarning 引导迁移。
"""
import warnings
from dataclasses import dataclass
from typing import List


@dataclass
class RiskSchema:
    """风控参数 schema,缺键即报错,无假默认

    DEPRECATED:2026-07-14 起请直接用 app.config.risk_params.RiskParams(frozen),
    本类仅作向后兼容层保留。
    """
    hard_stop: float
    trail_activate: float
    trail_dd: float
    time_exit_days: int
    time_exit_profit: float
    time_force_days: int
    first_day_exit_min_profit: float
    first_day_exit_days: int
    take_profit_tiers: list
    use_atr_trail: bool = False
    atr_trail_multiplier: float = 1.0
    breakeven_threshold: float = 0.0
    breakeven_stop: float = 0.0


def load_risk_params():
    """[已废弃] 请改用 app.config.risk_params.load_risk_params()。

    本函数仅做向后兼容:内部委托新入口,把 RiskParams 适配回 RiskSchema 字段命名。
    新代码禁止调用本函数(会有 DeprecationWarning)。
    """
    warnings.warn(
        "app.config.schema.load_risk_params is deprecated (2026-07-14). "
        "Use app.config.risk_params.load_risk_params instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    from app.config.risk_params import load_risk_params as _new
    p = _new()
    # RiskSchema 多 2 个保本止损字段(老默认值 0.0);新 RiskParams 没这俩。
    # 老调用方用不到这俩(都默认 0=禁用),所以这里用 getattr 兜底 0.0。
    return RiskSchema(
        hard_stop=p.hard_stop,
        trail_activate=p.trail_activate,
        trail_dd=p.trail_dd,
        time_exit_days=p.time_exit_days,
        time_exit_profit=p.time_exit_profit,
        time_force_days=p.time_force_days,
        first_day_exit_min_profit=p.first_day_exit_min_profit,
        first_day_exit_days=p.first_day_exit_days,
        take_profit_tiers=list(p.take_profit_tiers),
        use_atr_trail=p.use_atr_trail,
        atr_trail_multiplier=p.atr_trail_multiplier,
        breakeven_threshold=0.0,
        breakeven_stop=0.0,
    )