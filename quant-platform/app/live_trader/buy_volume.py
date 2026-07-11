"""买入量板块取整(v1.2.2 §6)

主板/创业板:100 股整数倍
科创板(688):200 股整数倍
北交所(8/4/920 开头):100 股整数倍

与卖出侧 _calc_sell_volume 对称。
"""
from app.utils.xtquant_compat import format_code, strip_code_suffix


def _calc_buy_volume(code: str, amount: float, price: float) -> int:
    """计算买入股数(板块取整)

    Args:
        code: 股票代码(支持带/不带后缀)
        amount: 期望买入金额(元)
        price: 买入单价(元)

    Returns:
        取整后的股数(≥0)
    """
    if price <= 0 or amount <= 0:
        return 0

    raw_shares = amount / price
    bare = strip_code_suffix(code) if '.' in code else code

    # 科创板(688):200 股整数倍
    if bare.startswith("688"):
        lot = 200
    else:
        # 主板/创业板/北交所:100 股整数倍
        lot = 100

    shares = int(raw_shares // lot) * lot
    return max(shares, 0)
