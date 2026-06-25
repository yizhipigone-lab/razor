"""
统一风控参数 schema
按用户铁律:"config.py 唯一真相源"
app/sim_trader/config.py 是硬编码真相源,本模块只做"读取 + 校验"
"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class RiskSchema:
    """风控参数 schema,缺键即报错,无假默认"""
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


def load_risk_params() -> RiskSchema:
    """从 app/sim_trader/config.py 加载(唯一真相源)"""
    import app.sim_trader.config as sc

    required = [
        'HARD_STOP', 'TRAIL_ACTIVATE', 'TRAIL_DD',
        'TIME_EXIT_DAYS', 'TIME_EXIT_PROFIT', 'TIME_FORCE_DAYS',
        'FIRST_DAY_EXIT_MIN_PROFIT', 'FIRST_DAY_EXIT_DAYS',
        'TAKE_PROFIT_TIERS',
    ]
    missing = [k for k in required if not hasattr(sc, k)]
    if missing:
        raise RuntimeError(
            f"app/sim_trader/config.py 缺少风控参数: {missing}\n"
            f"按用户铁律,缺键应直接报错,不允许假默认"
        )

    return RiskSchema(
        hard_stop=sc.HARD_STOP,
        trail_activate=sc.TRAIL_ACTIVATE,
        trail_dd=sc.TRAIL_DD,
        time_exit_days=sc.TIME_EXIT_DAYS,
        time_exit_profit=sc.TIME_EXIT_PROFIT,
        time_force_days=sc.TIME_FORCE_DAYS,
        first_day_exit_min_profit=sc.FIRST_DAY_EXIT_MIN_PROFIT,
        first_day_exit_days=sc.FIRST_DAY_EXIT_DAYS,
        take_profit_tiers=list(sc.TAKE_PROFIT_TIERS),
        use_atr_trail=getattr(sc, 'USE_ATR_TRAIL', False),
        atr_trail_multiplier=getattr(sc, 'ATR_TRAIL_MULTIPLIER', 1.0),
        breakeven_threshold=getattr(sc, 'BREAKEVEN_THRESHOLD', 0.0),
        breakeven_stop=getattr(sc, 'BREAKEVEN_STOP', 0.0),
    )