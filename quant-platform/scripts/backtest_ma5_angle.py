#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度策略 — 完整回测（含止损/止盈/风控体系）
回测区间: 2025-04-25 ~ 2026-04-29
初始资金: 100万, 单票上限: 4万

止损体系 (按优先级):
  1. 硬止损 -9%
  2. 时间止损: >10天强制清仓
  3. 分阶段止盈: +5%卖1/3, +10%再卖1/2, +15%清仓 (每级仅触发一次)
  4. 移动止盈: 盈利>5%后回撤-3%离场
  5. 保本线: 盈利>3%后跌破成本离场
  6. 时间止损-早期: >7天且盈利>1%

风控:
  连败保护: 连亏3笔仓位减半, 连亏5笔暂停3天
  20天无重复信号
  大盘MA20过滤

执行假设: 收盘前最后几分钟确认信号并买入，出入场均用当日收盘价。
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
from collections import Counter

from database.duckdb_manager import db
from app.screener.strategies.ma5_angle import generate_signals

# ── 回测配置 ──────────────────────────────────────────────────
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


# ── 数据结构 ──────────────────────────────────────────────────
@dataclass
class Position:
    code: str
    entry_date: date
    entry_price: float
    shares: int
    cost: float
    peak_price: float
    peak_profit_pct: float = 0.0
    remaining_shares: int = 0
    staged_level: int = 0           # 0=未触发, 1=已触发5%, 2=已触发10%, 3=已触发15%
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


# ── 回测引擎 ──────────────────────────────────────────────────
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

    def current_position_count(self) -> int:
        return len([p for p in self.positions.values() if p.is_active])

    def max_buy_amount(self) -> float:
        if self.consecutive_losses >= LOSS_STREAK_1:
            return MAX_PER_STOCK / 2
        return MAX_PER_STOCK

    def _trading_days_since(self, d: date) -> int:
        """d 是第几个交易日 (从回测起点算)"""
        return len([td for td in self.trading_dates if td <= d])

    def _trading_days_between(self, d1: date, d2: date) -> int:
        return len([td for td in self.trading_dates if d1 <= td <= d2])

    def check_stops(self, d: date, closes: Dict[str, float],
                    highs: Dict[str, float]) -> List[Tuple]:
        """
        检查所有持仓的止损/止盈条件。
        优先级: 硬止损 > 时间止损强制 > 分阶段止盈 > 移动止盈 > 保本线 > 时间止损早期
        返回: [(position, exit_price, reason, partial_shares_or_None), ...]
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

            current_profit = price / pos.entry_price - 1
            pos.peak_profit_pct = pos.peak_price / pos.entry_price - 1
            hold_days = self._trading_days_between(pos.entry_date, d)
            remaining = pos.remaining_shares

            # 1. 硬止损 -9%
            if current_profit <= HARD_STOP:
                sells.append((pos, price, f"硬止损({current_profit*100:.1f}%)", None))
                continue

            # 2. 时间止损: >10天强制清仓
            if hold_days > TIME_MAX_DAYS:
                sells.append((pos, price, f"时间强制({hold_days}天)", None))
                continue

            # 3. 分阶段止盈 (每级仅一次)
            if pos.staged_level < 3 and current_profit >= 0.15 and remaining > 0:
                sells.append((pos, price, f"阶段止盈15%(+{current_profit*100:.1f}%)", None))
                continue

            if pos.staged_level < 2 and current_profit >= 0.10 and remaining > 0:
                sell_shares = int(remaining // 2 // 100 * 100)
                if sell_shares >= 100:
                    sells.append((pos, price, f"阶段止盈10%(+{current_profit*100:.1f}%)", sell_shares))
                    continue

            if pos.staged_level < 1 and current_profit >= 0.05 and remaining > 0:
                sell_shares = int(remaining // 3 // 100 * 100)
                if sell_shares >= 100:
                    sells.append((pos, price, f"阶段止盈5%(+{current_profit*100:.1f}%)", sell_shares))
                    continue

            # 4. 移动止盈: 曾盈利>5%后回撤-3%离场
            if pos.peak_profit_pct >= TRAIL_TRIGGER:
                dd = price / pos.peak_price - 1
                if dd <= -TRAIL_DISTANCE:
                    sells.append((pos, price,
                        f"移动止盈(峰{pos.peak_profit_pct*100:.1f}%回{dd*100:.1f}%)", None))
                    continue

            # 5. 保本线: 曾盈利>3%后跌破成本
            if pos.peak_profit_pct >= BREAKEVEN_PROFIT and current_profit <= 0:
                sells.append((pos, price, f"保本(曾+{pos.peak_profit_pct*100:.1f}%)", None))
                continue

            # 6. 时间止损早期: >7天且盈利>1%
            if hold_days > TIME_EARLY_DAYS and current_profit > TIME_EARLY_PROFIT:
                sells.append((pos, price, f"时间早期({hold_days}天+{current_profit*100:.1f}%)", None))
                continue

        return sells

    def execute_sell(self, pos: Position, exit_price: float, reason: str,
                     partial_shares: Optional[int] = None,
                     exit_date: date = None) -> Trade:
        if partial_shares is not None:
            sell_shares = partial_shares
        else:
            sell_shares = pos.remaining_shares

        sell_shares = int(sell_shares // 100 * 100)
        if sell_shares <= 0:
            sell_shares = pos.remaining_shares

        sell_amount = sell_shares * exit_price
        profit_amount = sell_shares * (exit_price - pos.entry_price)
        return_pct = (exit_price / pos.entry_price - 1) * 100

        pos.remaining_shares -= sell_shares

        # 更新分阶段止盈级别
        if return_pct >= 15:
            pos.staged_level = 3
        elif return_pct >= 10:
            pos.staged_level = max(pos.staged_level, 2)
        elif return_pct >= 5:
            pos.staged_level = max(pos.staged_level, 1)

        if pos.remaining_shares <= 0:
            pos.is_active = False
            pos.remaining_shares = 0

        self.cash += sell_amount

        return Trade(
            code=pos.code,
            entry_date=pos.entry_date,
            exit_date=exit_date or date.today(),
            entry_price=pos.entry_price,
            exit_price=exit_price,
            shares=sell_shares,
            return_pct=return_pct,
            profit_amount=profit_amount,
            exit_reason=reason,
            hold_days=0,
        )

    def execute_buy(self, d: date, code: str, price: float) -> Optional[Position]:
        if code in self.positions:
            return None  # 已在持仓中

        max_amt = min(self.max_buy_amount(), self.cash)
        if max_amt < 5000:
            return None

        shares = int(max_amt / price / 100) * 100
        if shares < 100:
            return None

        cost = shares * price
        if cost > self.cash:
            return None

        pos = Position(
            code=code,
            entry_date=d,
            entry_price=price,
            shares=shares,
            cost=cost,
            peak_price=price,
            remaining_shares=shares,
        )
        self.cash -= cost
        self.positions[code] = pos
        return pos

    def record_equity(self, d: date, prices: Dict[str, float]):
        equity = self.total_equity(prices)
        self.equity_curve.append({
            'date': d,
            'equity': equity,
            'cash': self.cash,
            'positions': self.current_position_count(),
        })


# ── 主流程 ────────────────────────────────────────────────────
def run_backtest():
    print("=" * 70)
    print("  MA5 角度策略 — 完整回测")
    print(f"  区间: {BACKTEST_START} ~ {BACKTEST_END}")
    print(f"  初始资金: {INITIAL_CAPITAL:,}  单票上限: {MAX_PER_STOCK:,}")
    print("=" * 70)

    # 1. 加载
    print(f"\n[1/4] 加载 K 线 {LOAD_START} ~ {BACKTEST_END} ...")
    bars = db.load_all_bars(freq="daily", start=LOAD_START, end=BACKTEST_END)
    bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
    for c in ["open", "high", "low", "close", "volume"]:
        if c in bars:
            bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=["close"])
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    bars = bars.sort_values(["code", "date"])
    print(f"  {bars['code'].nunique():,} 只股票, {len(bars):,} 行")

    # 2. 信号
    print("\n[2/4] 生成信号（RPS=关, 市场宽度=关）...")
    sig = generate_signals(bars,
        version="improved", rps_threshold=0, use_ma_align=True, use_adx=True,
        adx_threshold=20, sh_index_filter=True, vol_threshold=2.0,
        close_position_threshold=0.8, breadth_threshold=0)
    sig = sig[(sig["date"] >= BACKTEST_START) & (sig["date"] <= BACKTEST_END)].copy()
    sig["date"] = pd.to_datetime(sig["date"]).dt.date
    sig = sig.sort_values(["date", "code"])
    print(f"  信号数: {len(sig):,}")

    # 3. 快照
    print("\n[3/4] 构建价格快照 ...")
    bt = bars[(bars["date"] >= BACKTEST_START) & (bars["date"] <= BACKTEST_END)]
    snaps = {}
    for d, g in bt.groupby("date"):
        snaps[d] = {
            'close': dict(zip(g['code'], g['close'])),
            'high':  dict(zip(g['code'], g['high'])),
        }
    trading_dates = sorted(snaps.keys())
    print(f"  交易日: {len(trading_dates):,}")

    # 信号按日索引
    sig_by_date = {}
    for _, r in sig.iterrows():
        d = r['date']
        sig_by_date.setdefault(d, []).append({'code': r['code']})

    # 4. 回测
    print("\n[4/4] 运行回测 ...")
    engine = BacktestEngine(trading_dates)

    for i, d in enumerate(trading_dates):
        closes = snaps[d]['close']
        highs  = snaps[d]['high']

        # Step 1: 检查止损 → 卖出 (用收盘价)
        for pos, exit_price, reason, partial in engine.check_stops(d, closes, highs):
            trade = engine.execute_sell(pos, exit_price, reason, partial, exit_date=d)
            trade.hold_days = engine._trading_days_between(pos.entry_date, d)
            engine.trades.append(trade)

            if trade.return_pct <= 0:
                engine.consecutive_losses += 1
            else:
                engine.consecutive_losses = 0
                engine.pause_until = None

            if engine.consecutive_losses >= LOSS_STREAK_2:
                engine.pause_until = d + timedelta(days=PAUSE_DAYS)

        # 清理
        engine.positions = {k: v for k, v in engine.positions.items() if v.is_active}

        # Step 2: 新信号 → 买入 (收盘价)
        if d in sig_by_date:
            paused = engine.pause_until and d <= engine.pause_until
            bull = engine.is_bull_market(d)
            if not paused and bull:
                for si in sig_by_date[d]:
                    code = si['code']
                    entry_price = closes.get(code)
                    if entry_price is None or entry_price <= 0:
                        continue
                    # 20天无重复
                    if any(t.code == code and t.entry_date >= d - timedelta(days=20)
                           for t in engine.trades):
                        continue
                    engine.execute_buy(d, code, entry_price)

        # Step 3: 记录净值
        engine.record_equity(d, closes)

        if (i + 1) % 50 == 0:
            eq = engine.total_equity(closes)
            print(f"  {d} | {i+1}/{len(trading_dates)} | "
                  f"净值 {eq:,.0f} | 持仓 {engine.current_position_count()} | 现金 {engine.cash:,.0f}")

    # ── 统计 ──────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  回测结果")
    print("=" * 70)

    if not engine.equity_curve:
        print("  无交易!")
        return

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
        from collections import Counter as Ctr
        mc = Ctr(pd.Timestamp(t.entry_date).to_period('M') for t in engine.trades)
        m_agg['trades'] = [mc.get(m, 0) for m in m_agg.index]

    # 交易
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
    else:
        n_total = n_win = n_loss = 0
        wr = avg_w = avg_l = avg_t = pf = avg_hold = total_profit = 0
        exit_dist = Counter()

    # 月度均衡
    monthly_trades = [m_agg['trades'].iloc[i] for i in range(len(m_agg))] if len(m_agg) > 0 else []
    avg_mt = np.mean(monthly_trades) if monthly_trades else 0
    std_mt = np.std(monthly_trades) if monthly_trades else 0
    cv = std_mt / avg_mt if avg_mt > 0 else 999

    # ── 打印 ──────────────────────────────────────────────────
    print(f"\n  [整体]  初始 {INITIAL_CAPITAL:,} → 最终 {final_eq:,.0f} | 收益 {total_ret:+.2f}% | 回撤 {max_dd:.2f}% | PF {pf:.2f}")
    print(f"  [交易]  共 {n_total:,}笔 | 盈 {n_win:,}/亏 {n_loss:,} | 胜率 {wr:.1f}%")
    print(f"          均盈 {avg_w:+.2f}% | 均亏 {avg_l:+.2f}% | 均笔 {avg_t:+.2f}% | 均持 {avg_hold:.1f}天")
    print(f"          总盈利 {total_profit:+,.0f}")
    print(f"  [月度]  月均交易 {avg_mt:.1f} | 标准差 {std_mt:.1f} | CV {cv:.2f}")

    print(f"\n  [止损分布]")
    for reason, count in exit_dist.most_common():
        print(f"    {reason:<30} {count:>5} ({count/n_total*100:5.1f}%)" if n_total else f"    {reason:<30} {count:>5}")

    print(f"\n  [月度表现]")
    print(f"  {'月份':<10} {'收益%':>8} {'回撤%':>8} {'交易':>6}")
    print(f"  {'-'*34}")
    for idx, row in m_agg.iterrows():
        print(f"  {str(idx):<10} {row['ret']:>+7.2f} {row['dd']:>7.2f} {int(row['trades']):>6}")

    # 保存
    out = Path(__file__).parent.parent / "output"
    out.mkdir(exist_ok=True)
    eq_df.to_parquet(str(out / "backtest_equity.parquet"), index=False)
    if trades:
        pd.DataFrame([{
            'code':t.code, 'entry':str(t.entry_date), 'exit':str(t.exit_date),
            'entry_px':t.entry_price, 'exit_px':t.exit_price, 'shares':t.shares,
            'ret_pct':t.return_pct, 'profit':t.profit_amount, 'reason':t.exit_reason,
            'hold_days':t.hold_days,
        } for t in trades]).to_parquet(str(out / "backtest_trades.parquet"), index=False)
    print(f"\n  结果已保存至 output/")

    return eq_df, trades, m_agg


if __name__ == "__main__":
    run_backtest()
