#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度策略 �?精确版回测（用日线OHLC的Low/High做日内模拟）
相比纯日线Close版本，这个版本：
- 硬止损：用当日最低价检查，触发则按止损价退出（而非收盘价）
- 移动止盈：用当日最高价更新峰值，最低价检查回�?- 分阶段止盈：用当日最高价检查是否触�?- 这比纯Close版本更精确地模拟了日内执�?"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pandas as pd
import numpy as np
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from collections import Counter
import time
import warnings
warnings.filterwarnings('ignore')

from database.duckdb_manager import db, PARQUET_DAILY_DIR
from app.screener.strategies.ma5_angle import generate_signals

# ══════════════════════════════════════════════════════════════�?INITIAL_CAPITAL = 1_000_000
POSITION_CAP    = 50_000
HARD_STOP       = -0.055
TP1_PCT         = 0.04
TP1_RATIO       = 0.20
TP2_PCT         = 0.14
TRAIL_ACTIVATE  = 0.08
TRAIL_DD        = 0.02
TIME_EXIT       = 7
TIME_FORCE      = 10
LOSS_S1         = 3
LOSS_S2         = 5
PAUSE_D         = 3
MIN_BUY         = 5000

BACKTEST_START = date(2022, 1, 4)
BACKTEST_END   = date(2026, 5, 2)
BUFFER_DAYS    = 365
LOAD_START     = date(2022, 1, 1)

SIGNAL_PARAMS = {
    "version": "improved", "filter_st": True, "filter_bj": True,
    "vol_threshold": 1.5, "close_position_threshold": 0.8,
    "disable_quality_sort": True,
    "filter_consecutive_up": False, "filter_gap_quality": False,
}
# ══════════════════════════════════════════════════════════════�?

@dataclass
class Position:
    code: str
    entry_date: date
    entry_price: float
    shares: int
    cost: float
    peak_price: float = 0.0
    peak_profit_pct: float = 0.0
    remaining_shares: int = 0
    tp1_triggered: bool = False
    tp2_triggered: bool = False
    is_active: bool = True

    def __post_init__(self):
        self.peak_price = self.entry_price
        self.remaining_shares = self.shares


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


class PreciseBacktestEngine:
    """
    精确版回测引擎：
    - 卖出用当�?OHLC 做精确检�?      - 硬止损：检�?Low 是否触及止损价，如触及则按止损价退�?      - 止盈：检�?High 是否触及止盈�?      - 移动止盈：High更新峰值，Low检查回�?    - 买入用当日收盘价
    """

    def __init__(self, trading_dates, sh_index):
        self.cash = INITIAL_CAPITAL
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict] = []
        self.consecutive_losses = 0
        self.pause_until = None
        self.trading_dates = trading_dates
        self.sh_index = sh_index

    def total_equity(self, prices):
        pv = sum(p.remaining_shares * prices.get(p.code, p.entry_price)
                 for p in self.positions.values() if p.is_active)
        return self.cash + pv

    def pos_count(self):
        return len([p for p in self.positions.values() if p.is_active])

    def max_pos_size(self):
        return POSITION_CAP / 2 if self.consecutive_losses >= LOSS_S1 else POSITION_CAP

    def _td(self, d1, d2):
        return len([td for td in self.trading_dates if d1 <= td <= d2])

    def check_stops(self, d: date, daily_bars: dict) -> List[Tuple]:
        """
        daily_bars: {code: {'open','high','low','close'}}
        按优先级检查，一次只触发一个退出（最高优先级�?        """
        sells = []
        for code, pos in list(self.positions.items()):
            if not pos.is_active or pos.remaining_shares <= 0:
                continue

            bar = daily_bars.get(code)
            if bar is None:
                continue

            open_p  = bar['open']
            high_p  = bar['high']
            low_p   = bar['low']
            close_p = bar['close']

            # 更新日内峰值（用当日最高价�?            if high_p > pos.peak_price:
                pos.peak_price = high_p
            pos.peak_profit_pct = pos.peak_price / pos.entry_price - 1

            hold_days = self._td(pos.entry_date, d)
            rem = pos.remaining_shares

            # ── 1. 硬止�?-5.5%（用当日最低价检查） ──
            hard_stop_price = pos.entry_price * (1 + HARD_STOP)
            if low_p <= hard_stop_price:
                # 按止损价退出（精确执行），而非收盘�?                sells.append((pos, hard_stop_price,
                    f"硬止�?{HARD_STOP*100:.1f}%)", None))
                continue

            # ── 2. 时间强制退�?>10�?──
            if hold_days > TIME_FORCE:
                sells.append((pos, close_p, f"时间强制({hold_days}�?", None))
                continue

            # ── 3. TP2: +14% 清仓（用最高价检查） ──
            if not pos.tp2_triggered:
                tp2_price = pos.entry_price * (1 + TP2_PCT)
                if high_p >= tp2_price:
                    sells.append((pos, tp2_price, f"TP2 +{TP2_PCT*100:.0f}%", None))
                    continue

            # ── 4. TP1: +4% �?0%（用最高价检查） ──
            if not pos.tp1_triggered:
                tp1_price = pos.entry_price * (1 + TP1_PCT)
                if high_p >= tp1_price:
                    ss = int(rem * TP1_RATIO / 100) * 100
                    if ss >= 100:
                        sells.append((pos, tp1_price, f"TP1 +{TP1_PCT*100:.0f}%", ss))
                        continue

            # ── 5. 移动止盈（最高价更新峰值，最低价检查回撤） ──
            if pos.peak_profit_pct >= TRAIL_ACTIVATE:
                dd_from_peak = low_p / pos.peak_price - 1
                if dd_from_peak <= -TRAIL_DD:
                    trail_price = pos.peak_price * (1 - TRAIL_DD)
                    sells.append((pos, trail_price,
                        f"移动止盈(峰{pos.peak_profit_pct*100:.1f}%回{dd_from_peak*100:.1f}%)", None))
                    continue

            # ── 6. 保本线（最低价触及成本�?已盈�?3%�?──
            if pos.peak_profit_pct >= 0.03 and low_p <= pos.entry_price:
                sells.append((pos, pos.entry_price,
                    f"保本(�?{pos.peak_profit_pct*100:.1f}%)", None))
                continue

            # ── 7. 时间条件 >7天且盈利>1% ──
            current_profit_for_time = close_p / pos.entry_price - 1
            if hold_days > TIME_EXIT and current_profit_for_time > 0.01:
                sells.append((pos, close_p,
                    f"时间条件({hold_days}�?{current_profit_for_time*100:.1f}%)", None))
                continue

        return sells

    def execute_sell(self, pos: Position, exit_price: float, reason: str,
                     partial: Optional[int] = None,
                     exit_date: Optional[date] = None) -> Optional[Trade]:
        ss = partial if partial is not None else pos.remaining_shares
        ss = int(ss // 100 * 100)
        if ss <= 0:
            return None

        rp = (exit_price / pos.entry_price - 1) * 100
        profit = ss * (exit_price - pos.entry_price)
        pos.remaining_shares -= ss

        if "TP2" in reason:
            pos.tp2_triggered = True
        if "TP1" in reason:
            pos.tp1_triggered = True

        if pos.remaining_shares <= 0:
            pos.is_active = False
            pos.remaining_shares = 0

        self.cash += ss * exit_price

        return Trade(
            code=pos.code, entry_date=pos.entry_date,
            exit_date=exit_date or date.today(),
            entry_price=pos.entry_price, exit_price=exit_price,
            shares=ss, return_pct=rp, profit_amount=profit,
            exit_reason=reason, hold_days=0,
        )

    def execute_buy(self, d: date, code: str, price: float) -> Optional[Position]:
        if code in self.positions:
            return None
        ma = min(self.max_pos_size(), self.cash)
        if ma < MIN_BUY:
            return None
        shares = int(ma / price / 100) * 100
        if shares < 100:
            return None
        cost = shares * price
        if cost > self.cash:
            return None
        pos = Position(code, d, price, shares, cost)
        self.cash -= cost
        self.positions[code] = pos
        return pos

    def record_equity(self, d, prices):
        eq = self.total_equity(prices)
        self.equity_curve.append({
            'date': d, 'equity': eq, 'cash': self.cash,
            'positions': self.pos_count(),
        })

    def is_bull_market(self, d):
        if self.sh_index.empty:
            return True
        row = self.sh_index[self.sh_index['date'] == d]
        if row.empty:
            return True
        r = row.iloc[0]
        ma20 = r.get('ma20')
        if pd.isna(ma20):
            return True
        return float(r['close']) >= float(ma20)


def load_sh_index():
    sh_path = PARQUET_DAILY_DIR / "index_000001.parquet"
    if not sh_path.exists():
        return pd.DataFrame()
    sh = pd.read_parquet(str(sh_path))
    sh['date'] = pd.to_datetime(sh['date']).dt.date
    sh = sh.sort_values('date')
    sh['ma20'] = sh['close'].rolling(20).mean()
    return sh


def run_precise_backtest():
    t0 = time.time()
    print("=" * 72)
    print("  MA5 角度策略 �?OHLC精确版回�?(用日线Low/High模拟日内)")
    print(f"  区间: {BACKTEST_START} ~ {BACKTEST_END}")
    print(f"  硬止�? {HARD_STOP*100:+.1f}% (按日Low检查，止损价退�?")
    print("=" * 72)

    # ── 1. 加载数据 ──────────────────────────────────
    print(f"\n[1/4] 加载日线数据 ...")
    bars = db.load_all_bars(freq="daily", start=LOAD_START, end=BACKTEST_END)
    bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
    for c in ["open", "high", "low", "close", "volume"]:
        if c in bars:
            bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=["close"])
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    bars = bars.sort_values(["code", "date"])
    print(f"  {bars['code'].nunique():,} 只股�? {len(bars):,} �?)

    # ── 2. 信号 ──────────────────────────────────────
    print(f"\n[2/4] 生成信号 ...")
    sig = generate_signals(bars, **SIGNAL_PARAMS)
    sig = sig[(sig["date"] >= BACKTEST_START) & (sig["date"] <= BACKTEST_END)].copy()
    sig["date"] = pd.to_datetime(sig["date"]).dt.date
    sig = sig.sort_values(["date", "code"])
    print(f"  信号: {len(sig):,}")

    # ── 3. 构建OHLC快照 ─────────────────────────────
    print(f"\n[3/4] 构建OHLC快照 ...")
    bt_bars = bars[(bars["date"] >= BACKTEST_START) & (bars["date"] <= BACKTEST_END)]

    # 每日每只股票的完整OHLC和收盘价
    daily_ohlc: Dict[date, Dict[str, dict]] = {}
    daily_closes: Dict[date, Dict[str, float]] = {}

    for d, g in bt_bars.groupby("date"):
        d_dict = {}
        c_dict = {}
        for _, r in g.iterrows():
            d_dict[r['code']] = {
                'open': float(r['open']),
                'high': float(r['high']),
                'low': float(r['low']),
                'close': float(r['close']),
            }
            c_dict[r['code']] = float(r['close'])
        daily_ohlc[d] = d_dict
        daily_closes[d] = c_dict

    trading_dates = sorted(daily_ohlc.keys())
    print(f"  交易�? {len(trading_dates):,}")

    # 信号索引
    sig_by_date: Dict[date, List[str]] = {}
    for _, r in sig.iterrows():
        sig_by_date.setdefault(r['date'], []).append(r['code'])

    sh_idx = load_sh_index()

    # ── 4. 回测 ──────────────────────────────────────
    print(f"\n[4/4] 运行精确回测 ...")
    engine = PreciseBacktestEngine(trading_dates, sh_idx)
    skipped_signals = 0

    for i, d in enumerate(trading_dates):
        ohlc = daily_ohlc[d]
        closes = daily_closes[d]

        # Step 1: 卖出（用OHLC做精确检查）
        for pos, exit_price, reason, partial in engine.check_stops(d, ohlc):
            trade = engine.execute_sell(pos, exit_price, reason, partial, exit_date=d)
            if trade:
                trade.hold_days = engine._td(pos.entry_date, d)
                engine.trades.append(trade)
                if trade.return_pct <= 0:
                    engine.consecutive_losses += 1
                else:
                    engine.consecutive_losses = 0
                    engine.pause_until = None
                if engine.consecutive_losses >= LOSS_S2:
                    engine.pause_until = d + timedelta(days=PAUSE_D)

        # 清理
        engine.positions = {k: v for k, v in engine.positions.items() if v.is_active}

        # Step 2: 买入
        if d in sig_by_date:
            paused = engine.pause_until and d <= engine.pause_until
            bull = engine.is_bull_market(d)
            if not paused and bull:
                for code in sig_by_date[d]:
                    price = closes.get(code)
                    if price is None or price <= 0:
                        continue
                    if any(t.code == code and d - t.entry_date <= timedelta(days=20)
                           for t in engine.trades):
                        continue
                    result = engine.execute_buy(d, code, price)
                    if result is None:
                        skipped_signals += 1

        # Step 3: 记录
        engine.record_equity(d, closes)

        if (i + 1) % 100 == 0:
            eq = engine.total_equity(closes)
            print(f"  {d} | {i+1}/{len(trading_dates)} | "
                  f"净�?{eq:,.0f} | 持仓 {engine.pos_count()}")

    # ── 报告 ──────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  精确版回测报�?)
    print(f"{'='*72}")

    eq_df = pd.DataFrame(engine.equity_curve)
    fe = eq_df['equity'].iloc[-1]
    tr = (fe / INITIAL_CAPITAL - 1) * 100
    eq_df['cummax'] = eq_df['equity'].cummax()
    eq_df['dd'] = (eq_df['equity'] - eq_df['cummax']) / eq_df['cummax'] * 100
    md = eq_df['dd'].min()
    days = (BACKTEST_END - BACKTEST_START).days
    ann = ((fe / INITIAL_CAPITAL) ** (365.25 / days) - 1) * 100
    eq_df['daily_ret'] = eq_df['equity'].pct_change()
    sharpe = eq_df['daily_ret'].mean() / eq_df['daily_ret'].std() * np.sqrt(252) if eq_df['daily_ret'].std() > 0 else 0

    # 年度
    eq_df['year'] = pd.to_datetime(eq_df['date']).dt.year
    y_agg = eq_df.groupby('year').agg(start=('equity','first'), end=('equity','last'), dd=('dd','min'))
    y_agg['ret'] = (y_agg['end'] / y_agg['start'] - 1) * 100

    # 月度
    eq_df['month'] = pd.to_datetime(eq_df['date']).dt.to_period('M')
    m_agg = eq_df.groupby('month').agg(start=('equity','first'), end=('equity','last'), dd=('dd','min'))
    m_agg['ret'] = (m_agg['end'] / m_agg['start'] - 1) * 100

    trades = engine.trades
    if trades:
        wins = [t for t in trades if t.return_pct > 0]
        loses = [t for t in trades if t.return_pct <= 0]
        n = len(trades)
        nw = len(wins)
        nl = len(loses)
        wr = nw / n * 100
        aw = np.mean([t.return_pct for t in wins]) if wins else 0
        al = np.mean([t.return_pct for t in loses]) if loses else 0
        at_ = np.mean([t.return_pct for t in trades])
        med = np.median([t.return_pct for t in trades])
        tg_ = sum(t.return_pct for t in wins)
        tl_ = abs(sum(t.return_pct for t in loses))
        pf = tg_ / tl_ if tl_ > 0 else float('inf')
        tp = sum(t.profit_amount for t in trades)
        ah = np.mean([t.hold_days for t in trades])
        ed = Counter(t.exit_reason.split('(')[0] for t in trades)

        # 统计硬止损的实际亏损
        hard_stop_trades = [t for t in trades if t.exit_reason.startswith('硬止�?)]
        hs_count = len(hard_stop_trades)
        hs_avg_loss = np.mean([t.return_pct for t in hard_stop_trades]) if hard_stop_trades else 0
        hs_total_loss = sum(t.profit_amount for t in hard_stop_trades)

        mc = Counter(pd.Timestamp(t.entry_date).to_period('M') for t in trades)
        m_agg['trades'] = [mc.get(m, 0) for m in m_agg.index]
        entries = {}
        for t in trades:
            key = (t.code, t.entry_date)
            if key not in entries:
                entries[key] = t
        me = Counter(pd.Timestamp(e.entry_date).to_period('M') for e in entries.values())
        m_agg['entries'] = [me.get(m, 0) for m in m_agg.index]

        for yr in sorted(y_agg.index):
            yr_trades = [t for t in trades if t.entry_date.year == yr]
            yr_wins = [t for t in yr_trades if t.return_pct > 0]
            y_agg.loc[yr, 'trades'] = len(yr_trades)
            y_agg.loc[yr, 'wr'] = len(yr_wins) / len(yr_trades) * 100 if yr_trades else 0
        yr_trades_vals = [y_agg['trades'].iloc[i] for i in range(len(y_agg))]
    else:
        n = nw = nl = hs_count = 0
        wr = aw = al = at_ = med = pf = ah = tp = hs_avg_loss = hs_total_loss = 0
        ed = Counter()
        yr_trades_vals = []

    mt = [m_agg['trades'].iloc[i] for i in range(len(m_agg))] if len(m_agg) > 0 else []
    amt = np.mean(mt) if mt else 0
    smt = np.std(mt) if mt else 0
    cv = smt / amt if amt > 0 else 999

    elapsed = time.time() - t0

    print(f"\n  ┌─────────────────────────────────────────────────�?)
    print(f"  �?初始资金: {INITIAL_CAPITAL:>13,}                     �?)
    print(f"  �?最终净�? {fe:>13,.0f}                     �?)
    print(f"  �?总收�? {tr:>+12.2f}%  年化: {ann:>+7.2f}%                �?)
    print(f"  �?最大回�? {md:>12.2f}%  夏普: {sharpe:>6.2f}                  �?)
    print(f"  �?PF: {pf:>10.2f}  胜率: {wr:>7.1f}%                    �?)
    print(f"  �?耗时: {elapsed:>10.0f}s  跳过信号: {skipped_signals:>7}              �?)
    print(f"  └─────────────────────────────────────────────────�?)

    print(f"\n  ┌─────────────────────────────────────────────────�?)
    print(f"  �?交易统计                                        �?)
    print(f"  �?总成�? {n:>6}�? �? {nw:<6}�? �? {nl:<6}�?             �?)
    print(f"  �?均盈: {aw:>+9.2f}%  均亏: {al:>+9.2f}%                      �?)
    print(f"  �?均笔: {at_:>+9.2f}%  中位: {med:>+9.2f}%                      �?)
    print(f"  �?均持: {ah:>9.1f}�?                                   �?)
    print(f"  �?硬止�? {hs_count:>4}�? 均亏: {hs_avg_loss:>+7.2f}%  总亏: {hs_total_loss:>+12,.0f}       �?)
    print(f"  └─────────────────────────────────────────────────�?)

    print(f"\n  [退出分布]")
    print(f"  {'原因':<32} {'笔数':>6} {'占比':>8}")
    print(f"  {'-'*48}")
    for reason, count in ed.most_common():
        print(f"  {reason:<32} {count:>6} {count/n*100:>7.1f}%")

    print(f"\n  [年度表现]")
    print(f"  {'年份':<6} {'收益%':>10} {'回撤%':>10} {'交易':>8} {'胜率%':>8}")
    print(f"  {'-'*40}")
    for yr, row in y_agg.iterrows():
        print(f"  {int(yr):<6} {row['ret']:>+9.2f} {row['dd']:>9.2f} "
              f"{int(row.get('trades', 0)):>8} {row.get('wr', 0):>7.1f}")

    print(f"\n  [月度表现]")
    print(f"  {'月份':<10} {'收益%':>8} {'回撤%':>8} {'买入':>6} {'成交':>6}")
    print(f"  {'-'*40}")
    for idx, row in m_agg.iterrows():
        print(f"  {str(idx):<10} {row['ret']:>+7.2f} {row['dd']:>7.2f} "
              f"{int(row.get('entries', 0)):>6} {int(row.get('trades', 0)):>6}")

    monthly_trades = [m_agg['trades'].iloc[i] for i in range(len(m_agg))] if len(m_agg) > 0 else []
    avg_mt = np.mean(monthly_trades) if monthly_trades else 0
    std_mt = np.std(monthly_trades) if monthly_trades else 0
    cv_val = std_mt / avg_mt if avg_mt > 0 else 999

    # 保存
    out = Path(__file__).parent.parent / "output"
    out.mkdir(exist_ok=True)
    eq_df.to_parquet(str(out / "backtest_precise_equity.parquet"), index=False)
    if trades:
        pd.DataFrame([{
            'code': t.code, 'entry': str(t.entry_date), 'exit': str(t.exit_date),
            'entry_px': t.entry_price, 'exit_px': t.exit_price, 'shares': t.shares,
            'ret_pct': t.return_pct, 'profit': t.profit_amount,
            'reason': t.exit_reason, 'hold_days': t.hold_days,
        } for t in trades]).to_parquet(str(out / "backtest_precise_trades.parquet"), index=False)
    print(f"\n  结果已保存至: output/backtest_precise_*.parquet")

    return eq_df, trades


if __name__ == "__main__":
    run_precise_backtest()
