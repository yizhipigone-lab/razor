"""
统一成交执行层 (L25 修复)
按 spec: 4 引擎(simple/tdx/strict/engine)统一以下 4 件事
1. 涨停买入过滤(can_buy)
2. T+1 约束(can_sell_today)
3. 买入成本(calc_buy_cost)
4. 卖出收入(calc_sell_revenue)
"""
from datetime import date
from typing import Tuple


# 涨幅表
LIMIT_UP_MAP = {
    '300': 0.20, '301': 0.20, '688': 0.20,  # 创业/科创 ±20%
    '8': 0.30, '4': 0.30,                    # 北证 ±30%
}
DEFAULT_LIMIT_UP = 0.10  # 主板 ±10%


def get_limit_up_pct(code: str) -> float:
    """根据股票代码前缀返回涨停幅度"""
    if code.startswith(('300', '301', '688')):
        return LIMIT_UP_MAP['300']
    if code.startswith(('8', '4')):
        return LIMIT_UP_MAP['8']
    return DEFAULT_LIMIT_UP


def can_buy(code: str, prev_close: float, today_high: float) -> Tuple[bool, str]:
    """涨停封板不能买入

    Args:
        code: 股票代码
        prev_close: 昨日收盘价
        today_high: 今日最高价

    Returns:
        (ok, reason): ok=True 可买,ok=False 不可买及原因
    """
    if prev_close <= 0 or today_high <= 0:
        return True, "OK"
    change = (today_high - prev_close) / prev_close
    limit = get_limit_up_pct(code)
    if change >= limit * 0.995:  # 0.5% 容差(向上,接近 limit 视为涨停)
        return False, f"涨停封板({change*100:.1f}%)"
    return True, "OK"


def can_sell_today(entry_date: date, today: date) -> bool:
    """T+1: 买入当天不能卖"""
    return today > entry_date  # 严格大于


# 默认成本配置（settings 缺失时的 fallback）
DEFAULT_COST_CFG = {
    'commission_rate': 0.00025,   # 万2.5
    'min_commission': 5.0,         # 最低 5 元
    'stamp_tax_rate': 0.0005,     # 千0.5(卖时)
    'slippage_rate': 0.001,        # 万10 双边
}


def get_cost_cfg() -> dict:
    """成本率唯一真相源：优先读 config(backtest.cost)，缺失回退 DEFAULT_COST_CFG。
    任务一前 execution 走硬编码，现统一从 settings 读，禁止散落硬编码。"""
    try:
        from core.settings import settings
        cfg = settings.get("backtest", "cost", default=None)
        if isinstance(cfg, dict) and cfg:
            return {**DEFAULT_COST_CFG, **cfg}
    except Exception:
        pass
    return dict(DEFAULT_COST_CFG)


def calc_buy_cost(price: float, shares: int, cfg: dict = None) -> dict:
    """买入成本 = 毛额 + 佣金 + 滑点"""
    cfg = {**get_cost_cfg(), **(cfg or {})}
    gross = price * shares
    commission = max(gross * cfg['commission_rate'], cfg['min_commission'])
    slippage = gross * cfg['slippage_rate']
    return {
        'gross': gross,
        'commission': commission,
        'slippage': slippage,
        'total': gross + commission + slippage,
    }


def calc_sell_revenue(price: float, shares: int, cfg: dict = None) -> dict:
    """卖出净收入 = 毛额 - 佣金 - 印花 - 滑点"""
    cfg = {**get_cost_cfg(), **(cfg or {})}
    gross = price * shares
    commission = max(gross * cfg['commission_rate'], cfg['min_commission'])
    stamp_tax = gross * cfg['stamp_tax_rate']
    slippage = gross * cfg['slippage_rate']
    return {
        'gross': gross,
        'commission': commission,
        'stamp_tax': stamp_tax,
        'slippage': slippage,
        'total': gross - commission - stamp_tax - slippage,
    }
