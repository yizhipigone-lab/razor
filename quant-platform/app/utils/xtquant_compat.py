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

    注意:000001 在股票场景是平安银行(.SZ),指数场景是上证指数(.SH)。
    冲突裸码(000001/000016/000300/000688/000852/000902-000987 等)由调用方
    显式判断股票/指数类型:股票保持原映射;指数场景按需走 .SH。
    """
    if not code:
        return code
    code = code.strip()
    if '.' in code:
        return code
    # ETF:15/16 开头是深市 ETF,50/51/52/56/58 开头是沪市 ETF
    if code.startswith(('15', '16')):
        return f'{code}.SZ'
    if code.startswith(('50', '51', '52', '56', '58')):
        return f'{code}.SH'
    if code.startswith('6'):
        return f'{code}.SH'
    # 申万综指等 880 指数→沪市(必须在 8→BJ 分支前,否则 880001 被误判北交所)
    if code.startswith('880'):
        return f'{code}.SH'
    if code.startswith('920') or code.startswith('8') or code.startswith('4'):
        return f'{code}.BJ'
    if code.startswith('0') or code.startswith('3'):
        return f'{code}.SZ'
    # 未知,默认 SH
    return f'{code}.SH'


# 中证/上证主要宽基指数代码(沪市,中证指数公司编制)
# 数据源:中证指数公司官网 + 东方财富 / 同花顺,2026-07-15 整理
# 命名:对于裸码,需结合 _CSI_INDEX_CODES 区分股票 vs 指数
_CSI_INDEX_CODES = frozenset({
    '000001',  # 上证指数
    '000016',  # 上证50
    '000300',  # 沪深300
    '000688',  # 科创50
    '000852',  # 中证1000
    '000902',  # 中证700
    '000903',  # 中证100
    '000904',  # 中证200
    '000905',  # 中证500
    '000906',  # 中证800
    '000933',  # 中证2000
    '000985',  # 中证全指
    '000987',  # 中证流通
    '880001',  # 申万综指
})


def is_index_code(code: str) -> bool:
    """判断裸码是否为已知指数代码(用于解决 000001 等股票+指数冲突)

    带后缀的码**永远返回 False**:后缀已确定身份(.SZ=深市股票,如 000001.SZ 平安银行;
    .SH 才可能是沪市指数)。严禁剥后缀反查指数表——旧实现剥后缀导致 000001.SZ
    被误判为上证指数,qmt_wrapper 据此查成 000001.SH 指数点位(2026-07-16 事故根因)。
    仅当传入裸码(无后缀)时,才查 _CSI_INDEX_CODES 判断是否为已知指数。
    """
    if not code:
        return False
    if '.' in code:
        return False  # 后缀已确定身份,不再二次判断
    return code in _CSI_INDEX_CODES


def format_index_code(code: str) -> str:
    """指数代码格式化:强制走 .SH
    用于前端/外部接口明确要查指数的场景(如对比沪深300/中证500走势)
    """
    if not code:
        return code
    bare = code.split('.')[0] if '.' in code else code
    return f'{bare}.SH'


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
