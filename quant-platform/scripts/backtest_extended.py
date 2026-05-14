#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度策略 �?全区间扩展回测（2023-01-01 ~ 2026-05-02�?使用用户优化后的策略参数和退出机�?"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pandas as pd
import numpy as np
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from collections import Counter
import time
import warnings
warnings.filterwarnings('ignore')

from database.duckdb_manager import db
from app.screener.strategies.ma5_angle import generate_signals

# ══════════════════════════════════════════════════════════════�?#  策略参数（用户最终精简版）
# ══════════════════════════════════════════════════════════════�?INITIAL_CAPITAL   = 1_000_000
POSITION_SIZE     = 50_000      # 单票仓位

# 退出参�?HARD_STOP_LOSS    = -0.055      # -5.5%
TP1_PCT           = 0.04        # +4%
TP1_SELL_RATIO    = 0.20        # 卖出20%
TP2_PCT           = 0.14        # +14%
TP2_SELL_RATIO    = 1.0         # 清仓剩余
TRAIL_ACTIVATE    = 0.08        # +8%激活移动止�?TRAIL_DD          = 0.02        # 2%回撤触发
TIME_EXIT_DAYS    = 7           # 7天条件退�?TIME_FORCE_DAYS   = 10          # 10天强制退�?BREAKEVEN_PCT     = 0.99        # 实质禁用

# 资金/仓位
POSITION_CAP      = POSITION_SIZE
MIN_BUY_AMT       = 5000

# 风控
LOSS_STREAK_1     = 3           # 连亏3笔仓位减�?LOSS_STREAK_2     = 5           # 连亏5笔暂�?�?PAUSE_DAYS        = 3

# 质量排序权重
W_ANGLE           = 0.4
W_VOL             = 0.3
W_POS             = 0.3

# 回测区间
BACKTEST_START = date(2022, 1, 4)
BACKTEST_END   = date(2026, 5, 2)
BUFFER_DAYS    = 365
LOAD_START     = date(2022, 1, 1)

# 信号参数（用户最终版�?SIGNAL_PARAMS = {
    "version": "improved",
    "filter_st": True,
    "filter_bj": True,
    "vol_threshold": 1.5,
    "close_position_threshold": 0.8,
    "disable_quality_sort": True,  # 回测引擎自己做质量排�?    "filter_consecutive_up": False,
    "filter_gap_quality": False,
}

# ══════════════════════════════════════════════════════════════�?#  数据结构
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


# ══════════════════════════════════════════════════════════════�?#  回测引擎
# ══════════════════════════════════════════════════════════════�?
class BacktestEngine:
    def __init__(self, trading_dates: List[date]):
        self.cash = INITIAL_CAPITAL
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict] = []
        self.consecutive_losses = 0
        self.pause_until: Optional[date] = None
        self.trading_dates = trading_dates
        self._date_set = set(trading_dates)
        self.sh_index = self._load_sh_index()

    def _load_sh_index(self) -> pd.DataFrame:
        try:
            sh_path = Path(PARQUET_DAILY_DIR) / "index_000001.parquet"
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
        """上证大盘 MA20 过滤（仅在上证红盘日基础上额外过滤）"""
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

    def max_position_size(self) -> float:
        if self.consecutive_losses >= LOSS_STREAK_1:
            return POSITION_CAP / 2
        return POSITION_CAP

    def _trading_days_between(self, d1: date, d2: date) -> int:
        return len([td for td in self.trading_dates if d1 <= td <= d2])

    def check_stops(self, d: date, closes: Dict[str, float],
                    highs: Dict[str, float]) -> List[Tuple]:
        """
        优先�? 硬止�?> 时间强制 > TP2 > TP1 > 移动止盈 > 时间条件
        """
        sells = []
        for code, pos in list(self.positions.items()):
            if not pos.is_active or pos.remaining_shares <= 0:
                continue
            price = closes.get(code)
            if price is None or price <= 0:
                continue

            high = highs.get(code, price)
            if high > pos.peak_price:
                pos.peak_price = high
            pos.peak_profit_pct = pos.peak_price / pos.entry_price - 1

            current_profit = price / pos.entry_price - 1
            hold_days = self._trading_days_between(pos.entry_date, d)
            rem = pos.remaining_shares

            # 1. 硬止�?-5.5%
            if current_profit <= HARD_STOP_LOSS:
                sells.append((pos, price, f"硬止�?{current_profit*100:.1f}%)", None))
                continue

            # 2. 时间强制退�?>10�?            if hold_days > TIME_FORCE_DAYS:
                sells.append((pos, price, f"时间强制({hold_days}�?", None))
                continue

            # 3. TP2: +14% 清仓
            if not pos.tp2_triggered and current_profit >= TP2_PCT:
                sells.append((pos, price, f"TP2 +14%({current_profit*100:.1f}%)", None))
                continue

            # 4. TP1: +4% �?0%
            if not pos.tp1_triggered and current_profit >= TP1_PCT:
                sell_shares = int(rem * TP1_SELL_RATIO / 100) * 100
                if sell_shares >= 100:
                    sells.append((pos, price, f"TP1 +4%({current_profit*100:.1f}%)", sell_shares))
                    continue

            # 5. 移动止盈: 盈利>8%后回�?%
            if pos.peak_profit_pct >= TRAIL_ACTIVATE:
                dd_from_peak = price / pos.peak_price - 1
                if dd_from_peak <= -TRAIL_DD:
                    trail_price = pos.peak_price * (1 - TRAIL_DD)
                    sells.append((pos, trail_price,
                        f"移动止盈(峰{pos.peak_profit_pct*100:.1f}%回{dd_from_peak*100:.1f}%)", None))
                    continue

            # 6. 时间条件: >7天且有利�?>1%)
            if hold_days > TIME_EXIT_DAYS and current_profit > 0.01:
                sells.append((pos, price, f"时间条件({hold_days}�?{current_profit*100:.1f}%)", None))
                continue

        return sells

    def execute_sell(self, pos: Position, exit_price: float, reason: str,
                     partial_shares: Optional[int] = None,
                     exit_date: Optional[date] = None) -> Optional[Trade]:
        if partial_shares is not None:
            sell_shares = partial_shares
        else:
            sell_shares = pos.remaining_shares
        sell_shares = int(sell_shares // 100 * 100)
        if sell_shares <= 0:
            return None

        ret_pct = (exit_price / pos.entry_price - 1) * 100
        profit = sell_shares * (exit_price - pos.entry_price)
        pos.remaining_shares -= sell_shares

        # 根据实际退出原因标�?        if "TP2" in reason:
            pos.tp2_triggered = True
        if "TP1" in reason:
            pos.tp1_triggered = True

        if pos.remaining_shares <= 0:
            pos.is_active = False
            pos.remaining_shares = 0

        self.cash += sell_shares * exit_price

        return Trade(
            code=pos.code, entry_date=pos.entry_date,
            exit_date=exit_date or date.today(),
            entry_price=pos.entry_price, exit_price=exit_price,
            shares=sell_shares, return_pct=ret_pct,
            profit_amount=profit, exit_reason=reason, hold_days=0,
        )

    def execute_buy(self, d: date, code: str, price: float) -> Optional[Position]:
        if code in self.positions:
            return None
        max_amt = min(self.max_position_size(), self.cash)
        if max_amt < MIN_BUY_AMT:
            return None
        shares = int(max_amt / price / 100) * 100
        if shares < 100:
            return None
        cost = shares * price
        if cost > self.cash:
            return None

        pos = Position(
            code=code, entry_date=d, entry_price=price,
            shares=shares, cost=cost,
        )
        self.cash -= cost
        self.positions[code] = pos
        return pos

    def record_equity(self, d: date, prices: Dict[str, float]):
        equity = self.total_equity(prices)
        self.equity_curve.append({
            'date': d, 'equity': equity,
            'cash': self.cash, 'positions': self.position_count(),
        })


# ══════════════════════════════════════════════════════════════�?#  主流�?# ══════════════════════════════════════════════════════════════�?
from database.duckdb_manager import PARQUET_DAILY_DIR


def run_backtest():
    t0 = time.time()
    print("=" * 72)
    print("  MA5 角度策略 �?全区间扩展回�?)
    print(f"  区间: {BACKTEST_START} ~ {BACKTEST_END}")
    print(f"  初始资金: {INITIAL_CAPITAL:,}  单票仓位: {POSITION_SIZE:,}")
    print(f"  退�? 硬止损{HARD_STOP_LOSS*100:+.1f}% TP1+{TP1_PCT*100:.0f}%/{int(TP1_SELL_RATIO*100)}% TP2+{TP2_PCT*100:.0f}%/清仓")
    print("=" * 72)

    # ── 1. 加载日线数据 ──────────────────────────────────
    print(f"\n[1/5] 加载日线数据 ({LOAD_START} ~ {BACKTEST_END}) ...")
    bars = db.load_all_bars(freq="daily", start=LOAD_START, end=BACKTEST_END)
    bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
    for c in ["open", "high", "low", "close", "volume"]:
        if c in bars:
            bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=["close"])
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    bars = bars.sort_values(["code", "date"])
    n_stocks = bars['code'].nunique()
    print(f"  {n_stocks:,} 只股�? {len(bars):,} �?)

    if n_stocks < 100:
        print("  [错误] 数据不足，请先同步日线数�?)
        return

    # ── 2. 生成信号 ─────────────────────────────────────
    print(f"\n[2/5] 生成策略信号 ...")
    sig = generate_signals(bars, **SIGNAL_PARAMS)
    sig = sig[(sig["date"] >= BACKTEST_START) & (sig["date"] <= BACKTEST_END)].copy()
    sig["date"] = pd.to_datetime(sig["date"]).dt.date
    sig = sig.sort_values(["date", "code"])
    total_signals = len(sig)
    signal_dates = sig['date'].nunique()
    signal_stocks = sig['code'].nunique()
    print(f"  信号�? {total_signals:,} | 信号�? {signal_dates} | 标的: {signal_stocks}")

    if total_signals == 0:
        print("  [错误] 无信号生�?)
        return

    # 查看信号质量分布
    if 'x1' in sig.columns:
        print(f"  斜率均�? {sig['x1'].mean():.2f}%  中位: {sig['x1'].median():.2f}%")
        print(f"  斜率范围: {sig['x1'].min():.2f}% ~ {sig['x1'].max():.2f}%")

    # ── 3. 构建交易日和快照 ─────────────────────────────
    print(f"\n[3/5] 构建价格快照 ...")
    bt_bars = bars[(bars["date"] >= BACKTEST_START) & (bars["date"] <= BACKTEST_END)]
    snaps = {}
    for d, g in bt_bars.groupby("date"):
        snaps[d] = {
            'close': dict(zip(g['code'], g['close'])),
            'high': dict(zip(g['code'], g['high'])),
        }
    trading_dates = sorted(snaps.keys())
    print(f"  交易�? {len(trading_dates):,}")

    # 信号按日索引
    sig_by_date: Dict[date, List[Dict]] = {}
    for _, r in sig.iterrows():
        d = r['date']
        sig_by_date.setdefault(d, []).append({
            'code': r['code'],
            'angle': float(r.get('x1', 0)),
            'vol_ratio': float(r.get('volume', 0)) / max(float(r.get('avg_vol_20', 1)), 1),
            'close_pos': float(r.get('close_pos', 0.5)),
            'quality': float(r.get('quality', 0)),
        })

    # ── 4. 运行回测 ─────────────────────────────────────
    print(f"\n[4/5] 运行回测 (逐日) ...")
    engine = BacktestEngine(trading_dates)
    skipped_signal_count = 0

    for i, d in enumerate(trading_dates):
        closes = snaps[d]['close']
        highs = snaps[d]['high']

        # Step 1: 检查止�?止盈
        for pos, exit_price, reason, partial in engine.check_stops(d, closes, highs):
            trade = engine.execute_sell(pos, exit_price, reason, partial, exit_date=d)
            if trade:
                trade.hold_days = engine._trading_days_between(pos.entry_date, d)
                engine.trades.append(trade)
                if trade.return_pct <= 0:
                    engine.consecutive_losses += 1
                else:
                    engine.consecutive_losses = 0
                    engine.pause_until = None
                if engine.consecutive_losses >= LOSS_STREAK_2:
                    engine.pause_until = d + timedelta(days=PAUSE_DAYS)

        # 清理已平�?        engine.positions = {k: v for k, v in engine.positions.items() if v.is_active}

        # Step 2: 买入新信�?        if d in sig_by_date:
            paused = engine.pause_until is not None and d <= engine.pause_until
            bull = engine.is_bull_market(d)
            if not paused:
                active_signals = sig_by_date[d]
                # �?quality 排序（如果有的话�?                if active_signals and 'quality' in active_signals[0]:
                    active_signals.sort(key=lambda x: x['quality'], reverse=True)
                # 计算当日可买信号数（根据现金和仓位上限）
                max_positions = int(engine.cash / POSITION_CAP) + 1
                for si in active_signals[:max_positions]:
                    code = si['code']
                    entry_price = closes.get(code)
                    if entry_price is None or entry_price <= 0:
                        continue
                    # 20天不重复同股�?                    if any(t.code == code and d - t.entry_date <= timedelta(days=20)
                           for t in engine.trades):
                        continue
                    result = engine.execute_buy(d, code, entry_price)
                    if result is None:
                        skipped_signal_count += 1

        # Step 3: 记录净�?        engine.record_equity(d, closes)

        if (i + 1) % 100 == 0:
            eq = engine.total_equity(closes)
            print(f"  {d} | {i+1}/{len(trading_dates)} | "
                  f"净�?{eq:,.0f} | 持仓 {engine.position_count()} | 现金 {engine.cash:,.0f}")

    # ── 5. 统计报告 ─────────────────────────────────────
    print(f"\n[5/5] 生成报告 ...")
    elapsed = time.time() - t0

    if not engine.equity_curve:
        print("  无交易记�?")
        return

    eq_df = pd.DataFrame(engine.equity_curve)
    final_eq = eq_df['equity'].iloc[-1]
    total_ret = (final_eq / INITIAL_CAPITAL - 1) * 100
    eq_df['cummax'] = eq_df['equity'].cummax()
    eq_df['dd'] = (eq_df['equity'] - eq_df['cummax']) / eq_df['cummax'] * 100
    max_dd = eq_df['dd'].min()

    # 月度统计
    eq_df['month'] = pd.to_datetime(eq_df['date']).dt.to_period('M')
    m_agg = eq_df.groupby('month').agg(
        start=('equity', 'first'), end=('equity', 'last'), dd=('dd', 'min')
    )
    m_agg['ret'] = (m_agg['end'] / m_agg['start'] - 1) * 100

    # 按年统计
    eq_df['year'] = pd.to_datetime(eq_df['date']).dt.year
    y_agg = eq_df.groupby('year').agg(
        start=('equity', 'first'), end=('equity', 'last'), dd=('dd', 'min')
    )
    y_agg['ret'] = (y_agg['end'] / y_agg['start'] - 1) * 100

    if engine.trades:
        mc = Counter(pd.Timestamp(t.entry_date).to_period('M') for t in engine.trades)
        m_agg['trades'] = [mc.get(m, 0) for m in m_agg.index]
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
        wins = [t for t in trades if t.return_pct > 0]
        loses = [t for t in trades if t.return_pct <= 0]
        n_total = len(trades)
        n_win = len(wins)
        n_loss = len(loses)
        wr = n_win / n_total * 100 if n_total else 0
        avg_w = np.mean([t.return_pct for t in wins]) if wins else 0
        avg_l = np.mean([t.return_pct for t in loses]) if loses else 0
        avg_t = np.mean([t.return_pct for t in trades])
        med_t = np.median([t.return_pct for t in trades])
        tg = sum(t.return_pct for t in wins)
        tl = abs(sum(t.return_pct for t in loses))
        pf = tg / tl if tl > 0 else float('inf')
        total_profit = sum(t.profit_amount for t in trades)
        avg_hold = np.mean([t.hold_days for t in trades])
        exit_dist = Counter(t.exit_reason.split('(')[0] for t in trades)

        # 按年统计交易
        for yr in sorted(y_agg.index):
            yr_trades = [t for t in trades if t.entry_date.year == yr]
            yr_wins = [t for t in yr_trades if t.return_pct > 0]
            y_agg.loc[yr, 'trades'] = len(yr_trades)
            y_agg.loc[yr, 'wr'] = len(yr_wins) / len(yr_trades) * 100 if yr_trades else 0
            y_agg.loc[yr, 'avg_ret'] = np.mean([t.return_pct for t in yr_trades]) if yr_trades else 0
    else:
        n_total = n_win = n_loss = 0
        wr = avg_w = avg_l = avg_t = med_t = pf = avg_hold = total_profit = 0
        exit_dist = Counter()

    monthly_trades = [m_agg['trades'].iloc[i] for i in range(len(m_agg))] if len(m_agg) > 0 else []
    avg_mt = np.mean(monthly_trades) if monthly_trades else 0
    std_mt = np.std(monthly_trades) if monthly_trades else 0
    cv = std_mt / avg_mt if avg_mt > 0 else 999

    # 滚动12个月统计
    if len(eq_df) >= 240:
        eq_df['ret_12m'] = eq_df['equity'].pct_change(240).fillna(0) * 100

    # ── 输出报告 ───────────────────────────────────────
    print("\n" + "=" * 72)
    print("  全区间扩展回测报�?)
    print("=" * 72)

    print(f"\n  ┌─────────────────────────────────────────────────�?)
    print(f"  �?区间: {BACKTEST_START} ~ {BACKTEST_END}")
    print(f"  �?交易�? {len(trading_dates):,}  股票: {n_stocks:,}")
    print(f"  �?初始资金: {INITIAL_CAPITAL:>13,}                     �?)
    print(f"  �?最终净�? {final_eq:>13,.0f}                     �?)
    print(f"  �?总收益率: {total_ret:>+12.2f}%                    �?)
    print(f"  �?年化收益: {((final_eq/INITIAL_CAPITAL)**(1/((BACKTEST_END-BACKTEST_START).days/365.25))-1)*100:>+10.2f}%                    �?)
    print(f"  �?最大回�? {max_dd:>12.2f}%                    �?)
    print(f"  �?Profit Factor: {pf:>10.2f}                      �?)
    print(f"  �?总盈利额: {total_profit:>+13,.0f}                     �?)
    print(f"  �?耗时: {elapsed:>10.0f}s                         �?)
    print(f"  └─────────────────────────────────────────────────�?)

    print(f"\n  ┌─────────────────────────────────────────────────�?)
    print(f"  �?交易统计                                        �?)
    print(f"  �?信号总数: {total_signals:>7}  实际买入: {len(set((t.code, t.entry_date) for t in trades)):>7}              �?)
    print(f"  �?总成�? {n_total:>8}�?(含部分止盈拆�?                 �?)
    print(f"  �?盈利: {n_win:>6}�?/ 亏损: {n_loss:<6}�?                 �?)
    print(f"  �?胜率: {wr:>9.1f}%                              �?)
    print(f"  �?均盈: {avg_w:>+9.2f}%  均亏: {avg_l:>+9.2f}%             �?)
    print(f"  �?均笔: {avg_t:>+9.2f}%  中位: {med_t:>+9.2f}%             �?)
    print(f"  �?均持: {avg_hold:>9.1f}�?                                   �?)
    print(f"  └─────────────────────────────────────────────────�?)

    print(f"\n  ┌─────────────────────────────────────────────────�?)
    print(f"  �?月度均衡                                        �?)
    print(f"  �?月均交易: {avg_mt:>7.1f}  标准�? {std_mt:>7.1f}  CV: {cv:>7.2f}              �?)
    print(f"  └─────────────────────────────────────────────────�?)

    print(f"\n  [退出分布] (共{n_total}�?")
    print(f"  {'原因':<32} {'笔数':>6} {'占比':>8}")
    print(f"  {'-'*48}")
    for reason, count in exit_dist.most_common():
        pct = count / n_total * 100 if n_total else 0
        print(f"  {reason:<32} {count:>6} {pct:>7.1f}%")

    print(f"\n  [年度表现]")
    print(f"  {'年份':<6} {'收益%':>10} {'回撤%':>10} {'交易笔数':>10} {'胜率%':>10} {'均笔%':>10}")
    print(f"  {'-'*58}")
    for yr, row in y_agg.iterrows():
        print(f"  {int(yr):<6} {row['ret']:>+9.2f} {row['dd']:>9.2f} {int(row.get('trades', 0)):>10} {row.get('wr', 0):>9.1f} {row.get('avg_ret', 0):>+9.2f}")

    print(f"\n  [月度表现]")
    print(f"  {'月份':<10} {'收益%':>8} {'回撤%':>8} {'买入':>6} {'成交':>6}")
    print(f"  {'-'*40}")
    for idx, row in m_agg.iterrows():
        print(f"  {str(idx):<10} {row['ret']:>+7.2f} {row['dd']:>7.2f} "
              f"{int(row.get('entries', 0)):>6} {int(row.get('trades', 0)):>6}")

    # ── 保存结果 ───────────────────────────────────────
    out = Path(__file__).parent.parent / "output"
    out.mkdir(exist_ok=True)
    eq_df.to_parquet(str(out / "backtest_extended_equity.parquet"), index=False)
    if trades:
        pd.DataFrame([{
            'code': t.code, 'entry': str(t.entry_date), 'exit': str(t.exit_date),
            'entry_px': t.entry_price, 'exit_px': t.exit_price, 'shares': t.shares,
            'ret_pct': t.return_pct, 'profit': t.profit_amount,
            'reason': t.exit_reason, 'hold_days': t.hold_days,
        } for t in trades]).to_parquet(str(out / "backtest_extended_trades.parquet"), index=False)

    print(f"\n  结果已保存至: output/backtest_extended_*.parquet")

    return eq_df, trades, m_agg, y_agg


if __name__ == "__main__":
    run_backtest()
