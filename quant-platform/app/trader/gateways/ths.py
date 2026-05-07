"""
同花顺 easytrader 下单网关
说明：
- 同花顺客户端会自动填充委托价（最新价），用户只需确认代码和数量
- 通过 easytrader 控制客户端 UI 完成自动化输入
"""
import math
from core.logger import get_logger, get_audit_logger
from core.settings import settings, calc_buy_volume

log = get_logger("THSGateway")
audit = get_audit_logger("THSGateway")


class THSGateway:
    """
    同花顺 easytrader 下单接口封装
    - 买入：只需传入股票代码和数量（价格由同花顺客户端自动填委托价）
    - 卖出：传入股票代码和数量
    """

    def __init__(self):
        self._trader = None
        self._connected = False

    def connect(self):
        """连接同花顺客户端"""
        try:
            import easytrader
            self._trader = easytrader.use("ths")
            ths_path = settings.get("gateway", "ths_client_path")
            if ths_path:
                self._trader.connect(ths_path)
            else:
                self._trader.connect()
            self._connected = True
            log.info("同花顺客户端连接成功")
        except Exception as e:
            self._connected = False
            log.error(f"同花顺连接失败: {e}")

    def buy(self, code: str, price: float, volume: int = None, reason: str = "手工买入") -> bool:
        """
        买入股票。
        volume: 为 None 时系统自动按金额上限计算。
        price: 当前最新价（用于计算手数），实际委托价由同花顺客户端填入。
        """
        if volume is None:
            volume = calc_buy_volume(price)

        if volume <= 0:
            log.warning(f"[{code}] 计算股数为0（单价 {price} 超出限额），跳过买入")
            return False

        audit.info(
            f"BUY | {code} | 参考价={price:.2f} | 数量={volume} | "
            f"预计金额≈{price*volume:.0f}元 | {reason}"
        )

        if not self._connected:
            log.warning("同花顺未连接，模拟下单（实际不会发出委托）")
            return False

        try:
            result = self._trader.buy(code, price=price, amount=volume)
            log.info(f"[{code}] 买入委托已发出: {result}")
            return True
        except Exception as e:
            log.error(f"[{code}] 买入委托异常: {e}")
            return False

    def sell(self, code: str, price: float, volume: int, reason: str = "手工卖出") -> bool:
        """
        卖出股票。price 由同花顺自动填入最新价，但我们仍传入用于校验计算。
        """
        audit.info(f"SELL | {code} | 参考价={price:.2f} | 数量={volume} | {reason}")

        if not self._connected:
            log.warning("同花顺未连接，模拟下单（实际不会发出委托）")
            return False

        try:
            result = self._trader.sell(code, price=price, amount=volume)
            log.info(f"[{code}] 卖出委托已发出: {result}")
            return True
        except Exception as e:
            log.error(f"[{code}] 卖出委托异常: {e}")
            return False

    def get_balance(self) -> dict:
        """获取账户余额信息"""
        if not self._connected:
            return {}
        try:
            return self._trader.balance
        except Exception as e:
            log.error(f"获取余额失败: {e}")
            return {}

    def get_position(self) -> list:
        """获取券商端持仓（用于比对核验）"""
        if not self._connected:
            return []
        try:
            return self._trader.position
        except Exception as e:
            log.error(f"获取持仓失败: {e}")
            return []


# 全局网关实例（程序启动时调用 .connect()）
ths_gateway = THSGateway()
