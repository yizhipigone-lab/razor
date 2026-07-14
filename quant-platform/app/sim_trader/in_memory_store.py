"""
模拟盘存储 — 纯内存 adapter（测试用）

2026-07-14 新增。目的: 让 PortfolioManager/RiskManager/EquityRecorder 等模块
可秒级单测, 无需启 DuckDB / 写文件。

行为对齐 JsonSimStore:
  - equity_curve 同日去重, 统一 'pos' 键, 带 source
  - positions 全量覆写, 保留完整运行态字段
  - state / prev_day_snap 独立槽位
  - clear_all() 清空一切
  注: save_trade/load_trades 额外保留 entry_reason(JsonSimStore 历史不存),
      测试场景下更完整, 不影响 engine(Trade.entry_reason 默认 "")。

不落盘: 所有数据存实例内存, 实例回收即消失。线程安全由调用方保证(测试单线程)。
"""
from datetime import date
from typing import Optional, Dict, List

from app.sim_trader.store_protocol import SimStore


class InMemoryStore:
    """纯 dict 模拟盘存储, 实现 SimStore Protocol。"""

    def __init__(self):
        self._state: dict = {
            'cash': 1_000_000.0,
            'consecutive_losses': 0,
            'pause_until': None,
            'trade_count': 0,
        }
        self._positions: Dict[str, dict] = {}
        self._trades: List[dict] = []
        self._equity_curve: List[dict] = []
        self._prev_day_snap: dict = {}

    # ── 引擎状态 ────────────────────────────────

    def load_state(self) -> dict:
        return {
            'cash': float(self._state.get('cash', 1_000_000)),
            'consecutive_losses': int(self._state.get('consecutive_losses', 0)),
            'pause_until': self._state.get('pause_until'),
            'trade_count': int(self._state.get('trade_count', 0)),
        }

    def save_state(self, cash: float, consecutive_losses: int,
                   pause_until: Optional[date], trade_count: int):
        self._state = {
            'cash': cash,
            'consecutive_losses': consecutive_losses,
            'pause_until': pause_until,
            'trade_count': trade_count,
        }

    # ── 持仓 ────────────────────────────────────

    def load_positions(self) -> dict:
        from app.sim_trader.models import Position
        result = {}
        for code, p in self._positions.items():
            # entry_date 防御性转 date(对齐 JsonSimStore, 防外部灌字符串日期)
            ed = p['entry_date']
            if not hasattr(ed, 'year'):
                ed = date.fromisoformat(str(ed))
            pos = Position(
                code=code, entry_date=ed,
                entry_price=p['entry_price'], shares=p['shares'],
                cost=p['cost'], strategy_name=p.get('strategy_name', ''),
                entry_time=p.get('entry_time', '15:00'),
            )
            if 'peak_price' in p:
                pos.peak_price = p['peak_price']
            if 'remaining_shares' in p:
                pos.remaining_shares = p['remaining_shares']
            pos.tp1_triggered = bool(p.get('tp1_triggered', pos.tp1_triggered))
            pos.tp2_triggered = bool(p.get('tp2_triggered', pos.tp2_triggered))
            pos.is_active = bool(p.get('is_active', pos.is_active))
            result[code] = pos
        return result

    def save_positions(self, positions: dict):
        self._positions = {
            code: {
                'entry_date': p.entry_date, 'entry_price': p.entry_price,
                'shares': p.shares, 'cost': p.cost,
                'strategy_name': p.strategy_name,
                'entry_time': getattr(p, 'entry_time', '15:00'),
                'peak_price': getattr(p, 'peak_price', p.entry_price),
                'remaining_shares': getattr(p, 'remaining_shares', p.shares),
                'tp1_triggered': getattr(p, 'tp1_triggered', False),
                'tp2_triggered': getattr(p, 'tp2_triggered', False),
                'is_active': getattr(p, 'is_active', True),
            }
            for code, p in positions.items()
        }

    # ── 交易记录 ────────────────────────────────

    def save_trade(self, trade):
        self._trades.append({
            'code': trade.code, 'entry_date': trade.entry_date,
            'exit_date': trade.exit_date, 'entry_price': trade.entry_price,
            'exit_price': trade.exit_price, 'shares': trade.shares,
            'return_pct': trade.return_pct, 'profit_amount': trade.profit_amount,
            'exit_reason': trade.exit_reason, 'hold_days': trade.hold_days,
            'entry_reason': getattr(trade, 'entry_reason', ''),
            'exit_timing': getattr(trade, 'exit_timing', 'close'),
            'entry_time': getattr(trade, 'entry_time', '15:00'),
            'exit_time': getattr(trade, 'exit_time', '15:00'),
        })

    def load_trades(self) -> list:
        from app.sim_trader.models import Trade
        return [Trade(
            code=t['code'], entry_date=t['entry_date'], exit_date=t['exit_date'],
            entry_price=t['entry_price'], exit_price=t['exit_price'],
            shares=t['shares'], return_pct=t['return_pct'],
            profit_amount=t['profit_amount'], exit_reason=t['exit_reason'],
            hold_days=t['hold_days'],
            entry_reason=t.get('entry_reason', ''),
            exit_timing=t.get('exit_timing', 'close'),
            entry_time=t.get('entry_time', '15:00'),
            exit_time=t.get('exit_time', '15:00'),
        ) for t in self._trades]

    # ── 净值曲线 ────────────────────────────────

    def save_equity_point(self, d: date, equity: float, cash: float,
                          positions: int, source: str = 'record'):
        # 同日去重, 统一 'pos' 键, 带 source (对齐 JsonSimStore)
        entry = {'date': str(d), 'equity': equity, 'cash': cash,
                 'pos': positions, 'source': source}
        if self._equity_curve and str(self._equity_curve[-1].get('date')) == str(d):
            self._equity_curve[-1] = entry
        else:
            self._equity_curve.append(entry)

    def load_equity_curve(self) -> List[dict]:
        return list(self._equity_curve)

    # ── 昨日快照 ────────────────────────────────

    def save_prev_day_snap(self, snap: dict):
        self._prev_day_snap = snap

    def load_prev_day_snap(self) -> dict:
        return self._prev_day_snap

    # ── 全量清空 ────────────────────────────────

    def clear_all(self):
        self._state = {'cash': 1_000_000.0, 'consecutive_losses': 0,
                       'pause_until': None, 'trade_count': 0}
        self._positions = {}
        self._trades = []
        self._equity_curve = []
        self._prev_day_snap = {}
