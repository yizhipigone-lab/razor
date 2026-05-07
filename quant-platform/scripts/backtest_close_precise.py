#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度策略 — 收盘前1分钟精确执行版回测
============================================
假设：收盘前1分钟可以按目标价退出（只要未跌停）
跌停处理：
  Mode A: 跌停仍可卖出（按跌停价）
  Mode B: 等打开跌停后当天收盘价卖出
与OHLC版完整对比
"""
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
from collections import Counter, defaultdict
import time
import warnings
warnings.filterwarnings('ignore')

from database.duckdb_manager import db, PARQUET_DAILY_DIR
from app.screener.strategies.ma5_angle import generate_signals

# ═══════════════════════════════════════════════════════════════
INITIAL_CAPITAL = 1_000_000
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
LOAD_START     = date(2022, 1, 1)

SIGNAL_PARAMS = {
    "version": "improved", "filter_st": True, "filter_bj": True,
    "sh_red_filter": False,
    "vol_threshold": 1.5, "close_position_threshold": 0.8,
    "disable_quality_sort": True,
    "filter_consecutive_up": False, "filter_gap_quality": False,
}
# ═══════════════════════════════════════════════════════════════


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
    # 跌停相关
    consecutive_limit_down: int = 0   # 连续跌停天数
    limit_down_dates: list = None     # 跌停日期列表

    def __post_init__(self):
        self.peak_price = self.entry_price
        self.remaining_shares = self.shares
        self.limit_down_dates = []


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
    was_limit_down: bool = False
    limit_down_days: int = 0


def is_limit_down(code, close, prev_close):
    """判断是否跌停"""
    if prev_close is None or prev_close <= 0:
        return False
    pct = (close / prev_close - 1) * 100
    if code.startswith(('300','301','688','689')):
        return pct <= -19.9  # 科创板/创业板20%
    elif code.startswith(('8','4')):
        return pct <= -29.9  # 北交所30%
    else:
        return pct <= -9.9   # 主板10%


def get_limit_down_limit(code, prev_close):
    """跌停价"""
    if prev_close is None or prev_close <= 0:
        return 0
    if code.startswith(('300','301','688','689')):
        return prev_close * 0.80
    elif code.startswith(('8','4')):
        return prev_close * 0.70
    else:
        return prev_close * 0.90


class ClosePreciseEngine:
    """
    收盘前1分钟精确执行引擎
    - 止损：如果当日Low触及止损价 且 未跌停 → 按止损价退出（精确-5.5%）
    - 止盈：如果当日High触及止盈价 → 按止盈价退出
    - 移动止盈：High更新峰值，Low检查回撤
    - 跌停处理：根据mode决定
    """
    def __init__(self, trading_dates, sh_index, mode='A'):
        self.cash = INITIAL_CAPITAL
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict] = []
        self.consecutive_losses = 0
        self.pause_until = None
        self.trading_dates = trading_dates
        self.sh_index = sh_index
        self.mode = mode  # 'A' or 'B'

        # 跌停统计
        self.ld_stats = {
            'total_positions': 0,
            'hit_limit_down': 0,        # 曾跌停的持仓
            'consecutive_ld_events': 0,  # 连续跌停事件
            'ld_exits': 0,              # 因跌停退出的
            'max_consecutive_ld': 0,    # 最长连续跌停
            'ld_details': [],           # 跌停详情
        }

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

    def is_bull_market(self, d):
        if self.sh_index.empty:
            return True
        row = self.sh_index[self.sh_index['date'] == d]
        if row.empty:
            return True
        r = row.iloc[0]
        if pd.isna(r.get('ma20')):
            return True
        return float(r['close']) >= float(r['ma20'])

    def check_stops(self, d: date, daily_bars: dict) -> List[Tuple]:
        """
        收盘前1分钟精确检查
        daily_bars: {code: {'open','high','low','close','prev_close'}}
        """
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
            prev_c  = bar.get('prev_close', close_p)

            # 更新峰值
            if high_p > pos.peak_price:
                pos.peak_price = high_p
            pos.peak_profit_pct = pos.peak_price / pos.entry_price - 1

            hold_days = self._td(pos.entry_date, d)

            # ── 检查跌停状态 ──
            is_ld = is_limit_down(code, close_p, prev_c)
            if is_ld:
                pos.consecutive_limit_down += 1
                pos.limit_down_dates.append(d)
                if pos.consecutive_limit_down == 1:
                    self.ld_stats['hit_limit_down'] += 1
                self.ld_stats['consecutive_ld_events'] += 1
                self.ld_stats['max_consecutive_ld'] = max(
                    self.ld_stats['max_consecutive_ld'], pos.consecutive_limit_down)
            else:
                pos.consecutive_limit_down = 0

            # ── 1. 硬止损 -5.5% ──
            hard_stop_price = pos.entry_price * (1 + HARD_STOP)
            if low_p <= hard_stop_price:
                if not is_ld:
                    # 未跌停 → 收盘前1分钟精确执行止损
                    sells.append((pos, hard_stop_price,
                        f"硬止损(-5.5%)", None))
                    continue
                else:
                    # 跌停了 → 根据mode处理
                    if self.mode == 'A':
                        # Mode A: 跌停仍可卖出（按跌停价）
                        # 跌停价可能比止损价更低
                        ld_price = get_limit_down_limit(code, prev_c)
                        actual_exit = max(ld_price, hard_stop_price * 0.99)  # 卖在跌停价或更差
                        sells.append((pos, actual_exit,
                            f"硬止损跌停({((actual_exit/pos.entry_price)-1)*100:.1f}%)", None))
                        self.ld_stats['ld_exits'] += 1
                        continue
                    else:
                        # Mode B: 等打开跌停，今天不卖
                        # 但如果连续跌停超过3天，强制卖出
                        if pos.consecutive_limit_down >= 3:
                            sells.append((pos, close_p,
                                f"连续跌停强制({pos.consecutive_limit_down}天)", None))
                            self.ld_stats['ld_exits'] += 1
                            continue
                        # 否则跳过止损检查，继续持有
                        pass

            # ── 2. 时间强制 ──
            if hold_days > TIME_FORCE:
                if is_ld:
                    # 跌停中先不强制，等打开（除非连续多天）
                    if pos.consecutive_limit_down >= 3:
                        sells.append((pos, close_p, f"时间强制+跌停({hold_days}天)", None))
                        continue
                else:
                    sells.append((pos, close_p, f"时间强制({hold_days}天)", None))
                    continue

            # ── 3. TP2 +14% ──
            if not pos.tp2_triggered:
                tp2_price = pos.entry_price * (1 + TP2_PCT)
                if high_p >= tp2_price and not is_ld:
                    sells.append((pos, tp2_price, f"TP2 +14%", None))
                    continue

            # ── 4. TP1 +4% ──
            if not pos.tp1_triggered:
                tp1_price = pos.entry_price * (1 + TP1_PCT)
                if high_p >= tp1_price and not is_ld:
                    ss = int(pos.remaining_shares * TP1_RATIO / 100) * 100
                    if ss >= 100:
                        sells.append((pos, tp1_price, f"TP1 +4%", ss))
                        continue

            # ── 5. 移动止盈 ──
            if pos.peak_profit_pct >= TRAIL_ACTIVATE:
                dd_from_peak = low_p / pos.peak_price - 1
                if dd_from_peak <= -TRAIL_DD and not is_ld:
                    trail_price = pos.peak_price * (1 - TRAIL_DD)
                    sells.append((pos, trail_price,
                        f"移动止盈(峰{pos.peak_profit_pct*100:.1f}%回{dd_from_peak*100:.1f}%)", None))
                    continue

            # ── 6. 保本线 ──
            if pos.peak_profit_pct >= 0.03 and low_p <= pos.entry_price and not is_ld:
                sells.append((pos, pos.entry_price, f"保本(曾+{pos.peak_profit_pct*100:.1f}%)", None))
                continue

            # ── 7. 时间条件 ──
            current_profit = close_p / pos.entry_price - 1
            if hold_days > TIME_EXIT and current_profit > 0.01:
                if not is_ld:
                    sells.append((pos, close_p, f"时间条件({hold_days}天+{current_profit*100:.1f}%)", None))
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

        was_ld = pos.consecutive_limit_down > 0
        ld_days = pos.consecutive_limit_down

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
            was_limit_down=was_ld, limit_down_days=ld_days,
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
        self.ld_stats['total_positions'] += 1
        return pos

    def record_equity(self, d, prices):
        eq = self.total_equity(prices)
        self.equity_curve.append({
            'date': d, 'equity': eq, 'cash': self.cash,
            'positions': self.pos_count(),
        })


def load_sh_index():
    sh_path = PARQUET_DAILY_DIR / "index_000001.parquet"
    if not sh_path.exists():
        return pd.DataFrame()
    sh = pd.read_parquet(str(sh_path))
    sh['date'] = pd.to_datetime(sh['date']).dt.date
    sh = sh.sort_values('date')
    sh['ma20'] = sh['close'].rolling(20).mean()
    return sh


def compute_stats(engine, name):
    eq_df = pd.DataFrame(engine.equity_curve)
    if eq_df.empty:
        return {'name': name, 'error': True}
    fe = eq_df['equity'].iloc[-1]
    tr = (fe / INITIAL_CAPITAL - 1) * 100
    eq_df['cummax'] = eq_df['equity'].cummax()
    eq_df['dd'] = (eq_df['equity'] - eq_df['cummax']) / eq_df['cummax'] * 100
    md = eq_df['dd'].min()
    eq_df['daily_ret'] = eq_df['equity'].pct_change()
    sharpe = eq_df['daily_ret'].mean() / eq_df['daily_ret'].std() * np.sqrt(252) if eq_df['daily_ret'].std() > 0 else 0
    days = (BACKTEST_END - BACKTEST_START).days
    ann = ((fe / INITIAL_CAPITAL) ** (365.25 / days) - 1) * 100

    trades = engine.trades
    if not trades:
        return {'name': name, 'total_ret': tr, 'max_dd': md}

    wins = [t for t in trades if t.return_pct > 0]
    loses = [t for t in trades if t.return_pct <= 0]
    n = len(trades)
    nw = len(wins)
    nl = len(loses)
    wr = nw / n * 100 if n else 0
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

    # 硬止损详细
    hs = [t for t in trades if '硬止损' in t.exit_reason]
    hs_avg = np.mean([t.return_pct for t in hs]) if hs else 0
    hs_med = np.median([t.return_pct for t in hs]) if hs else 0
    hs_worst = min([t.return_pct for t in hs]) if hs else 0

    # 跌停统计
    ld_trades = [t for t in trades if t.was_limit_down]
    ld_avg = np.mean([t.return_pct for t in ld_trades]) if ld_trades else 0

    # 年度
    eq_df['year'] = pd.to_datetime(eq_df['date']).dt.year
    y_agg = eq_df.groupby('year').agg(start=('equity','first'), end=('equity','last'), dd=('dd','min'))
    y_agg['ret'] = (y_agg['end'] / y_agg['start'] - 1) * 100

    for yr in sorted(y_agg.index):
        yr_t = [t for t in trades if t.entry_date.year == yr]
        yr_w = [t for t in yr_t if t.return_pct > 0]
        y_agg.loc[yr, 'trades'] = len(yr_t)
        y_agg.loc[yr, 'wr'] = len(yr_w) / len(yr_t) * 100 if yr_t else 0

    return {
        'name': name,
        'final_eq': fe, 'total_ret': tr, 'ann_ret': ann,
        'max_dd': md, 'sharpe': sharpe,
        'trades': n, 'win_trades': nw, 'loss_trades': nl,
        'win_rate': wr, 'avg_win': aw, 'avg_loss': al,
        'avg_trade': at_, 'med_trade': med, 'profit_factor': pf,
        'total_profit': tp, 'avg_hold': ah,
        'exit_dist': ed,
        'hs_count': len(hs), 'hs_avg': hs_avg, 'hs_med': hs_med, 'hs_worst': hs_worst,
        'ld_trades': len(ld_trades), 'ld_avg': ld_avg,
        'ld_stats': engine.ld_stats,
        'yearly': y_agg,
    }


def run_backtest(mode='A'):
    """mode: 'A'=跌停可卖, 'B'=等打开跌停"""
    t0 = time.time()
    mode_label = {'A': '跌停可卖(按跌停价)', 'B': '等打开跌停(收盘价退出)'}[mode]
    print(f"\n{'='*72}")
    print(f"  收盘前1分钟精确执行 — Mode {mode}: {mode_label}")
    print(f"{'='*72}")

    bars = db.load_all_bars(freq="daily", start=LOAD_START, end=BACKTEST_END)
    bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
    for c in ["open", "high", "low", "close", "volume"]:
        if c in bars:
            bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=["close"])
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    bars = bars.sort_values(["code", "date"])

    # 计算前日收盘价（用于判断跌停）
    bars['prev_close'] = bars.groupby('code')['close'].shift(1)

    sig = generate_signals(bars, **SIGNAL_PARAMS)
    sig = sig[(sig["date"] >= BACKTEST_START) & (sig["date"] <= BACKTEST_END)].copy()
    sig["date"] = pd.to_datetime(sig["date"]).dt.date
    sig = sig.sort_values(["date", "code"])

    bt_bars = bars[(bars["date"] >= BACKTEST_START) & (bars["date"] <= BACKTEST_END)]
    daily_ohlc: Dict[date, Dict] = {}
    daily_closes: Dict[date, Dict] = {}

    for d, g in bt_bars.groupby("date"):
        d_dict = {}
        c_dict = {}
        for _, r in g.iterrows():
            d_dict[r['code']] = {
                'open': float(r['open']), 'high': float(r['high']),
                'low': float(r['low']), 'close': float(r['close']),
                'prev_close': float(r.get('prev_close', r['close'])),
            }
            c_dict[r['code']] = float(r['close'])
        daily_ohlc[d] = d_dict
        daily_closes[d] = c_dict

    trading_dates = sorted(daily_ohlc.keys())

    sig_by_date: Dict[date, List[str]] = {}
    for _, r in sig.iterrows():
        sig_by_date.setdefault(r['date'], []).append(r['code'])

    sh_idx = load_sh_index()
    engine = ClosePreciseEngine(trading_dates, sh_idx, mode)
    skipped = 0

    for i, d in enumerate(trading_dates):
        ohlc = daily_ohlc[d]
        closes = daily_closes[d]

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

        engine.positions = {k: v for k, v in engine.positions.items() if v.is_active}

        if d in sig_by_date:
            paused = engine.pause_until and d <= engine.pause_until
            bull = engine.is_bull_market(d)
            if not paused and bull:
                for code in sig_by_date[d]:
                    price = closes.get(code)
                    if price is None:
                        continue
                    if any(t.code == code and d - t.entry_date <= timedelta(days=20)
                           for t in engine.trades):
                        continue
                    result = engine.execute_buy(d, code, price)
                    if result is None:
                        skipped += 1

        engine.record_equity(d, closes)

        if (i + 1) % 150 == 0:
            print(f"  {d} | {i+1}/{len(trading_dates)} | "
                  f"净值 {engine.total_equity(closes):,.0f} | 持仓 {engine.pos_count()}")

    stats = compute_stats(engine, f"Mode {mode}")
    stats['runtime'] = time.time() - t0
    return stats, engine


def print_report(stats, engine):
    s = stats
    if s.get('error'):
        print("无交易")
        return

    print(f"\n  ┌─────────────────────────────────────────────────────────┐")
    print(f"  │ {s['name']:<55} │")
    print(f"  │ 初始资金: {INITIAL_CAPITAL:>13,}  最终净值: {s['final_eq']:>13,.0f}         │")
    print(f"  │ 总收益: {s['total_ret']:>+10.2f}%  年化: {s['ann_ret']:>+8.2f}%  回撤: {s['max_dd']:>+7.2f}%         │")
    print(f"  │ 夏普: {s['sharpe']:>6.2f}  PF: {s['profit_factor']:>5.2f}  胜率: {s['win_rate']:>5.1f}%                   │")
    print(f"  │ 均盈: {s['avg_win']:>+7.2f}%  均亏: {s['avg_loss']:>+7.2f}%  均笔: {s['avg_trade']:>+7.2f}%               │")
    print(f"  │ 交易: {s['trades']:>6}笔  均持: {s['avg_hold']:>4.1f}天                            │")
    print(f"  └─────────────────────────────────────────────────────────┘")

    print(f"\n  ┌─────────────────────────────────────────────────────────┐")
    print(f"  │ 硬止损: {s['hs_count']:>5}笔  均亏: {s['hs_avg']:>+7.2f}%  中位: {s['hs_med']:>+7.2f}%  最差: {s['hs_worst']:>+7.2f}%    │")
    print(f"  │ 跌停交易: {s['ld_trades']:>4}笔  均亏: {s['ld_avg']:>+7.2f}%                                   │")
    print(f"  └─────────────────────────────────────────────────────────┘")

    ld = s['ld_stats']
    print(f"\n  ┌─────────────────────────────────────────────────────────┐")
    print(f"  │ 跌停统计                                               │")
    print(f"  │ 总持仓: {ld['total_positions']:>5}  曾跌停: {ld['hit_limit_down']:>5} ({ld['hit_limit_down']/max(ld['total_positions'],1)*100:>4.1f}%)                │")
    print(f"  │ 连续跌停事件: {ld['consecutive_ld_events']:>5}  跌停退出: {ld['ld_exits']:>5}                     │")
    print(f"  │ 最长连续跌停: {ld['max_consecutive_ld']:>5}天                                     │")
    print(f"  └─────────────────────────────────────────────────────────┘")

    print(f"\n  [退出分布]")
    print(f"  {'原因':<32} {'笔数':>6} {'占比':>8}")
    print(f"  {'-'*48}")
    for reason, count in s['exit_dist'].most_common():
        print(f"  {reason:<32} {count:>6} {count/s['trades']*100:>7.1f}%")

    print(f"\n  [年度表现]")
    y = s['yearly']
    print(f"  {'年份':<6} {'收益%':>10} {'回撤%':>10} {'交易':>8} {'胜率%':>8}")
    for yr, row in y.iterrows():
        print(f"  {int(yr):<6} {row['ret']:>+9.2f} {row['dd']:>9.2f} {int(row.get('trades',0)):>8} {row.get('wr',0):>7.1f}")


# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # 运行两个模式 + 加载OHLC版数据做对比
    results = {}

    # Mode A: 跌停可卖
    stats_a, eng_a = run_backtest('A')
    print_report(stats_a, eng_a)
    results['A'] = stats_a

    # Mode B: 等打开跌停
    stats_b, eng_b = run_backtest('B')
    print_report(stats_b, eng_b)
    results['B'] = stats_b

    # 加载OHLC版结果做对比
    print(f"\n{'='*72}")
    print(f"  三版综合对比")
    print(f"{'='*72}")

    ohlc_eq = pd.read_parquet('output/backtest_precise_equity.parquet')
    ohlc_tr = pd.read_parquet('output/backtest_precise_trades.parquet')
    ohlc_fe = ohlc_eq['equity'].iloc[-1]
    ohlc_total_ret = (ohlc_fe / INITIAL_CAPITAL - 1) * 100
    ohlc_eq['cummax'] = ohlc_eq['equity'].cummax()
    ohlc_dd = (ohlc_eq['equity'] - ohlc_eq['cummax']) / ohlc_eq['cummax'] * 100
    ohlc_max_dd = ohlc_dd.min()
    ohlc_eq['daily_ret'] = ohlc_eq['equity'].pct_change()
    ohlc_sharpe = ohlc_eq['daily_ret'].mean() / ohlc_eq['daily_ret'].std() * np.sqrt(252)
    ohlc_wins = (ohlc_tr['ret_pct'] > 0).sum()
    ohlc_wr = ohlc_wins / len(ohlc_tr) * 100
    ohlc_hs = ohlc_tr[ohlc_tr['reason'].str.startswith('硬止损')]
    ohlc_pf = ohlc_tr[ohlc_tr['ret_pct']>0]['ret_pct'].sum() / abs(ohlc_tr[ohlc_tr['ret_pct']<=0]['ret_pct'].sum())

    print(f"\n  {'指标':<24} {'OHLC版(Low执行)':<22} {'Mode A(跌停可卖)':<22} {'Mode B(等打开跌停)':<22}")
    print(f"  {'-'*90}")
    print(f"  {'总收益%':<24} {ohlc_total_ret:>+20.2f} {results['A']['total_ret']:>+20.2f} {results['B']['total_ret']:>+20.2f}")
    print(f"  {'年化%':<24} {((ohlc_fe/INITIAL_CAPITAL)**(365.25/(BACKTEST_END-BACKTEST_START).days)-1)*100:>+20.2f} {results['A']['ann_ret']:>+20.2f} {results['B']['ann_ret']:>+20.2f}")
    print(f"  {'最大回撤%':<24} {ohlc_max_dd:>+20.2f} {results['A']['max_dd']:>+20.2f} {results['B']['max_dd']:>+20.2f}")
    print(f"  {'夏普':<24} {ohlc_sharpe:>20.2f} {results['A']['sharpe']:>20.2f} {results['B']['sharpe']:>20.2f}")
    print(f"  {'胜率%':<24} {ohlc_wr:>20.1f} {results['A']['win_rate']:>20.1f} {results['B']['win_rate']:>20.1f}")
    print(f"  {'PF':<24} {ohlc_pf:>20.2f} {results['A']['profit_factor']:>20.2f} {results['B']['profit_factor']:>20.2f}")
    print(f"  {'交易笔数':<24} {len(ohlc_tr):>20} {results['A']['trades']:>20} {results['B']['trades']:>20}")
    print(f"  {'均持(天)':<24} {ohlc_tr['hold_days'].mean():>20.1f} {results['A']['avg_hold']:>20.1f} {results['B']['avg_hold']:>20.1f}")

    print(f"\n  {'硬止损':-^90}")
    print(f"  {'硬止损笔数':<24} {len(ohlc_hs):>20} {results['A']['hs_count']:>20} {results['B']['hs_count']:>20}")
    print(f"  {'硬止损均亏%':<24} {ohlc_hs['ret_pct'].mean():>+20.2f} {results['A']['hs_avg']:>+20.2f} {results['B']['hs_avg']:>+20.2f}")
    print(f"  {'硬止损最差%':<24} {ohlc_hs['ret_pct'].min():>+20.2f} {results['A']['hs_worst']:>+20.2f} {results['B']['hs_worst']:>+20.2f}")

    print(f"\n  {'跌停':-^90}")
    for k in ['A', 'B']:
        ld = results[k]['ld_stats']
        print(f"  Mode {k}: 总持仓{ld['total_positions']} | 曾跌停{ld['hit_limit_down']} ({ld['hit_limit_down']/max(ld['total_positions'],1)*100:.1f}%) | 连续跌停事件{ld['consecutive_ld_events']} | 最长{ld['max_consecutive_ld']}天 | 跌停退出{ld['ld_exits']}")
