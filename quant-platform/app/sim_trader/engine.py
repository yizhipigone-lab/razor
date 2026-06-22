"""
模拟盘交易 — 核心引擎
每日流程:
  14:52 — 止盈止损卖出（先回收现金）
  14:54 — 选股买入
  14:56 — 记录净值
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pandas as pd
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from collections import Counter

from app.sim_trader.config import *
from core.logger import get_logger

log = get_logger("SimEngine")

_stock_name_cache = {}

def _get_stock_name(code: str) -> str:
    """查股票简称，带缓存"""
    if code in _stock_name_cache:
        return _stock_name_cache[code]
    try:
        from database.duckdb_manager import db
        df = db.conn.execute("SELECT name FROM stocks WHERE code = ?", [code]).fetchdf()
        name = str(df.iloc[0]['name']) if not df.empty else ''
    except Exception:
        name = ''
    _stock_name_cache[code] = name
    return name


def _safe_broadcast(data: dict):
    """安全广播到前端（非 server 环境静默跳过）"""
    try:
        from server.websocket.manager import sync_broadcast
        sync_broadcast(data)
    except Exception:
        pass
    # 同步写日志文件持久化
    _write_log(data)


def _write_log(entry: dict):
    """追加一行 JSON 到当日日志文件"""
    import json, os
    from datetime import date
    try:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'output', 'sim_trader', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f'{date.today()}.jsonl')
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
    except Exception:
        pass


@dataclass
class Position:
    code: str
    entry_date: date
    entry_price: float
    shares: int
    cost: float
    peak_price: float = 0.0
    remaining_shares: int = 0
    tp1_triggered: bool = False
    tp2_triggered: bool = False
    is_active: bool = True
    strategy_name: str = ""
    entry_time: str = "15:00"
    current_price: float = 0.0  # #8 修复:由 record() 阶段从 snapshot 写入

    def __post_init__(self):
        self.peak_price = self.entry_price
        self.remaining_shares = self.shares

    @property
    def market_value(self) -> float:
        return self.remaining_shares * self.current_price  # #8 修复:用当前价

    @property
    def profit_pct(self) -> float:  # #8 修复:从方法变 property
        if self.current_price <= 0:
            return 0.0
        return (self.current_price / self.entry_price - 1) * 100

    def is_tier_triggered(self, idx: int) -> bool:
        return self.tp1_triggered if idx == 0 else self.tp2_triggered

    def mark_tier_triggered(self, idx: int):
        if idx == 0:
            self.tp1_triggered = True
        else:
            self.tp2_triggered = True


@dataclass
class Trade:
    code: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    shares: int
    return_pct: float
    profit_amount: float
    exit_reason: str
    hold_days: int
    entry_reason: str = ""
    exit_timing: str = "close"  # "intraday" | "close"
    entry_time: str = "15:00"
    exit_time: str = "15:00"


class SimTraderEngine:
    """尾盘模拟交易引擎（含盘中实时监控）"""

    def __init__(self, store=None):
        if store is not None:
            self._store = store
            state = self._store.load_state()
            self.cash = state['cash']
            self.consecutive_losses = state['consecutive_losses']
            self.pause_until = state['pause_until']
            self._trade_count = state['trade_count']
            self.positions = self._store.load_positions()
            self.trades = self._store.load_trades()
            self.equity_curve = self._store.load_equity_curve()
        else:
            self.cash = INITIAL_CAPITAL
            self.positions: Dict[str, Position] = {}
            self.trades: List[Trade] = []
            self.equity_curve: List[Dict] = []
            self.consecutive_losses = 0
            self.pause_until: Optional[date] = None
            self._trade_count = 0

        self._prev_snap: dict = {}  # 前一日快照，用于除权跳空保护

        # 启动时补齐缺失的交易日净值快照（防止曲线断档）
        if store is not None:
            self._fill_missing_snapshots()

        # 盘中监控器（延迟初始化，避免循环导入）
        self._monitor = None

    def _fill_missing_snapshots(self):
        """补齐 equity_curve 中缺失的交易日快照。
        从最后一个快照日到昨天，逐日查 parquet 收盘价计算净值。"""
        if not self.equity_curve:
            return
        try:
            from datetime import date as _d, timedelta
            last_date = _d.fromisoformat(str(self.equity_curve[-1]['date']))
            yesterday = _d.today() - timedelta(days=1)

            # 获取交易日列表
            trading_dates = set()
            try:
                from app.api.sim_trader import _load_trading_calendar
                trading_dates = _load_trading_calendar()
            except Exception:
                pass

            # 逐日检查到昨天
            d = last_date + timedelta(days=1)
            while d <= yesterday:
                if d.weekday() >= 5:  # 跳过周末
                    d += timedelta(days=1)
                    continue
                if trading_dates and d not in trading_dates:
                    d += timedelta(days=1)
                    continue

                # 查 parquet 收盘价，计算持仓市值
                total_mv = 0.0
                daily_dir = None
                try:
                    from pathlib import Path as _Path
                    import pandas as pd
                    root = _Path(__file__).resolve().parent.parent.parent
                    daily_dir = root / "data" / "parquet" / "daily"
                except Exception:
                    pass

                for code, pos in self.positions.items():
                    if not pos.is_active or pos.remaining_shares <= 0:
                        continue
                    close_px = pos.entry_price  # 兜底用买入价
                    if daily_dir:
                        f = daily_dir / f"{code}.parquet"
                        if f.exists():
                            try:
                                df = pd.read_parquet(str(f), columns=['date', 'close'])
                                df['date'] = pd.to_datetime(df['date']).dt.date
                                row = df[df['date'] == d]
                                if not row.empty:
                                    close_px = float(row.iloc[0]['close'])
                            except Exception:
                                pass
                    total_mv += pos.remaining_shares * close_px

                equity = self.cash + total_mv
                self.equity_curve.append({
                    'date': str(d), 'equity': equity,
                    'cash': self.cash, 'positions': self.position_count,
                })
                # 持久化
                if self._store:
                    self._store.save_equity_point(d, equity, self.cash, self.position_count)
                log.info(f"[快照补录] {d} 净值={equity:,.0f}（启动时自动补齐）")
                d += timedelta(days=1)
        except Exception as e:
            log.warning(f"[快照补录] 跳过: {e}")

    @property
    def monitor(self):
        if self._monitor is None:
            try:
                from app.sim_trader.intraday_monitor import IntradayMonitor
                self._monitor = IntradayMonitor(self)
            except ImportError:
                return None
        return self._monitor

    @property
    def monitor_enabled(self) -> bool:
        if self.monitor is None:
            return False
        return getattr(self.monitor, 'enabled', False)

    @property
    def auto_sell(self) -> bool:
        from app.sim_trader.config import AUTO_SELL
        return AUTO_SELL

    @property
    def auto_buy(self) -> bool:
        from app.sim_trader.config import AUTO_BUY
        return AUTO_BUY

    @property
    def auto_scan(self) -> bool:
        from app.sim_trader.config import AUTO_SCAN
        return AUTO_SCAN

    # ── 仓位管理 ──────────────────────────────

    def max_buy_amount(self) -> float:
        if self.consecutive_losses >= LOSS_STREAK_HALVE:
            return POSITION_SIZE / 2
        return POSITION_SIZE

    def active_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.is_active]

    @property
    def position_count(self) -> int:
        return len(self.active_positions())

    def total_equity(self, snapshot: dict) -> float:
        """snapshot: {code: {'close': float, ...}}"""
        pv = sum(
            p.remaining_shares * snapshot.get(p.code, {}).get('close', p.entry_price)
            for p in self.active_positions()
        )
        return self.cash + pv

    # ── 买入 ──────────────────────────────────

    def execute_buy(self, today: date, code: str, price: float,
                    strategy_name: str = "") -> Optional[Position]:
        """买入一只股票，收盘价成交"""
        if code in self.positions:
            return None
        max_amt = min(self.max_buy_amount(), self.cash)
        if max_amt < MIN_BUY_AMT:
            return None
        shares = int(max_amt / price / 100) * 100
        if shares < 100:
            return None
        cost = shares * price
        if cost > self.cash:
            return None

        pos = Position(code=code, entry_date=today, entry_price=price,
                       shares=shares, cost=cost, strategy_name=strategy_name,
                       entry_time=datetime.now().strftime('%H:%M:%S'))
        self.cash -= cost
        self.positions[code] = pos
        log.info(f"[买入] {code} 价格={price:.2f} 数量={shares} 金额={cost:.0f} 剩余现金={self.cash:.0f} 策略={strategy_name}")
        _safe_broadcast({"type":"sim_trader_log","action":"buy","code":code,"name":_get_stock_name(code),"price":round(price,2),"shares":shares,"cost":round(cost,0),"cash":round(self.cash,0),"strategy":strategy_name,"date":str(today),"time":datetime.now().strftime('%H:%M:%S')})
        if self._store:
            self._store.save_positions(self.positions)
            self._store.save_state(
                self.cash, self.consecutive_losses,
                self.pause_until, self._trade_count)
        return pos

    # ── 卖出（止盈止损） ──────────────────────

    def check_stops(self, today: date, snapshot: dict,
                    trading_dates: List[date],
                    prev_snap: dict = None,
                    readonly: bool = False) -> List[Tuple]:
        """
        按优先级检查所有持仓的止盈止损。
        readonly=True 时不修改持仓状态（用于告警模式）。
        返回: [(pos, exit_price, reason, partial_shares_or_None), ...]
        """
        from app.backtest.exit_rules import exit_rule_engine

        sells = []
        for code, pos in list(self.positions.items()):
            if not pos.is_active or pos.remaining_shares <= 0:
                continue

            bar = snapshot.get(code)
            if bar is None:
                continue

            close_p = bar['close']
            high_p  = bar.get('high', close_p)
            hold_days = sum(1 for td in trading_dates if pos.entry_date <= td <= today)

            # 0. 除权跳空保护（readonly 模式跳过）
            if not readonly and prev_snap:
                prev_bar = prev_snap.get(code)
                if prev_bar:
                    from app.backtest.exit_rules import adjust_for_gap
                    pos.entry_price, pos.peak_price = adjust_for_gap(
                        code, pos.entry_price, pos.peak_price,
                        close_p, prev_bar.get('close', 0)
                    )

            # 更新峰值
            if not readonly and high_p > pos.peak_price:
                pos.peak_price = high_p

            # 构建参数 — 优先从 settings（app_setting.json）读取，config.py 兜底
            # 与 intraday_monitor._check_position 保持完全一致的取值逻辑
            from core.settings import settings as _settings
            def _cfg(key, default):
                val = _settings.get("risk", key)
                if val is None:
                    import app.sim_trader.config as _sc
                    return getattr(_sc, key.upper(), default)
                return val

            sim_params = {
                "hard_stop": _cfg("hard_stop_loss_pct", -6.0) / 100.0,
                "take_profit_tiers": _cfg("take_profit_tiers", [{"profit_pct": 0.03, "sell_ratio": 0.30}]),
                "trail_activate": _cfg("trailing_stop_activate_pct", 5.0) / 100.0,
                "trail_dd": _cfg("trailing_stop_drawdown_pct", 2.0) / 100.0,
                "time_exit_days": _cfg("time_exit_days", 7),
                "time_exit_profit": _cfg("time_exit_min_profit_pct", 3.0) / 100.0,
                "time_force_days": _cfg("time_exit_force_days", 12),
                "first_day_exit_min_profit": _cfg("first_day_exit_min_profit", 0.0),
                "first_day_exit_days": _cfg("first_day_exit_days", 1),
                "use_atr_trail": _cfg("use_atr_stop", False),
                "atr_trail_multiplier": _cfg("atr_stop_multiplier", 1.0),
            }
            ctx = exit_rule_engine.build_context(pos, bar, hold_days, sim_params, use_high_for_tp=True)
            signal = exit_rule_engine.check(ctx)

            if signal:
                # 标记TP档位
                if signal.reason.startswith('TP') and not readonly:
                    idx = int(signal.reason[2]) - 1
                    pos.mark_tier_triggered(idx)

                if signal.sell_ratio < 1.0:
                    ss = int(pos.remaining_shares * signal.sell_ratio / 100) * 100
                    if ss < 100:
                        ss = min(100, int(pos.remaining_shares))
                    sells.append((pos, signal.sell_price, signal.reason, ss))
                else:
                    sells.append((pos, signal.sell_price, signal.reason, None))

        return sells

    def execute_sell(self, pos: Position, exit_price: float, reason: str,
                     partial: Optional[int] = None,
                     exit_date: Optional[date] = None,
                     exit_timing: str = "close") -> Optional[Trade]:
        ss = partial if partial is not None else pos.remaining_shares
        ss = int(ss // 100 * 100)
        if ss <= 0:
            return None

        rp = (exit_price / pos.entry_price - 1) * 100
        profit = ss * (exit_price - pos.entry_price)
        pos.remaining_shares -= ss

        if pos.remaining_shares <= 0:
            pos.is_active = False
            pos.remaining_shares = 0

        self.cash += ss * exit_price
        self._trade_count += 1

        log.info(f"[卖出] {pos.code} 价格={exit_price:.2f} 数量={ss} 收益={rp:.1f}% 利润={profit:.0f} 原因={reason} 剩余现金={self.cash:.0f}")
        _safe_broadcast({"type":"sim_trader_log","action":"sell","code":pos.code,"name":_get_stock_name(pos.code),"price":round(exit_price,2),"shares":ss,"ret_pct":round(rp,1),"profit":round(profit,0),"reason":reason,"cash":round(self.cash,0),"date":str(exit_date or date.today()),"time":datetime.now().strftime('%H:%M:%S')})

        # 连亏计数（盘中/尾盘卖出统一在此更新）
        if rp <= 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
            self.pause_until = None
        if self.consecutive_losses >= LOSS_STREAK_PAUSE:
            self.pause_until = (exit_date or date.today()) + timedelta(days=PAUSE_DAYS)

        # 立即持久化（盘中/尾盘卖出统一保存，防止状态丢失）
        trade = Trade(
            code=pos.code, entry_date=pos.entry_date,
            exit_date=exit_date or date.today(),
            entry_price=pos.entry_price, exit_price=exit_price,
            shares=ss, return_pct=rp, profit_amount=profit,
            exit_reason=reason, hold_days=0,
            entry_reason=pos.strategy_name, exit_timing=exit_timing,
            entry_time=getattr(pos, 'entry_time', '15:00'),
            exit_time=datetime.now().strftime('%H:%M:%S'),
        )

        if self._store:
            self._store.save_trade(trade)
            self._store.save_positions(self.positions)
            self._store.save_state(
                self.cash, self.consecutive_losses,
                self.pause_until, self._trade_count)

        return trade

    def sell_phase(self, today: date, snapshot: dict,
                   trading_dates: List[date]):
        """卖出阶段（先卖后买，回收现金）。仅交易时段内执行。"""
        from datetime import datetime
        now = datetime.now()
        t = now.hour * 100 + now.minute
        if t < 925 or t > 1505:
            log.warning(f"[卖出阶段] 非交易时段({now.strftime('%H:%M')})，跳过执行")
            return

        sells = self.check_stops(today, snapshot, trading_dates,
                                 prev_snap=self._prev_snap)

        for pos, exit_price, reason, partial in sells:
            trade = self.execute_sell(pos, exit_price, reason, partial,
                                      exit_date=today, exit_timing="close")
            if trade:
                trade.hold_days = sum(1 for td in trading_dates
                                      if pos.entry_date <= td <= today)
                # 不再 append:execute_sell 已写 DB;冷启动时 load_trades 一次性加载
                # 这样能避免盘中 sell + 尾盘 sell 时,内存与 DB 不一致
                # 注意:execute_sell 已调用 _store.save_trade(trade),这里不再重复保存

                # 真实券商委托（需 BROKER_ENABLED=True 且 gateway 可用）
                from app.sim_trader.config import BROKER_ENABLED
                if BROKER_ENABLED:
                    try:
                        from core.gateway import get_gateway
                        gw = get_gateway()
                        gw.sell(code=trade.code, price=exit_price,
                                volume=trade.shares, reason=reason)
                        log.info(f"券商委托: {trade.code} 卖出 {trade.shares}股 @ {exit_price:.2f} [{reason}]")
                    except Exception as e:
                        log.error(f"券商委托失败 {trade.code}: {e}")

        if sells:
            log.info(f"[卖出阶段] 共执行 {len(sells)} 笔卖出")
        # 清理已平仓
        self.positions = {k: v for k, v in self.positions.items() if v.is_active}
        # 保存当日快照供次日除权跳空保护
        self._prev_snap = {k: dict(v) for k, v in snapshot.items()}
        if self._store:
            self._store.save_positions(self.positions)
            self._store.save_state(
                self.cash, self.consecutive_losses,
                self.pause_until, self._trade_count)

    # ── 记录 ──────────────────────────────────

    def record(self, today: date, snapshot: dict):
        # #8 修复:增量更新所有持仓的 current_price,让 market_value/profit_pct property 反映实时行情
        # 边界保护: bar['close'] 缺失/停牌(为 0)时,保持原值或 fallback 到 entry_price,避免突然归零
        for code, pos in self.positions.items():
            bar = snapshot.get(code)
            if bar:
                close_raw = bar.get('close')
                close_val = float(close_raw) if close_raw else 0.0
                pos.current_price = close_val if close_val > 0 else (pos.current_price or pos.entry_price)
        eq = self.total_equity(snapshot)
        log.info(f"[快照] 日期={today} 权益={eq:,.0f} 现金={self.cash:,.0f} 持仓={self.position_count}")
        _safe_broadcast({"type":"sim_trader_log","action":"snapshot","date":str(today),"time":datetime.now().strftime('%H:%M:%S'),"equity":round(eq,0),"cash":round(self.cash,0),"positions":self.position_count})
        # 同一天只保留最新一条，防止重复
        entry = {
            'date': today,
            'equity': eq,
            'cash': self.cash,
            'positions': self.position_count,
        }
        if self.equity_curve and str(self.equity_curve[-1].get('date')) == str(today):
            self.equity_curve[-1] = entry
        else:
            self.equity_curve.append(entry)
        if self._store:
            self._store.save_equity_point(today, eq, self.cash, self.position_count)
            self._store.save_state(
                self.cash, self.consecutive_losses,
                self.pause_until, self._trade_count)
