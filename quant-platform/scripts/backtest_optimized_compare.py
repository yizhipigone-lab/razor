#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度策略 — 优化版回测（多方案对比）
在基线回测基础上测试多种优化方案
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
import time
import warnings
warnings.filterwarnings('ignore')

from database.duckdb_manager import db, PARQUET_DAILY_DIR
from app.screener.strategies.ma5_angle import generate_signals

# ═══════════════════════════════════════════════════════════════
INITIAL_CAPITAL = 1_000_000
BACKTEST_START = date(2023, 1, 1)
BACKTEST_END   = date(2026, 5, 2)
BUFFER_DAYS    = 365
LOAD_START     = BACKTEST_START - timedelta(days=BUFFER_DAYS)

BASE_SIGNAL_PARAMS = {
    "version": "improved",
    "filter_st": True,
    "filter_bj": True,
    "sh_red_filter": True,
    "vol_threshold": 1.5,
    "close_position_threshold": 0.8,
    "disable_quality_sort": True,
    "filter_consecutive_up": False,
    "filter_gap_quality": False,
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


class BacktestEngine:
    """可配置策略参数的回测引擎"""
    def __init__(self, trading_dates, sh_index, config=None):
        self.cash = INITIAL_CAPITAL
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[Dict] = []
        self.consecutive_losses = 0
        self.pause_until = None
        self.trading_dates = trading_dates
        self.sh_index = sh_index

        # 默认配置 = 基线
        c = config or {}
        self.hard_stop = c.get('hard_stop', -0.055)
        self.tp1_pct = c.get('tp1_pct', 0.04)
        self.tp1_ratio = c.get('tp1_ratio', 0.20)
        self.tp2_pct = c.get('tp2_pct', 0.14)
        self.trail_activate = c.get('trail_activate', 0.08)
        self.trail_dd = c.get('trail_dd', 0.02)
        self.time_exit = c.get('time_exit', 7)
        self.time_force = c.get('time_force', 10)
        self.position_cap = c.get('position_cap', 50000)
        self.min_buy = c.get('min_buy', 5000)
        self.use_market_filter = c.get('use_market_filter', False)
        self.use_atr_filter = c.get('use_atr_filter', False)
        self.use_dynamic_position = c.get('use_dynamic_position', False)
        self.use_sector_filter = c.get('use_sector_filter', False)
        self.loss_streak_1 = c.get('loss_streak_1', 3)
        self.loss_streak_2 = c.get('loss_streak_2', 5)
        self.pause_days = c.get('pause_days', 3)
        self.name = c.get('name', 'baseline')

        # 加载ATR数据（如果需要）
        self.atr_data = self._load_atr() if self.use_atr_filter else {}

    def _load_atr(self) -> dict:
        """预加载所有股票的ATR(14)"""
        return {}  # 后续实现

    def market_state(self, d: date) -> str:
        """判断市场状态: bull / neutral / bear"""
        if self.sh_index.empty:
            return 'neutral'
        row = self.sh_index[self.sh_index['date'] == d]
        if row.empty:
            return 'neutral'
        r = row.iloc[0]
        close, ma20, ma60 = float(r['close']), float(r.get('ma20', close)), float(r.get('ma60', close))
        if pd.isna(ma20) or pd.isna(ma60):
            return 'neutral'
        if close >= ma20 and ma20 >= ma60:
            return 'bull'
        elif close < ma60:
            return 'bear'
        return 'neutral'

    def total_equity(self, prices):
        pos_val = sum(p.remaining_shares * prices.get(p.code, p.entry_price)
                      for p in self.positions.values() if p.is_active)
        return self.cash + pos_val

    def position_count(self):
        return len([p for p in self.positions.values() if p.is_active])

    def max_position(self):
        base = self.position_cap
        if self.consecutive_losses >= self.loss_streak_1:
            base = base / 2
        return base

    def _td_between(self, d1, d2):
        return len([td for td in self.trading_dates if d1 <= td <= d2])

    def check_stops(self, d, closes, highs):
        sells = []
        market = self.market_state(d) if self.use_market_filter else 'neutral'

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

            cp = price / pos.entry_price - 1
            hd = self._td_between(pos.entry_date, d)
            rem = pos.remaining_shares

            # 1. 硬止损（熊市收紧）
            hs = self.hard_stop
            if market == 'bear':
                hs = self.hard_stop * 1.3  # 熊市收紧30%
            if cp <= hs:
                sells.append((pos, price, f"硬止损({cp*100:.1f}%)", None))
                continue

            # 2. 时间强制
            if hd > self.time_force:
                sells.append((pos, price, f"时间强制({hd}天)", None))
                continue

            # 3. TP2
            if not pos.tp2_triggered and cp >= self.tp2_pct:
                sells.append((pos, price, f"TP2 +{self.tp2_pct*100:.0f}%({cp*100:.1f}%)", None))
                continue

            # 4. TP1
            if not pos.tp1_triggered and cp >= self.tp1_pct:
                ss = int(rem * self.tp1_ratio / 100) * 100
                if ss >= 100:
                    sells.append((pos, price, f"TP1 +{self.tp1_pct*100:.0f}%({cp*100:.1f}%)", ss))
                    continue

            # 5. 移动止盈
            if pos.peak_profit_pct >= self.trail_activate:
                dd = price / pos.peak_price - 1
                if dd <= -self.trail_dd:
                    tp = pos.peak_price * (1 - self.trail_dd)
                    sells.append((pos, tp, f"移动止盈(峰{pos.peak_profit_pct*100:.1f}%回{dd*100:.1f}%)", None))
                    continue

            # 6. 时间条件（熊市更早退出）
            te = self.time_exit
            if market == 'bear':
                te = max(3, self.time_exit - 2)
            if hd > te and cp > 0.01:
                sells.append((pos, price, f"时间条件({hd}天+{cp*100:.1f}%)", None))
                continue

        return sells

    def execute_sell(self, pos, exit_price, reason, partial=None, exit_date=None):
        ss = partial if partial else pos.remaining_shares
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
        return Trade(pos.code, pos.entry_date, exit_date or date.today(),
                     pos.entry_price, exit_price, ss, rp, profit, reason, 0)

    def execute_buy(self, d, code, price):
        if code in self.positions:
            return None
        max_amt = min(self.max_position(), self.cash)
        if max_amt < self.min_buy:
            return None
        shares = int(max_amt / price / 100) * 100
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
        self.equity_curve.append({'date': d, 'equity': eq, 'cash': self.cash,
                                  'positions': self.position_count()})


# ═══════════════════════════════════════════════════════════════
#  优化方案定义
# ═══════════════════════════════════════════════════════════════
# 每个方案是一个 config dict，覆盖基线的参数

CONFIGS = {
    "baseline": {
        "name": "1.基线(当前策略)",
    },
    "market_filter": {
        "name": "2.市场状态过滤",
        "use_market_filter": True,
        "use_dynamic_position": True,  # 牛市满仓/熊市半仓
    },
    "tighter_exit": {
        "name": "3.收紧退出",
        "hard_stop": -0.045,        # 从-5.5%收紧到-4.5%
        "time_exit": 5,             # 从7天提前到5天
        "time_force": 8,            # 从10天提前到8天
        "trail_activate": 0.06,     # 从8%降到6%
        "trail_dd": 0.025,          # 从2%放宽到2.5%
    },
    "better_tp": {
        "name": "4.优化止盈",
        "tp1_pct": 0.05,            # 从4%→5%
        "tp1_ratio": 0.30,          # 从20%→30%
        "tp2_pct": 0.12,            # 从14%→12%
        "trail_activate": 0.07,     # 从8%→7%
        "trail_dd": 0.025,          # 从2%→2.5%
    },
    "combo_aggressive": {
        "name": "5.组合优化(激进)",
        "use_market_filter": True,
        "use_dynamic_position": True,
        "hard_stop": -0.045,
        "time_exit": 5,
        "time_force": 8,
        "tp1_pct": 0.05,
        "tp1_ratio": 0.30,
        "tp2_pct": 0.12,
        "trail_activate": 0.06,
        "trail_dd": 0.025,
        "loss_streak_1": 2,         # 更早降仓
        "loss_streak_2": 4,
        "pause_days": 5,
    },
    "combo_conservative": {
        "name": "6.组合优化(稳健)",
        "use_market_filter": True,
        "use_dynamic_position": True,
        "hard_stop": -0.04,         # 更紧的止损
        "time_exit": 5,
        "time_force": 7,
        "tp1_pct": 0.04,
        "tp1_ratio": 0.25,
        "tp2_pct": 0.10,            # 更早清仓
        "trail_activate": 0.05,
        "trail_dd": 0.02,
        "position_cap": 40000,      # 降低单票仓位
        "loss_streak_1": 2,
        "loss_streak_2": 4,
        "pause_days": 5,
    },
}


def load_sh_index():
    sh_path = PARQUET_DAILY_DIR / "index_000001.parquet"
    if not sh_path.exists():
        return pd.DataFrame()
    sh = pd.read_parquet(str(sh_path))
    sh['date'] = pd.to_datetime(sh['date']).dt.date
    sh = sh.sort_values('date')
    sh['ma20'] = sh['close'].rolling(20).mean()
    sh['ma60'] = sh['close'].rolling(60).mean()
    return sh


def run_config(config, bars, bt_bars, trading_dates, snaps, sig_by_date):
    """运行单个配置的回测"""
    engine = BacktestEngine(trading_dates, load_sh_index(), config)

    for d in trading_dates:
        closes = snaps[d]['close']
        highs = snaps[d]['high']

        # 卖出
        for pos, exit_price, reason, partial in engine.check_stops(d, closes, highs):
            trade = engine.execute_sell(pos, exit_price, reason, partial, exit_date=d)
            if trade:
                trade.hold_days = engine._td_between(pos.entry_date, d)
                engine.trades.append(trade)
                if trade.return_pct <= 0:
                    engine.consecutive_losses += 1
                else:
                    engine.consecutive_losses = 0
                    engine.pause_until = None
                if engine.consecutive_losses >= engine.loss_streak_2:
                    engine.pause_until = d + timedelta(days=engine.pause_days)

        engine.positions = {k: v for k, v in engine.positions.items() if v.is_active}

        # 买入
        if d in sig_by_date:
            paused = engine.pause_until is not None and d <= engine.pause_until
            if not paused:
                # 市场过滤
                if engine.use_market_filter:
                    ms = engine.market_state(d)
                    if ms == 'bear':
                        continue  # 熊市不买

                active = sig_by_date[d]
                if active and 'quality' in active[0]:
                    active.sort(key=lambda x: x['quality'], reverse=True)

                # 动态仓位
                max_pos = int(engine.cash / engine.max_position()) + 1
                for si in active[:max_pos]:
                    code = si['code']
                    ep = closes.get(code)
                    if ep is None or ep <= 0:
                        continue
                    if any(t.code == code and d - t.entry_date <= timedelta(days=20)
                           for t in engine.trades):
                        continue
                    engine.execute_buy(d, code, ep)

        engine.record_equity(d, closes)

    return engine


def compute_stats(engine, config_name):
    """计算回测统计"""
    if not engine.equity_curve:
        return {'name': config_name, 'error': '无交易'}
    eq_df = pd.DataFrame(engine.equity_curve)
    fe = eq_df['equity'].iloc[-1]
    tr = (fe / INITIAL_CAPITAL - 1) * 100
    eq_df['cummax'] = eq_df['equity'].cummax()
    eq_df['dd'] = (eq_df['equity'] - eq_df['cummax']) / eq_df['cummax'] * 100
    md = eq_df['dd'].min()

    # 年化
    days = (BACKTEST_END - BACKTEST_START).days
    ann = ((fe / INITIAL_CAPITAL) ** (365.25 / days) - 1) * 100 if days > 0 else 0

    # 夏普比率（简化）
    eq_df['daily_ret'] = eq_df['equity'].pct_change()
    sharpe = eq_df['daily_ret'].mean() / eq_df['daily_ret'].std() * np.sqrt(252) if eq_df['daily_ret'].std() > 0 else 0

    # 卡玛比率
    calmar = ann / abs(md) if md != 0 else 0

    trades = engine.trades
    if not trades:
        return {'name': config_name, 'total_ret': tr, 'max_dd': md, 'trades': 0}

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

    # 月度统计
    eq_df['month'] = pd.to_datetime(eq_df['date']).dt.to_period('M')
    m_agg = eq_df.groupby('month').agg(start=('equity','first'), end=('equity','last'))
    m_agg['ret'] = (m_agg['end'] / m_agg['start'] - 1) * 100
    pos_months = (m_agg['ret'] > 0).sum()
    neg_months = (m_agg['ret'] <= 0).sum()
    avg_win_month = m_agg[m_agg['ret'] > 0]['ret'].mean() if pos_months > 0 else 0
    avg_loss_month = m_agg[m_agg['ret'] <= 0]['ret'].mean() if neg_months > 0 else 0
    max_win_month = m_agg['ret'].max()
    max_loss_month = m_agg['ret'].min()

    # 最大连续盈利/亏损
    eq_df['win_streak'] = (eq_df['daily_ret'] > 0).astype(int)
    # 简单起见用月度的

    return {
        'name': config_name,
        'final_eq': fe,
        'total_ret': tr,
        'ann_ret': ann,
        'max_dd': md,
        'sharpe': sharpe,
        'calmar': calmar,
        'trades': n,
        'win_trades': nw,
        'loss_trades': nl,
        'win_rate': wr,
        'avg_win': aw,
        'avg_loss': al,
        'avg_trade': at_,
        'med_trade': med,
        'profit_factor': pf,
        'total_profit': tp,
        'avg_hold': ah,
        'pos_months': pos_months,
        'neg_months': neg_months,
        'avg_win_month': avg_win_month,
        'avg_loss_month': avg_loss_month,
        'max_win_month': max_win_month,
        'max_loss_month': max_loss_month,
        'exit_dist': ed,
    }


# ═══════════════════════════════════════════════════════════════
def main():
    t0 = time.time()
    print("=" * 80)
    print("  MA5 角度策略 — 多方案优化对比回测")
    print(f"  区间: {BACKTEST_START} ~ {BACKTEST_END}")
    print("=" * 80)

    # ── 加载数据 ──────────────────────────────────────
    print(f"\n[1/4] 加载数据 ...")
    bars = db.load_all_bars(freq="daily", start=LOAD_START, end=BACKTEST_END)
    bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
    for c in ["open", "high", "low", "close", "volume"]:
        if c in bars:
            bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=["close"])
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    bars = bars.sort_values(["code", "date"])
    print(f"  {bars['code'].nunique():,} 只股票, {len(bars):,} 行")

    # ── 信号 ──────────────────────────────────────────
    print(f"\n[2/4] 生成信号 ...")
    sig = generate_signals(bars, **BASE_SIGNAL_PARAMS)
    sig = sig[(sig["date"] >= BACKTEST_START) & (sig["date"] <= BACKTEST_END)].copy()
    sig["date"] = pd.to_datetime(sig["date"]).dt.date
    sig = sig.sort_values(["date", "code"])
    print(f"  信号: {len(sig):,}")

    # ── 快照 ──────────────────────────────────────────
    bt_bars = bars[(bars["date"] >= BACKTEST_START) & (bars["date"] <= BACKTEST_END)]
    snaps = {}
    for d, g in bt_bars.groupby("date"):
        snaps[d] = {'close': dict(zip(g['code'], g['close'])),
                     'high': dict(zip(g['code'], g['high']))}
    trading_dates = sorted(snaps.keys())

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

    # ── 运行所有方案 ──────────────────────────────────
    print(f"\n[3/4] 运行 {len(CONFIGS)} 个方案 ...")
    results = []
    for key, cfg in CONFIGS.items():
        print(f"  {cfg['name']} ...", end=' ')
        t1 = time.time()
        engine = run_config(cfg, bars, bt_bars, trading_dates, snaps, sig_by_date)
        stats = compute_stats(engine, cfg['name'])
        stats['runtime'] = time.time() - t1
        results.append(stats)
        if 'error' not in stats:
            print(f"收益{stats['total_ret']:+.1f}% DD{stats['max_dd']:.1f}% WR{stats['win_rate']:.1f}% PF{stats['profit_factor']:.2f}")
        else:
            print(stats['error'])

    # ── 输出对比表 ────────────────────────────────────
    print(f"\n[4/4] 方案对比")
    print("\n" + "=" * 100)
    print(f"{'方案':<24} {'收益%':>8} {'年化%':>8} {'回撤%':>8} {'夏普':>7} {'卡玛':>7} {'胜率%':>7} {'PF':>6} {'交易':>6} {'均持':>5}")
    print("=" * 100)
    baseline = results[0] if results else None
    for r in results:
        if 'error' in r:
            print(f"{r['name']:<24} {'ERROR':>8}")
            continue
        print(f"{r['name']:<24} {r['total_ret']:>+7.2f} {r['ann_ret']:>+7.2f} {r['max_dd']:>7.2f} "
              f"{r['sharpe']:>6.2f} {r['calmar']:>6.2f} {r['win_rate']:>6.1f} {r['profit_factor']:>5.2f} "
              f"{r['trades']:>6} {r['avg_hold']:>4.1f}d")
    print("=" * 100)

    # 与基线对比
    if baseline and 'error' not in baseline:
        print(f"\n  [与基线对比]")
        print(f"  {'方案':<24} {'收益Δ':>10} {'回撤Δ':>10} {'胜率Δ':>10} {'PFΔ':>10}")
        print(f"  {'-'*60}")
        for r in results[1:]:
            if 'error' in r:
                continue
            rd = r['total_ret'] - baseline['total_ret']
            dd = r['max_dd'] - baseline['max_dd']
            wd = r['win_rate'] - baseline['win_rate']
            pd = r['profit_factor'] - baseline['profit_factor']
            print(f"  {r['name']:<24} {rd:>+9.2f}% {dd:>+9.2f}% {wd:>+9.1f}% {pd:>+9.2f}")

    print(f"\n  总耗时: {time.time()-t0:.0f}s")

    # ── 月度明细对最优方案 ─────────────────────────────
    best = max(results, key=lambda x: x.get('calmar', -999))
    print(f"\n  最优方案(卡玛比率): {best['name']}")
    print(f"  月胜率: {best['pos_months']}/{best['pos_months']+best['neg_months']}"
          f" ({best['pos_months']/(best['pos_months']+best['neg_months'])*100:.0f}%)")
    print(f"  均赢月: {best['avg_win_month']:+.2f}%  均亏月: {best['avg_loss_month']:+.2f}%")
    print(f"  最佳月: {best['max_win_month']:+.2f}%  最差月: {best['max_loss_month']:+.2f}%")

    return results


if __name__ == "__main__":
    main()
