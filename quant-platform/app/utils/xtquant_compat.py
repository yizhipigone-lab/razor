"""xtquant 兼容层(v5.4 §18.8 / §19.7.1)

抽取自 qmt_proxy_server.py:87-113 的 4 层 XtAccount 兼容 + 安全取值 + 代码格式化。
qmt_proxy_server / qmt_wrapper / QMTGateway 三处共用,避免重复实现。

POC 验证(250516 版本):xttype.StockAccount 唯一可用,4 层降级只需第 1 层,
但保留完整降级链以兼容其他券商/版本。
"""
import math
from typing import Any, Optional


def get_stock_account_class() -> Any:
    """获取 StockAccount 类(4 层降级)

    POC 确认 xtquant 250516:
    - 第1层 xtquant.xttype.StockAccount ✅ 唯一可用
    - 第2层 xtquant.xttrader.XtAccount 不存在
    - 第3层 getattr(xtquant,'XtAccount') None
    - 第4层 MockXtAccount 兜底(不会触发,但保留)

    Returns:
        StockAccount 类(构造签名 (account_id, account_type='STOCK'))
    """
    # 第1层:xttype.StockAccount(MQ + POC 验证)
    try:
        from xtquant.xttype import StockAccount
        return StockAccount
    except ImportError:
        pass

    # 第2层:xttrader.XtAccount(部分版本)
    try:
        from xtquant.xttrader import XtAccount  # type: ignore
        return XtAccount
    except ImportError:
        pass

    # 第3层:顶层 getattr
    try:
        import xtquant
        cls = getattr(xtquant, 'XtAccount', None)
        if cls is not None:
            return cls
    except Exception:
        pass

    # 第4层:Mock 兜底(仅占位,实际不会触发)
    class MockXtAccount:  # type: ignore
        def __init__(self, account_id: str, account_type: str = "STOCK"):
            self.account_id = account_id
            self.account_type = account_type

    return MockXtAccount


def safe_getattr(obj: Any, field: str, default: Any = None) -> Any:
    """getattr 安全取值(防 QMT 对象版本差异崩溃)

    QMT 返回的 XtOrder/XtTrade/XtPosition 对象字段因版本不同可能有差异,
    全部用 safe_getattr 取值,禁止 obj.field 直接访问。
    """
    return getattr(obj, field, default)


def safe_float(val: Any, default: float = 0.0) -> float:
    """浮点清洗:NaN/Inf/None → default

    复制自 MQ positions_service.py:81-95。防 JSON 序列化失败和脏数据传播。
    """
    if val is None:
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def safe_int(val: Any, default: int = 0) -> int:
    """整数清洗:None/异常 → default"""
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return default


def format_code(code: str) -> str:
    """股票代码格式化:补市场后缀(.SH/.SZ/.BJ)

    规则(与 qmt_proxy_server 一致):
    - 已带点的直接返回
    - 6 开头 → .SH
    - 0/3 开头 → .SZ(含 000001 平安银行等股票)
    - 8/4/920 开头 → .BJ
    - 指数(999/880 开头)→ .SH

    注意:000001 在股票场景是平安银行(.SZ),不是上证指数。
    """
    if not code:
        return code
    code = code.strip()
    if '.' in code:
        return code
    # 指数特例(999/880 开头)
    if code.startswith(('999', '880')):
        return f'{code}.SH'
    # ETF:15/16/51/52 开头是深市 ETF,50/56 开头是沪市 ETF
    if code.startswith(('15', '16', '51', '52')):
        return f'{code}.SZ'
    if code.startswith(('50', '56')):
        return f'{code}.SH'
    if code.startswith('6'):
        return f'{code}.SH'
    if code.startswith('920') or code.startswith('8') or code.startswith('4'):
        return f'{code}.BJ'
    if code.startswith('0') or code.startswith('3'):
        return f'{code}.SZ'
    # 未知,默认 SH
    return f'{code}.SH'


def strip_code_suffix(code: str) -> str:
    """去除代码后缀(159226.SZ → 159226)"""
    if not code:
        return code
    return code.split('.')[0]


# price_type 枚举(POC 验证 250516 版本实际值,§8 固化)
# 延迟导入 xtconstant,避免模块加载时强依赖 xtquant(非 Windows 环境无此包)
PRICE_TYPE_FIX = 11            # 限价单
PRICE_TYPE_LATEST = 5          # 最新价
PRICE_TYPE_PEER_FIRST = 44     # 对手方最优(止损快成交)
PRICE_TYPE_SH_5_CANCEL = 42    # 沪市五档即时成交剩余撤销
PRICE_TYPE_SZ_5_CANCEL = 47    # 深市五档即时成交剩余撤销
PRICE_TYPE_BEST = 18           # 最优价

# order_type(POC 验证)
ORDER_TYPE_BUY = 23
ORDER_TYPE_SELL = 24

# 委托状态码(MQ 硬编码 48-57+255,xtconstant 无常量定义)
# 终态集合:释放清仓锁 + 标记 finished
ORDER_STATUS_TERMINAL = {53, 54, 56, 57}
# 在途白名单:可撤
ORDER_STATUS_INFLIGHT = {48, 49, 50, 51, 52, 55}

STATUS_TEXT = {
    48: "未报", 49: "待报", 50: "已报", 51: "已报待撤", 52: "部成待撤",
    53: "部撤", 54: "已撤", 55: "部成", 56: "已成", 57: "废单", 255: "未知",
}


def status_to_text(status: Any) -> str:
    """状态码 → 中文(用于前端展示)"""
    try:
        return STATUS_TEXT.get(int(status), f"未知({status})")
    except (TypeError, ValueError):
        return f"未知({status})"
