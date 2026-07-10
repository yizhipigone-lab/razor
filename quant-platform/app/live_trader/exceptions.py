"""实盘交易模块异常层级(v5.4 §5.1)

借鉴 MQ qmt_exceptions.py,简化为单层。
"""
from typing import Optional


class LiveTraderError(Exception):
    """实盘模块基类"""

    def __init__(self, message: str, code: int = 1001):
        super().__init__(message)
        self.message = message
        self.code = code


class QmtConnectionError(LiveTraderError):
    """QMT 连接失败"""

    def __init__(self, message: str, code: int = 1101):
        super().__init__(message, code)


class QmtTimeoutError(LiveTraderError):
    """xtquant 调用超时(v5.1 §10.2 3秒超时)"""

    def __init__(self, message: str, code: int = 1102):
        super().__init__(message, code)


class QmtOrderError(LiveTraderError):
    """下单失败"""

    def __init__(self, message: str, code: int = 1104):
        super().__init__(message, code)


class CircuitOpenError(LiveTraderError):
    """熔断器开启(§5.2)"""

    def __init__(self, message: str = "熔断器开启,拒绝 QMT 调用", code: int = 1105):
        super().__init__(message, code)
