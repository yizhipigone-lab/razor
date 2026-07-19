"""市价类型映射(2026-07-18 手工下单功能 M1)

市价键 + 股票代码 → xtconstant int,市场感知降级。
纯函数、不 import xtquant(避免无 QMT 环境导入失败),便于单测。

市场判断口径: 股票 60/68→SH;00/30→SZ;8/4/920→BJ;基金 50/51/52/56/58→SH;15/16→SZ。
降级规则(参考 MQ trade-service.ts:251-287 的坑点):
- 北交所不支持五档类 → 降级对手最优(44) + warning
- 沪五档类用于非沪市 → 降级对手最优 + warning
- 深五档撤销用于非深市 → 降级对手最优 + warning
- "五档转限价"只有沪市常量(43),深交所不支持 → 深五档只提供"撤销"(47)
"""
from typing import Dict, Optional, Tuple

from core.logger import get_logger

logger = get_logger("live_trader.price_type")

# xtquant.xtconstant 值(硬编码避免 import xtquant;与 2026-07-18 实测一致)
FIX_PRICE = 11
LATEST_PRICE = 5
MARKET_PEER_PRICE_FIRST = 44
MARKET_MINE_PRICE_FIRST = 45
MARKET_SH_CONVERT_5_CANCEL = 42
MARKET_SH_CONVERT_5_LIMIT = 43
MARKET_SZ_CONVERT_5_CANCEL = 47

# key → (中文名, 是否需要价格输入, 限定市场或 None=全市场, xtconstant int)
PRICE_TYPE_KEYS: Dict[str, Tuple[str, bool, Optional[str], int]] = {
    "limit":      ("限价", True, None, FIX_PRICE),
    "latest":     ("最新价", False, None, LATEST_PRICE),
    "peer_best":  ("对手最优", False, None, MARKET_PEER_PRICE_FIRST),
    "mine_best":  ("本方最优", False, None, MARKET_MINE_PRICE_FIRST),
    "sh5_cancel": ("沪五档撤销", False, "SH", MARKET_SH_CONVERT_5_CANCEL),
    "sh5_limit":  ("沪五档转限价", False, "SH", MARKET_SH_CONVERT_5_LIMIT),
    "sz5_cancel": ("深五档撤销", False, "SZ", MARKET_SZ_CONVERT_5_CANCEL),
}

# 市价类(price=0 合法)的 key 集合 — T2 市价单金额估算用
MARKET_KEYS = {k for k, v in PRICE_TYPE_KEYS.items() if not v[1]}


def detect_market(code: str) -> str:
    """代码 → 市场(SH/SZ/BJ/UNKNOWN)

    股票: 60/68→SH;00/30→SZ;8/4/920→BJ。
    基金/ETF: 50/51/52/56/58→SH;15/16→SZ。
    """
    bare = code.split(".")[0]
    if bare.startswith(("60", "68", "50", "51", "52", "56", "58")):
        return "SH"
    if bare.startswith(("00", "30", "15", "16")):
        return "SZ"
    if bare.startswith(("8", "4", "920")):
        return "BJ"
    return "UNKNOWN"


def map_price_type(key: str, code: str) -> Tuple[int, Optional[str]]:
    """市价键 + 代码 → (xtconstant_int, warning|None)

    市场不匹配(含北交所五档)时降级对手最优(44)并返回 warning。
    未知 key 抛 ValueError。
    """
    if key not in PRICE_TYPE_KEYS:
        raise ValueError(f"未知价格类型: {key}")
    name, _needs_price, target_market, const = PRICE_TYPE_KEYS[key]
    market = detect_market(code)
    if target_market is None or target_market == market:
        return const, None
    warning = f"{name}仅支持{target_market}市场,{code}属{market}市场,已降级为对手最优"
    logger.warning(f"price_type 降级: {warning}")
    return MARKET_PEER_PRICE_FIRST, warning


def needs_price(key: str) -> bool:
    """该 key 是否需要用户输入价格(仅限价)"""
    if key not in PRICE_TYPE_KEYS:
        raise ValueError(f"未知价格类型: {key}")
    return PRICE_TYPE_KEYS[key][1]


def key_name(key: str) -> str:
    """该 key 的中文名(供确认弹窗/audit)"""
    if key not in PRICE_TYPE_KEYS:
        raise ValueError(f"未知价格类型: {key}")
    return PRICE_TYPE_KEYS[key][0]


def available_keys(code: str) -> list:
    """按代码市场返回可选 key 列表(前端下拉过滤用,R4)

    全市场类(limit/latest/peer_best/mine_best)恒可选;
    限定市场类只在对应市场出现;北交所只出全市场类。
    """
    market = detect_market(code)
    return [
        k for k, v in PRICE_TYPE_KEYS.items()
        if v[2] is None or v[2] == market
    ]
