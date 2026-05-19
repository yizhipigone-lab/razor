"""五分钟线盘中回测 — 与日线回测共用信号，但止损按5分钟K线触发"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
from pathlib import Path
from collections import defaultdict, Counter

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
MIN5_DIR  = ROOT / "data" / "parquet" / "min5"

# ── 参数 ────────────────────────────────────
PARAMS = {
    'initial_capital': 1_000_000, 'position_size': 50_000, 'min_buy_amt': 5_000,
    'hard_stop': -0.06, 'tp1_pct': 0.04, 'tp1_sell_ratio': 0.15, 'tp2_pct': 0.16,
    'trail_activate': 0.03, 'trail_dd': 0.01,
    'time_exit_days': 3, 'time_exit_profit': 0.03, 'time_force_days': 9,
    'loss_streak_halve': 3, 'loss_streak_pause': 5, 'pause_days': 3,
    'same_stock_cooldown': 20,
    'start_date': date(2026, 1, 1), 'end_date': date(2026, 5, 12),
}
SIGNAL_PARAMS = {
    "version": "improved", "filter_st": True, "filter_bj": True,
    "vol_threshold": 1.5, "close_position_threshold": 0.8,
    "disable_quality_sort": False,
    "filter_consecutive_up": False, "filter_gap_quality": False,
}

print("Loading daily bars for signals...")
from app.backtest.simple_runner import load_daily_bars
bars = load_daily_bars(date(2022, 6, 1), PARAMS['end_date'])

print("Generating signals...")
from app.screener.strategies.ma5_angle import generate_signals
sig = generate_signals(bars, **SIGNAL_PARAMS)
sig = sig[(sig['date'] >= PARAMS['start_date']) & (sig['date'] <= PARAMS['end_date'])].copy()
sig['date'] = pd.to_datetime(sig['date']).dt.date

# Build trading dates list
bt = bars[(bars['date'] >= PARAMS['start_date']) & (bars['date'] <= PARAMS['end_date'])]
td_list = sorted(bt['date'].unique())

# Group signals by date
sig_by_date = defaultdict(list)
for _, r in sig.iterrows():
    sig_by_date[r['date']].append((r['code'], float(r['close'])))

# Pre-load 5-min data for signal stocks
signal_codes = set(sig['code'].unique())
print(f"Loading 5-min data for {len(signal_codes)} signal stocks...")
min5_data = {}
for code in signal_codes:
    fp = MIN5_DIR / f"{code}.parquet"
    if not fp.exists():
        continue
    try:
        df = pd.read_parquet(str(fp), columns=['datetime', 'open', 'high', 'low', 'close'])
        df['datetime'] = pd.to_datetime(df['datetime'])
        df = df.sort_values('datetime')
        # Filter to backtest period + buffer
        df = df[(df['datetime'] >= pd.Timestamp(PARAMS['start_date'])) &
                (df['datetime'] < pd.Timestamp(PARAMS['end_date']) + timedelta(days=10))]
        if len(df) > 0:
            min5_data[code] = df
    except Exception:
        pass
print(f"Loaded 5-min data for {len(min5_data)} stocks")

# ── 引擎 ────────────────────────────────────
class IntradayPosition:
    __slots__ = ('code','entry_date','entry_price','shares','cost','peak_price',
                 'remaining','tp1','tp2','active','entry_bar_idx')
    def __init__(self, c, d, px, sh, cost):
        self.code=c; self.entry_date=d; self.entry_price=px; self.shares=sh
        self.cost=cost; self.peak_price=px; self.remaining=sh
        self.tp1=False; self.tp2=False; self.active=True; self.entry_bar_idx=0

class IntradayEngine:
    def __init__(self, params):
        self.cash = params['initial_capital']
        self.position_size = params['position_size']
        self.min_buy = params.get('min_buy_amt', 5000)
        self.positions = {}
        self.trades = []
        self.equity = []
        self.cl = 0
        self.pause = None
        self.p = params

    def max_pos(self):
        if self.cl >= self.p.get('loss_streak_halve', 3):
            return self.position_size / 2
        return self.position_size

    def pos_n(self):
        return sum(1 for p in self.positions.values() if p.active)

    def eq(self, prices):
        pv = 0
        for p in self.positions.values():
            if not p.active: continue
            px = prices.get(p.code, p.entry_price)
            pv += p.remaining * px
        return self.cash + pv

    def buy(self, d, code, px):
        if code in self.positions: return None
        ma = min(self.max_pos(), self.cash)
        if ma < self.min_buy: return None
        sh = int(ma / px / 100) * 100
        if sh < 100: return None
        cost = sh * px
        if cost > self.cash: return None
        p = IntradayPosition(code, d, px, sh, cost)
        self.cash -= cost
        self.positions[code] = p
        return p

    def check_stop(self, pos, bar, d, prev_close=None):
        cp = bar['close']; hp = bar['high']
        if pd.isna(cp) or cp <= 0: return None
        if not pd.isna(hp) and hp > pos.peak_price: pos.peak_price = hp
        pp = pos.peak_price / pos.entry_price - 1
        cur = cp / pos.entry_price - 1
        hd = sum(1 for td in td_list if pos.entry_date <= td <= d)

        # 除权保护
        if prev_close and prev_close > 0 and cp / prev_close - 1 <= -0.20:
            ratio = cp / prev_close
            pos.entry_price *= ratio
            pos.peak_price *= ratio
            cur = cp / pos.entry_price - 1

        hs = self.p['hard_stop']; tp1p = self.p['tp1_pct']
        tp1r = self.p['tp1_sell_ratio']; tp2p = self.p['tp2_pct']
        ta = self.p['trail_activate']; td_p = self.p['trail_dd']
        te = self.p['time_exit_days']; tep = self.p['time_exit_profit']
        tf = self.p['time_force_days']

        if cur <= hs: return (cp, "HS")
        if hd > tf: return (cp, "TF")
        if not pos.tp2 and cur >= tp2p: return (cp, "TP2")
        if not pos.tp1 and cur >= tp1p:
            ss = int(pos.remaining * tp1r / 100) * 100
            if ss >= 100: return (cp, "TP1", ss)
        if pp >= ta:
            dd = cp / pos.peak_price - 1
            if dd <= -td_p: return (cp, "TR")
        if hd > te and cur > tep: return (cp, "TC")
        return None

    def execute_sell(self, pos, px, reason, partial=None, exit_d=None, exit_dt=None):
        ss = partial if partial else pos.remaining
        ss = int(ss // 100 * 100)
        if ss <= 0: return None
        ret = (px / pos.entry_price - 1) * 100
        profit = ss * (px - pos.entry_price)
        pos.remaining -= ss
        if "TP2" in reason: pos.tp2 = True
        if "TP1" in reason: pos.tp1 = True
        if pos.remaining <= 0: pos.active = False; pos.remaining = 0
        self.cash += ss * px
        return {
            'code': pos.code, 'entry_date': str(pos.entry_date),
            'exit_date': str(exit_d or date.today()),
            'exit_time': str(exit_dt) if exit_dt else '',
            'entry_px': round(float(pos.entry_price), 2),
            'exit_px': round(float(px), 2),
            'shares': int(ss),
            'ret_pct': round(float(ret), 2),
            'profit': round(float(profit), 0),
            'reason': reason, 'hold_days': 0,
        }

# ── 主循环 ──────────────────────────────────
print(f"Running intraday backtest over {len(td_list)} trading days...")
eng = IntradayEngine(PARAMS)
cooldown = PARAMS['same_stock_cooldown']
all_trades = []

for day_idx, d in enumerate(td_list):
    if day_idx % 20 == 0:
        print(f"  Day {day_idx+1}/{len(td_list)}: {d}, pos={eng.pos_n()}, cash={eng.cash:,.0f}")

    # Determine entry bar: 14:50 (last available 5-min bar before close)
    entry_hour, entry_min = 14, 50

    # ── Sell phase: check every 5-min bar from open to 14:50 ──
    prev_close_map = {}

    # Get all relevant stocks' 5-min data for today
    stocks_to_check = list(eng.positions.keys())
    for code in stocks_to_check:
        if code not in min5_data: continue
        df = min5_data[code]
        today_bars = df[df['datetime'].dt.date == d]
        if len(today_bars) == 0: continue

        prev_close = None
        # Get previous day's last close for gap detection
        prev_day = df[df['datetime'].dt.date < d]
        if len(prev_day) > 0:
            prev_close = prev_day.iloc[-1]['close']

        for _, bar in today_bars.iterrows():
            bar_time = bar['datetime'].time()
            if bar_time > pd.Timestamp(f"{entry_hour}:{entry_min}:00").time():
                break  # Don't check after entry time

            pos = eng.positions.get(code)
            if not pos or not pos.active: break

            result = eng.check_stop(pos, bar, d, prev_close)
            if prev_close is not None:
                prev_close = bar['close']

            if result:
                reason = result[1]
                partial = result[2] if len(result) > 2 else None
                px = result[0]
                t = eng.execute_sell(pos, px, reason, partial, exit_d=d, exit_dt=bar['datetime'])
                if t:
                    t['hold_days'] = (d - pos.entry_date).days
                    all_trades.append(t)
                    if t['ret_pct'] <= 0:
                        eng.cl += 1
                    else:
                        eng.cl = 0; eng.pause = None
                    if eng.cl >= PARAMS['loss_streak_pause']:
                        eng.pause = d + timedelta(days=PARAMS['pause_days'])
                    if not pos.active:
                        break

    # Clean up closed positions
    eng.positions = {k: v for k, v in eng.positions.items() if v.active}

    # ── Buy phase: at 14:50 ──
    paused = eng.pause is not None and d <= eng.pause

    if d in sig_by_date and not paused:
        # Get 14:50 prices for all signal stocks
        for code, _ in sig_by_date[d]:
            if eng.cash < min(eng.max_pos(), eng.min_buy):
                break
            if any(t.get('code') == code and (d - pd.Timestamp(t['entry_date']).date()).days <= cooldown
                   for t in all_trades):
                continue

            # Get 14:50 price from 5-min data
            if code not in min5_data: continue
            df = min5_data[code]
            today_bars = df[df['datetime'].dt.date == d]
            if len(today_bars) == 0: continue

            # Find closest bar to 14:50
            target_time = pd.Timestamp(f"{d} {entry_hour}:{entry_min}:00")
            before = today_bars[today_bars['datetime'] <= target_time]
            if len(before) == 0: continue
            entry_bar = before.iloc[-1]
            px = float(entry_bar['close'])

            if eng.buy(d, code, px):
                pass

    # Record equity using last available prices
    eod_prices = {}
    for code in eng.positions:
        if code in min5_data:
            df = min5_data[code]
            today = df[df['datetime'].dt.date == d]
            if len(today) > 0:
                eod_prices[code] = float(today.iloc[-1]['close'])
    eng.equity.append({
        'date': str(d), 'equity': round(eng.eq(eod_prices), 2),
        'cash': round(eng.cash, 2), 'pos': eng.pos_n()
    })

# ── 汇总 ────────────────────────────────────
eq = pd.DataFrame(eng.equity)
if eq.empty or not all_trades:
    print("No trades!")
    exit()

fe = eq['equity'].iloc[-1]
total_ret = (fe / PARAMS['initial_capital'] - 1) * 100
eq['cmax'] = eq['equity'].cummax()
eq['dd'] = (eq['equity'] - eq['cmax']) / eq['cmax'] * 100
max_dd = float(eq['dd'].min())

n = len(all_trades)
wins = [t for t in all_trades if t['ret_pct'] > 0]
loses = [t for t in all_trades if t['ret_pct'] <= 0]
nw, nl = len(wins), len(loses)
wr = nw / n * 100 if n > 0 else 0
aw = np.mean([t['ret_pct'] for t in wins]) if wins else 0
al = np.mean([t['ret_pct'] for t in loses]) if loses else 0
profit_wins = sum(t['profit'] for t in wins)
profit_loses = abs(sum(t['profit'] for t in loses))
pf = profit_wins / profit_loses if profit_loses != 0 else 0

# Annualized return
trading_span = (td_list[-1] - td_list[0]).days
ann_ret = (1 + total_ret/100) ** (365/max(trading_span, 1)) - 1
calmar = ann_ret / abs(max_dd/100) if max_dd != 0 else 0

# Sharpe
eq['daily_ret'] = eq['equity'].pct_change()
daily_rets = eq['daily_ret'].dropna()
rf_daily = 0.02 / 252
excess = daily_rets - rf_daily
sharpe = float(np.sqrt(252) * excess.mean() / excess.std()) if excess.std() > 0 else 0

# Sortino
downside = daily_rets[daily_rets < 0]
downside_std = downside.std() if len(downside) > 0 else 0
sortino = float(np.sqrt(252) * excess.mean() / downside_std) if downside_std > 0 else 0

rc = Counter(t['reason'] for t in all_trades)

print(f"\n=== 五分钟线盘中回测结果 ===")
print(f"区间: {PARAMS['start_date']} ~ {PARAMS['end_date']} ({len(td_list)}天)")
print(f"总收益: {total_ret:.2f}%")
print(f"最大回撤: {max_dd:.2f}%")
print(f"胜率: {wr:.1f}%")
print(f"交易: {n} 笔 (赢{nw} 亏{nl})")
print(f"盈利因子: {pf:.2f}")
print(f"均盈: +{aw:.2f}%  均亏: {al:.2f}%")
print(f"夏普: {sharpe:.2f}  卡玛: {calmar:.2f}  索提诺: {sortino:.2f}")
print(f"年化收益: {ann_ret*100:.2f}%")
print(f"退出分布: {dict(rc.most_common())}")
print(f"净值: {PARAMS['initial_capital']:,} -> {fe:,.0f}")
