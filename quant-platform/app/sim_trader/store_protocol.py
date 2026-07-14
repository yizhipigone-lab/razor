"""
模拟盘存储接口契约（Protocol）

2026-07-14 抽出。三个 adapter 显式实现本 Protocol:
  - SimTraderStore  (DuckDB, 运行态)
  - JsonSimStore    (JSON 文件, 无锁)
  - InMemoryStore   (纯 dict, 测试用)

返回格式约定（load_equity_curve）:
  返回 list[dict], 每个 dict 必有键:
    date (str YYYY-MM-DD), equity (float), cash (float),
    pos  (int)   ← 统一键名
    source (str, optional)
  注: SimTraderStore 历史用 'positions' 键, 现已同时输出 'pos' (向后兼容,
      老消费方读 'positions' 不受影响; 新代码应读 'pos')。

clear_all() 为可选方法（不在 Protocol 必选），测试/回放需要清空时调用；
SimTraderStore 与 JsonSimStore 均已实现。
"""
from datetime import date
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class SimStore(Protocol):
    """模拟盘存储接口契约。所有 adapter 必须实现。"""

    def load_state(self) -> dict: ...
    def save_state(self, cash: float, consecutive_losses: int,
                   pause_until: Optional[date], trade_count: int) -> None: ...
    def load_positions(self) -> dict: ...
    def save_positions(self, positions: dict) -> None: ...
    def save_trade(self, trade) -> None: ...
    def load_trades(self) -> list: ...
    def save_equity_point(self, d: date, equity: float, cash: float,
                          positions: int, source: str = 'record') -> None: ...
    def load_equity_curve(self) -> list: ...
    def save_prev_day_snap(self, snap: dict) -> None: ...
    def load_prev_day_snap(self) -> dict: ...
