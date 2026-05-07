"""
策略基类 (Pandas 驱动)
"""
from abc import ABC, abstractmethod
import pandas as pd

class BaseStrategy(ABC):
    """
    选股策略基类。
    子类实现 generate_signals()，接收全市场 K 线宽表 (Pandas)，返回信号结果。
    """

    name: str = "BaseStrategy"
    description: str = ""

    def __init__(self, params: dict = None):
        self.params = params or self.default_params()

    def default_params(self) -> dict:
        return {}

    @abstractmethod
    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        """
        输入全市场 K 线 (含 code, date, open, high, low, close, volume)
        """
        raise NotImplementedError

    def get_info(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "params": self.params,
        }
