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


def get_limit_down_pct(code: str) -> float:
    """根据股票代码前缀返回跌停幅度(与涨停对称)

    v5.4 新增:实盘 C2 跌停不可成交性判断需要。
    主板 ±10%,创业板/科创 ±20%,北证 ±30%。
    """
    return get_limit_up_pct(code)


def is_limit_down(code: str, last_price: float, prev_close: float, tolerance: float = 0.005) -> bool:
    """判断当前是否已跌停(C2:跌停时市价清仓无意义,跳过)

    Args:
        code: 股票代码
        last_price: 最新价
        prev_close: 昨收价
        tolerance: 容差(0.5%,向下接近跌停价视为已跌停)

    Returns:
        True=已跌停,应跳过强平
    """
    if prev_close <= 0 or last_price <= 0:
        return False
    down_pct = get_limit_down_pct(code)
    limit_down_price = prev_close * (1 - down_pct)
    # 最新价 <= 跌停价*(1+容差) 视为已跌停
    return last_price <= limit_down_price * (1 + tolerance)


def is_suspended_or_locked(code: str, last_price: float, prev_close: float,
                            today_open: float, today_high: float, today_low: float) -> bool:
    """判断停牌/一字板(C2:这类票市价清仓无意义)

    一字板:开盘=最高=最低=昨收±涨停/跌停(全天锁死)
    停牌:无最新价或开高低全为0
    """
    if last_price <= 0 or today_open <= 0:
        return True  # 停牌
    # 一字板:振幅几乎为0且接近涨跌停
    if today_high > 0 and today_low > 0 and today_high == today_low:
        return True
    return False


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
