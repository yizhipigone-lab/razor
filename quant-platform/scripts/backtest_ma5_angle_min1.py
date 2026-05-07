#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度策略 — 1分钟线精确回测
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
回测区间: 2025-04-25 ~ 2026-04-29
数据精度: 1分钟K线（逐根检查止损）
初始资金: 100万, 单票上限: 4万

止损检查顺序（每根1分钟K线）:
  1. 硬止损 -9%: 该分钟Low触及止损价 → 立即卖出
  2. 时间强制: 持仓>10个交易日 → 收盘清仓
  3. 分阶段止盈: +5%卖1/3, +10%卖1/2, +15%清仓
  4. 移动止盈: 盈利>5%后峰值回撤3% → 卖出
  5. 保本线: 曾盈利>3%后跌破成本 → 卖出
  6. 时间早期: >7天且盈利>1% → 收盘卖出

执行: 收盘前几分钟确认信号并买入（当日收盘价）
      盘中触发止损立即卖出，资金当日可复用
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pandas as pd
import numpy as np
from datetime import date, timedelta, datetime
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from collections import Counter
import time

from database.duckdb_manager import db
from app.screener.strategies.ma5_angle import generate_signals

# ── 配置 ──────────────────────────────────────────────────────
INITIAL_CAPITAL   = 1_000_000
MAX_PER_STOCK     = 40_000
HARD_STOP         = -0.09
BREAKEVEN_PROFIT  = 0.03
TRAIL_TRIGGER     = 0.05
TRAIL_DISTANCE    = 0.03
TIME_EARLY_DAYS   = 7
TIME_EARLY_PROFIT = 0.01
TIME_MAX_DAYS     = 10
LOSS_STREAK_1     = 3
LOSS_STREAK_2     = 5
PAUSE_DAYS        = 3

BACKTEST_START = date(2025, 4, 25)
BACKTEST_END   = date(2026, 4, 29)
BUFFER_DAYS    = 365
LOAD_START     = BACKTEST_START - timedelta(days=BUFFER_DAYS)

MIN1_DIR = Path(__file__).parent.parent / "data" / "parquet" / "min1"


# ── 数据结构 ──────────────────────────────────────────────────
@dataclass
class Position:
    code: str
    entry_date: date
    entry_price: float
    entry_time: str           # 入场时间 e.g. "15:00"
    shares: int
    cost: float
    peak_price: float
    peak_profit_pct: float = 0.0
    remaining_shares: int = 0
    staged_level: int = 0
    is_active: bool = True

    def __post_init__(self):
        self.peak_price = self.entry_price
        self.remaining_shares = self.shares


@dataclass
class Trade:
    code: str
    entry_date: date
    entry_time: str
    exit_date: date
    exit_time: str
    entry_price: float
    exit_price: float
    shares: int
    return_pct: float
    profit_amount: float
    exit_reason: str
    hold_days: int


# ── 引擎 ──────────────────────────────────────────────────────
class MinuteBacktestEngine:
    def __init__(self, trading_dates: List[date]):
        self.cash = INITIAL_CAPITAL
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict] = []
        self.consecutive_losses = 0
        self.pause_until: Optional[date] = None
        self.trading_dates = trading_dates
        self._date_idx = {d: i for i, d in enumerate(trading_dates)}
        self.sh_index = self._load_sh_index()

    def _load_sh_index(self) -> pd.DataFrame:
        try:
            sh_path = Path(__file__).parent.parent / "data" / "parquet" / "daily" / "index_000001.parquet"
            if sh_path.exists():
                sh = pd.read_parquet(str(sh_path))
                sh['date'] = pd.to_datetime(sh['date']).dt.date
                sh = sh.sort_values('date')
                sh['ma20'] = sh['close'].rolling(20).mean()
                return sh
        except Exception as e:
            from core.logger import get_logger
            get_logger("Backtest").error(f"加载上证指数失败: {e}", exc_info=True)
        return pd.DataFrame()

    def is_bull_market(self, d: date) -> bool:
        if self.sh_index.empty:
            return True
        row = self.sh_index[self.sh_index['date'] == d]
        if row.empty:
            return True
        r = row.iloc[0]
        if pd.isna(r['ma20']):
            return True
        return float(r['close']) >= float(r['ma20'])

    def total_equity(self, prices: Dict[str, float]) -> float:
        pos_value = sum(
            p.remaining_shares * prices.get(p.code, p.entry_price)
            for p in self.positions.values() if p.is_active
        )
        return self.cash + pos_value

    def position_count(self) -> int:
        return len([p for p in self.positions.values() if p.is_active])

    def max_buy_amount(self) -> float:
        if self.consecutive_losses >= LOSS_STREAK_1:
            return MAX_PER_STOCK / 2
        return MAX_PER_STOCK

    def _trading_days_between(self, d1: date, d2: date) -> int:
        return sum(1 for td in self.trading_dates if d1 <= td <= d2)

    def check_intraday_stop(self, pos: Position, bar: dict, d: date) -> Optional[Tuple]:
        """
        根据单根1分钟K线检查止损。
        bar: {'time': str, 'open', 'high', 'low', 'close'}
        返回: (exit_price, reason, partial_shares) 或 None
        """
        high   = float(bar['high'])
        low    = float(bar['low'])
        close  = float(bar['close'])
        minute = bar['time_str']

        # 更新峰值（用这根K线的高点）
        if high > pos.peak_price:
            pos.peak_price = high
        pos.peak_profit_pct = pos.peak_price / pos.entry_price - 1

        current_profit = close / pos.entry_price - 1
        hold_days = self._trading_days_between(pos.entry_date, d)
        remaining = pos.remaining_shares

        # 1. 硬止损: 这根K线的Low是否触及止损价
        hard_stop_price = pos.entry_price * (1 + HARD_STOP)
        if low <= hard_stop_price:
            exit_px = hard_stop_price
            return (exit_px, f"硬止损({(exit_px/pos.entry_price-1)*100:.1f}%)", None)

        # 2. 时间强制 (>10交易日, 仅收盘检查)
        # 在日内不触发，留到收盘处理

        # 3. 分阶段止盈
        if remaining > 0:
            if pos.staged_level < 3 and current_profit >= 0.15:
                return (close, f"阶段止盈15%(+{current_profit*100:.1f}%)", None)
            if pos.staged_level < 2 and current_profit >= 0.10:
                sell_shares = int(remaining // 2 // 100 * 100)
                if sell_shares >= 100:
                    return (close, f"阶段止盈10%(+{current_profit*100:.1f}%)", sell_shares)
            if pos.staged_level < 1 and current_profit >= 0.05:
                sell_shares = int(remaining // 3 // 100 * 100)
                if sell_shares >= 100:
                    return (close, f"阶段止盈5%(+{current_profit*100:.1f}%)", sell_shares)

        # 4. 移动止盈
        if pos.peak_profit_pct >= TRAIL_TRIGGER:
            # 用这根K线的Low检查是否触发回撤
            dd_from_peak_low = low / pos.peak_price - 1
            if dd_from_peak_low <= -TRAIL_DISTANCE:
                trail_price = pos.peak_price * (1 - TRAIL_DISTANCE)
                exit_px = min(trail_price, close)  # 取更保守的
                return (exit_px,
                    f"移动止盈(峰{pos.peak_profit_pct*100:.1f}%回{dd_from_peak_low*100:.1f}%)", None)
            # 也用close检查
            dd_from_peak_close = close / pos.peak_price - 1
            if dd_from_peak_close <= -TRAIL_DISTANCE:
                trail_price = pos.peak_price * (1 - TRAIL_DISTANCE)
                return (trail_price,
                    f"移动止盈(峰{pos.peak_profit_pct*100:.1f}%回{dd_from_peak_close*100:.1f}%)", None)

        # 5. 保本线: Low触及成本价
        if pos.peak_profit_pct >= BREAKEVEN_PROFIT and low <= pos.entry_price:
            return (pos.entry_price, f"保本(曾+{pos.peak_profit_pct*100:.1f}%)", None)

        return None

    def check_eod_stops(self, pos: Position, close: float, d: date) -> Optional[Tuple]:
        """收盘时额外检查（时间止损等）"""
        current_profit = close / pos.entry_price - 1
        hold_days = self._trading_days_between(pos.entry_date, d)

        # 时间强制 >10天
        if hold_days > TIME_MAX_DAYS:
            return (close, f"时间强制({hold_days}天)", None)

        # 时间早期 >7天且盈利>1%
        if hold_days > TIME_EARLY_DAYS and current_profit > TIME_EARLY_PROFIT:
            return (close, f"时间早期({hold_days}天+{current_profit*100:.1f}%)", None)

        return None

    def execute_sell(self, pos: Position, exit_price: float, reason: str,
                     partial: Optional[int] = None, exit_date: date = None,
                     exit_time: str = "") -> Trade:
        sell_shares = partial if partial else pos.remaining_shares
        sell_shares = int(sell_shares // 100 * 100)
        if sell_shares <= 0:
            sell_shares = pos.remaining_shares

        profit_amount = sell_shares * (exit_price - pos.entry_price)
        return_pct = (exit_price / pos.entry_price - 1) * 100

        pos.remaining_shares -= sell_shares
        if return_pct >= 15:       pos.staged_level = 3
        elif return_pct >= 10:     pos.staged_level = max(pos.staged_level, 2)
        elif return_pct >= 5:      pos.staged_level = max(pos.staged_level, 1)

        if pos.remaining_shares <= 0:
            pos.is_active = False
            pos.remaining_shares = 0

        self.cash += sell_shares * exit_price

        return Trade(
            code=pos.code, entry_date=pos.entry_date, entry_time=pos.entry_time,
            exit_date=exit_date or date.today(), exit_time=exit_time,
            entry_price=pos.entry_price, exit_price=exit_price,
            shares=sell_shares, return_pct=return_pct,
            profit_amount=profit_amount, exit_reason=reason, hold_days=0,
        )

    def record_equity(self, d: date, prices: Dict[str, float]):
        equity = self.total_equity(prices)
        self.equity_curve.append({
            'date': d, 'equity': equity, 'cash': self.cash,
            'positions': self.position_count(),
        })

    def execute_buy(self, d: date, code: str, price: float, time_str: str = "15:00") -> Optional[Position]:
        if code in self.positions:
            return None
        max_amt = min(self.max_buy_amount(), self.cash)
        if max_amt < 5000:
            return None
        shares = int(max_amt / price / 100) * 100
        if shares < 100:
            return None
        cost = shares * price
        if cost > self.cash:
            return None

        pos = Position(code=code, entry_date=d, entry_price=price, entry_time=time_str,
                       shares=shares, cost=cost, peak_price=price, remaining_shares=shares)
        self.cash -= cost
        self.positions[code] = pos
        return pos


# ── 1分钟数据加载器 ──────────────────────────────────────────
def load_min1_for_codes(codes: List[str], d: date) -> pd.DataFrame:
    """加载指定股票在某一天的1分钟K线"""
    if not codes:
        return pd.DataFrame()
    files = [str(MIN1_DIR / f"{c}.parquet") for c in codes if (MIN1_DIR / f"{c}.parquet").exists()]
    if not files:
        return pd.DataFrame()

    day_str = d.isoformat()
    next_day = (d + timedelta(days=1)).isoformat()

    try:
        # 用 DuckDB 直接查询
        file_list = str(files).replace("'", "")
        sql = f"""
            SELECT filename, *
            FROM read_parquet({files}, filename=true, union_by_name=True)
            WHERE datetime >= '{day_str}' AND datetime < '{next_day}'
            ORDER BY filename, datetime
        """
        df = db.conn.execute(sql).df()
        if not df.empty:
            df["code"] = df["filename"].str.extract(r"([^\\/]+)\.parquet$")
            df = df.drop(columns=["filename"])
        return df
    except Exception as e:
        return pd.DataFrame()


# ── 主流程 ────────────────────────────────────────────────────
def run_backtest():
    t0 = time.time()
    print("=" * 70)
    print("  MA5 角度策略 — 1分钟线精确回测")
    print(f"  区间: {BACKTEST_START} ~ {BACKTEST_END}")
    print(f"  资金: {INITIAL_CAPITAL:,}  单票上限: {MAX_PER_STOCK:,}")
    print("=" * 70)

    # ── 1. 加载日线 + 生成信号 ──────────────────────────────
    print(f"\n[1/4] 加载日线 + 生成信号 ...")
    bars = db.load_all_bars(freq="daily", start=LOAD_START, end=BACKTEST_END)
    bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
    for c in ["open", "high", "low", "close", "volume"]:
        if c in bars:
            bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=["close"])
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    bars = bars.sort_values(["code", "date"])

    sig = generate_signals(bars,
        version="improved", rps_threshold=0, use_ma_align=True, use_adx=True,
        adx_threshold=20, sh_index_filter=True, vol_threshold=2.0,
        close_position_threshold=0.8, breadth_threshold=0)
    sig = sig[(sig["date"] >= BACKTEST_START) & (sig["date"] <= BACKTEST_END)].copy()
    sig["date"] = pd.to_datetime(sig["date"]).dt.date
    sig = sig.sort_values(["date", "code"])
    print(f"  日线: {bars['code'].nunique():,}只, 信号: {len(sig):,}")

    # 信号按日索引
    sig_by_date: Dict[date, List[str]] = {}
    for _, r in sig.iterrows():
        sig_by_date.setdefault(r['date'], []).append(r['code'])

    # ── 2. 获取交易日列表 ──────────────────────────────────
    print(f"\n[2/4] 扫描交易日 ...")
    # 从日线数据中获取交易日
    bt_bars = bars[(bars["date"] >= BACKTEST_START) & (bars["date"] <= BACKTEST_END)]
    trading_dates = sorted(bt_bars["date"].unique())
    print(f"  交易日: {len(trading_dates):,}")

    # 每日收盘价快照（用于净值计算）
    daily_closes = {}
    for d, grp in bt_bars.groupby("date"):
        daily_closes[d] = dict(zip(grp['code'], grp['close']))

    # ── 3. 初始化引擎 ──────────────────────────────────────
    engine = MinuteBacktestEngine(trading_dates)

    # ── 4. 逐日回测 ────────────────────────────────────────
    print(f"\n[3/4] 逐分钟回测 ({len(trading_dates)} 天) ...")
    total_bars_processed = 0
    intraday_exits = 0
    eod_exits = 0

    for day_idx, d in enumerate(trading_dates):
        # ── 需要加载1分钟数据的股票 ──────────────────────
        held_codes = [p.code for p in engine.positions.values() if p.is_active]
        signal_codes = sig_by_date.get(d, [])
        needed_codes = list(set(held_codes + signal_codes))

        # ── 加载当日1分钟数据 ────────────────────────────
        min1_df = load_min1_for_codes(needed_codes, d)

        if min1_df.empty and not held_codes and not signal_codes:
            # 没有持仓也没有信号，记录净值跳过
            closes_snap = daily_closes.get(d, {})
            engine.record_equity(d, closes_snap)
            continue

        # ── 按股票分组，按时间排序 ────────────────────────
        if not min1_df.empty:
            min1_df['datetime'] = pd.to_datetime(min1_df['datetime'])
            min1_df = min1_df.sort_values(['code', 'datetime'])
            min1_df['time_str'] = min1_df['datetime'].dt.strftime('%H:%M')

            # 先处理收盘（最后几分钟确认信号买入）
            # 找出每只股票的最后一根K线（收盘价）
            last_bars = min1_df.groupby('code').last()
            eod_prices = dict(zip(last_bars.index, last_bars['close']))
            eod_times = dict(zip(last_bars.index, last_bars['time_str']))
        else:
            eod_prices = {}
            eod_times = {}

        # ── 逐分钟处理 ──────────────────────────────────
        # 对每个持仓股票，逐根K线检查
        for code, pos in list(engine.positions.items()):
            if not pos.is_active:
                continue

            stock_bars = min1_df[min1_df['code'] == code] if not min1_df.empty else pd.DataFrame()
            if stock_bars.empty:
                # 没有日内数据，用收盘价做 EOD 检查
                eod_close = eod_prices.get(code)
                if eod_close is not None and eod_close > 0:
                    result = engine.check_eod_stops(pos, float(eod_close), d)
                    if result:
                        exit_price, reason = result
                        trade = engine.execute_sell(pos, exit_price, reason, exit_date=d, exit_time="15:00")
                        trade.hold_days = engine._trading_days_between(pos.entry_date, d)
                        engine.trades.append(trade)
                        eod_exits += 1
                        if trade.return_pct <= 0:
                            engine.consecutive_losses += 1
                        else:
                            engine.consecutive_losses = 0
                            engine.pause_until = None
                continue

            # 逐根K线检查
            exited = False
            for _, bar in stock_bars.iterrows():
                if exited:
                    break
                total_bars_processed += 1

                result = engine.check_intraday_stop(pos, bar.to_dict(), d)
                if result:
                    exit_price, reason, partial = result
                    trade = engine.execute_sell(pos, exit_price, reason, partial,
                                                  exit_date=d, exit_time=bar['time_str'])
                    trade.hold_days = engine._trading_days_between(pos.entry_date, d)
                    engine.trades.append(trade)
                    intraday_exits += 1
                    exited = True

                    if trade.return_pct <= 0:
                        engine.consecutive_losses += 1
                    else:
                        engine.consecutive_losses = 0
                        engine.pause_until = None

                    if engine.consecutive_losses >= LOSS_STREAK_2:
                        engine.pause_until = d + timedelta(days=PAUSE_DAYS)
                    continue

            # 如果日内没有退出，收盘检查
            if not exited:
                eod_close = eod_prices.get(code)
                if eod_close is not None and eod_close > 0:
                    result = engine.check_eod_stops(pos, float(eod_close), d)
                    if result:
                        exit_price, reason = result
                        trade = engine.execute_sell(pos, exit_price, reason, exit_date=d, exit_time="15:00")
                        trade.hold_days = engine._trading_days_between(pos.entry_date, d)
                        engine.trades.append(trade)
                        eod_exits += 1
                        if trade.return_pct <= 0:
                            engine.consecutive_losses += 1
                        else:
                            engine.consecutive_losses = 0
                            engine.pause_until = None

        # 清理已平仓的持仓
        engine.positions = {k: v for k, v in engine.positions.items() if v.is_active}

        # ── 收盘买入新信号 ──────────────────────────────
        if d in sig_by_date:
            paused = engine.pause_until is not None and d <= engine.pause_until
            bull = engine.is_bull_market(d)
            if not paused and bull:
                for code in sig_by_date[d]:
                    price = eod_prices.get(code) if eod_prices else None
                    if price is None or price <= 0:
                        continue
                    price = float(price)
                    # 20天无重复
                    if any(t.code == code and t.entry_date >= d - timedelta(days=20)
                           for t in engine.trades):
                        continue
                    engine.execute_buy(d, code, price, eod_times.get(code, "15:00"))

        # ── 记录净值 ────────────────────────────────────
        # 构建当日收盘价字典（含所有股票）
        all_closes = eod_prices if eod_prices else {}
        # 补全非当日关注的持仓股票的收盘价（从日线快照）
        full_closes = daily_closes.get(d, {})
        for code, pos in engine.positions.items():
            if pos.is_active and code not in all_closes:
                all_closes[code] = full_closes.get(code, pos.entry_price)

        engine.record_equity(d, all_closes)

        if (day_idx + 1) % 50 == 0:
            eq = engine.total_equity(all_closes)
            print(f"  {d} | {day_idx+1}/{len(trading_dates)} | "
                  f"净值 {eq:,.0f} | 持仓 {engine.position_count()} | "
                  f"盘中退出 {intraday_exits} | 收盘退出 {eod_exits}")

    elapsed = time.time() - t0
    print(f"\n  处理 {total_bars_processed:,} 根1分钟K线, 耗时 {elapsed:.0f}s")

    # ── 5. 统计 ──────────────────────────────────────────────
    print("\n[4/4] 生成报告 ...")
    eq_df = pd.DataFrame(engine.equity_curve)
    final_eq = eq_df['equity'].iloc[-1]
    total_ret = (final_eq / INITIAL_CAPITAL - 1) * 100
    eq_df['cummax'] = eq_df['equity'].cummax()
    eq_df['dd'] = (eq_df['equity'] - eq_df['cummax']) / eq_df['cummax'] * 100
    max_dd = eq_df['dd'].min()

    # 月度
    eq_df['month'] = pd.to_datetime(eq_df['date']).dt.to_period('M')
    m_agg = eq_df.groupby('month').agg(start=('equity','first'), end=('equity','last'), dd=('dd','min'))
    m_agg['ret'] = (m_agg['end'] / m_agg['start'] - 1) * 100

    if engine.trades:
        mc = Counter(pd.Timestamp(t.entry_date).to_period('M') for t in engine.trades)
        m_agg['trades'] = [mc.get(m, 0) for m in m_agg.index]
        # 买入次数（按entry去重）
        entries = {}
        for t in engine.trades:
            key = (t.code, t.entry_date)
            if key not in entries:
                entries[key] = t
        m_entry = Counter(pd.Timestamp(e.entry_date).to_period('M') for e in entries.values())
        m_agg['entries'] = [m_entry.get(m, 0) for m in m_agg.index]

    # 交易统计
    trades = engine.trades
    if trades:
        wins  = [t for t in trades if t.return_pct > 0]
        loses = [t for t in trades if t.return_pct <= 0]
        n_total = len(trades)
        n_win = len(wins)
        n_loss = len(loses)
        wr = n_win / n_total * 100 if n_total else 0
        avg_w = np.mean([t.return_pct for t in wins]) if wins else 0
        avg_l = np.mean([t.return_pct for t in loses]) if loses else 0
        avg_t = np.mean([t.return_pct for t in trades])
        tg = sum(t.return_pct for t in wins)
        tl = abs(sum(t.return_pct for t in loses))
        pf = tg / tl if tl > 0 else float('inf')
        total_profit = sum(t.profit_amount for t in trades)
        avg_hold = np.mean([t.hold_days for t in trades])
        exit_dist = Counter(t.exit_reason.split('(')[0] for t in trades)
        # 日内 vs 收盘
        intraday_count = sum(1 for t in trades if t.exit_time != "15:00")
        eod_count = sum(1 for t in trades if t.exit_time == "15:00")
    else:
        n_total = n_win = n_loss = 0
        wr = avg_w = avg_l = avg_t = pf = avg_hold = total_profit = 0
        exit_dist = Counter()
        intraday_count = eod_count = 0

    monthly_trades = [m_agg['trades'].iloc[i] for i in range(len(m_agg))] if len(m_agg) > 0 else []
    avg_mt = np.mean(monthly_trades) if monthly_trades else 0
    std_mt = np.std(monthly_trades) if monthly_trades else 0
    cv = std_mt / avg_mt if avg_mt > 0 else 999

    # ── 输出报告 ──────────────────────────────────────────────
    print("\n" + "═" * 70)
    print("  📊 回测报告 — 1分钟线精确模拟")
    print("═" * 70)

    print(f"\n  ┌─────────────────────────────────────────┐")
    print(f"  │ 初始资金:  {INITIAL_CAPITAL:>13,}                │")
    print(f"  │ 最终净值:  {final_eq:>13,.0f}                │")
    print(f"  │ 总收益率:  {total_ret:>+12.2f}%               │")
    print(f"  │ 最大回撤:  {max_dd:>12.2f}%               │")
    print(f"  │ Profit Factor:  {pf:>10.2f}                 │")
    print(f"  │ 总盈利额:  {total_profit:>+13,.0f}                │")
    print(f"  └─────────────────────────────────────────┘")

    print(f"\n  ┌─────────────────────────────────────────┐")
    print(f"  │ 交易统计                                  │")
    print(f"  │ 总成交: {n_total:>6}笔 (含分阶段止盈拆分)        │")
    print(f"  │ 盈利: {n_win:>6}笔 / 亏损: {n_loss:<6}笔            │")
    print(f"  │ 胜率: {wr:>7.1f}%                            │")
    print(f"  │ 均盈: {avg_w:>+7.2f}%    均亏: {avg_l:>+7.2f}%        │")
    print(f"  │ 均笔: {avg_t:>+7.2f}%    均持: {avg_hold:>6.1f}天        │")
    print(f"  │ 盘中退出: {intraday_count:>5}笔  收盘退出: {eod_count:<5}笔       │")
    print(f"  └─────────────────────────────────────────┘")

    print(f"\n  ┌─────────────────────────────────────────┐")
    print(f"  │ 月度均衡性                                │")
    print(f"  │ 月均交易: {avg_mt:>7.1f}笔  标准差: {std_mt:>7.1f}         │")
    print(f"  │ 变异系数(CV): {cv:>7.2f}  (<0.5为均衡)          │")
    print(f"  └─────────────────────────────────────────┘")

    print(f"\n  [止损分布] (共{n_total}笔)")
    print(f"  {'原因':<32} {'笔数':>6} {'占比':>8}")
    print(f"  {'-'*48}")
    for reason, count in exit_dist.most_common():
        print(f"  {reason:<32} {count:>6} {count/n_total*100:>7.1f}%")

    print(f"\n  [月度表现]")
    print(f"  {'月份':<10} {'收益%':>8} {'回撤%':>8} {'买入':>6} {'成交':>6}")
    print(f"  {'-'*42}")
    for idx, row in m_agg.iterrows():
        print(f"  {str(idx):<10} {row['ret']:>+7.2f} {row['dd']:>7.2f} "
              f"{int(row.get('entries', 0)):>6} {int(row.get('trades', 0)):>6}")

    # ── 保存 ──────────────────────────────────────────────────
    out = Path(__file__).parent.parent / "output"
    out.mkdir(exist_ok=True)
    eq_df.to_parquet(str(out / "backtest_min1_equity.parquet"), index=False)
    if trades:
        pd.DataFrame([{
            'code':t.code, 'entry_date':str(t.entry_date), 'entry_time':t.entry_time,
            'exit_date':str(t.exit_date), 'exit_time':t.exit_time,
            'entry_px':t.entry_price, 'exit_px':t.exit_price, 'shares':t.shares,
            'ret_pct':t.return_pct, 'profit':t.profit_amount,
            'reason':t.exit_reason, 'hold_days':t.hold_days,
        } for t in trades]).to_parquet(str(out / "backtest_min1_trades.parquet"), index=False)

    print(f"\n  净值 → output/backtest_min1_equity.parquet")
    print(f"  交易 → output/backtest_min1_trades.parquet")
    print(f"  总耗时: {elapsed:.0f}s")

    return eq_df, trades, m_agg


if __name__ == "__main__":
    run_backtest()
