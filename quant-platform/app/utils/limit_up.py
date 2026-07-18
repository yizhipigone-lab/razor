from typing import Tuple


LIMIT_UP_MAP = {
    "300": 0.20,
    "301": 0.20,
    "688": 0.20,
    "8": 0.30,
    "4": 0.30,
}
DEFAULT_LIMIT_UP = 0.10


def get_limit_up_pct(code: str) -> float:
    """根据股票代码前缀返回涨停幅度。"""
    if code.startswith(("300", "301", "688")):
        return LIMIT_UP_MAP["300"]
    if code.startswith(("8", "4")):
        return LIMIT_UP_MAP["8"]
    return DEFAULT_LIMIT_UP


def _is_valid_price(x) -> bool:
    """价格有效性检查：非 None、非 NaN、大于 0。"""
    return x is not None and x > 0


def is_limit_up(
    code: str,
    prev_close: float,
    price: float,
    strict: bool = True,
) -> Tuple[bool, str]:
    """判断 price 是否达到 code 的涨停价。

    Returns:
        (是否涨停, reason)
    """
    if not (_is_valid_price(prev_close) and _is_valid_price(price)):
        if strict:
            return True, "missing_price_data"
        return False, "missing_price_data_ok"

    change = (price - prev_close) / prev_close
    limit = get_limit_up_pct(code)
    if change >= limit * 0.995:
        return True, f"limit_up({change * 100:.1f}%)"
    return False, "OK"
