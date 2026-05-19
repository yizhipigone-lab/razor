"""Audit backtest trades against raw daily data"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.backtest.simple_runner import run_backtest, load_daily_bars
from datetime import date
import pandas as pd
import random

params = {
    'initial_capital': 1_000_000, 'position_size': 50_000, 'min_buy_amt': 5_000,
    'hard_stop': -0.06, 'tp1_pct': 0.04, 'tp1_sell_ratio': 0.15, 'tp2_pct': 0.16,
    'trail_activate': 0.03, 'trail_dd': 0.01,
    'time_exit_days': 3, 'time_exit_profit': 0.03, 'time_force_days': 9,
    'loss_streak_halve': 3, 'loss_streak_pause': 5, 'pause_days': 3,
    'same_stock_cooldown': 20,
    'signal_params': {
        "version": "improved", "filter_st": True, "filter_bj": True,
        "vol_threshold": 1.5, "close_position_threshold": 0.8,
        "disable_quality_sort": False,
        "filter_consecutive_up": False, "filter_gap_quality": False,
    },
    'start_date': date(2026, 1, 1), 'end_date': date(2026, 5, 12),
}

result = run_backtest(params)
trades = result['trades']

bars = load_daily_bars(date(2026, 1, 1), date(2026, 5, 12))
bars['date'] = pd.to_datetime(bars['date']).dt.date

random.seed(42)
sample = random.sample(trades, min(20, len(trades)))

print("=== Random 20 trade audit ===\n")
errors = 0
for i, t in enumerate(sample):
    code = t['code']
    entry_d = pd.Timestamp(t['entry_date']).date()
    exit_d = pd.Timestamp(t['exit_date']).date()
    entry_bar = bars[(bars['code'] == code) & (bars['date'] == entry_d)]
    exit_bar = bars[(bars['code'] == code) & (bars['date'] == exit_d)]
    entry_ok = len(entry_bar) > 0 and abs(entry_bar.iloc[0]['close'] - t['entry_px']) < 0.02
    exit_ok = len(exit_bar) > 0 and abs(exit_bar.iloc[0]['close'] - t['exit_px']) < 0.02
    actual_entry = entry_bar.iloc[0]['close'] if len(entry_bar) > 0 else 'N/A'
    actual_exit = exit_bar.iloc[0]['close'] if len(exit_bar) > 0 else 'N/A'
    marker = "OK" if (entry_ok and exit_ok) else "ERR"
    print(f"{marker} {code} buy {t['entry_date']}@{t['entry_px']}(actual:{actual_entry}) -> sell {t['exit_date']}@{t['exit_px']}(actual:{actual_exit}) {t['ret_pct']}% reason:{t['reason']} hold:{t['hold_days']}d")
    if not entry_ok or not exit_ok:
        errors += 1

print(f"\n=== Audit result: {errors}/{len(sample)} mismatches ===")

# Quick win stats
tp1_trades = [t for t in trades if t['reason'].startswith('TP1')]
tr_trades = [t for t in trades if t['reason'].startswith('TR')]
quick_wins = [t for t in trades if t['hold_days'] <= 2 and t['ret_pct'] > 0]
print(f"\nTP1 exits: {len(tp1_trades)}, avg hold {sum(t['hold_days'] for t in tp1_trades)/max(len(tp1_trades),1):.1f}d")
print(f"TR exits: {len(tr_trades)}, avg hold {sum(t['hold_days'] for t in tr_trades)/max(len(tr_trades),1):.1f}d")
print(f"Quick wins (<=2d): {len(quick_wins)} ({len(quick_wins)/max(len(trades),1)*100:.1f}%)")

# Check entry/exit price = daily close
print("\n=== 100 random trades: price vs actual close ===")
mismatches_entry = 0
mismatches_exit = 0
for t in random.sample(trades, min(100, len(trades))):
    eb = bars[(bars['code'] == t['code']) & (bars['date'] == pd.Timestamp(t['entry_date']).date())]
    xb = bars[(bars['code'] == t['code']) & (bars['date'] == pd.Timestamp(t['exit_date']).date())]
    if len(eb) > 0 and abs(eb.iloc[0]['close'] - t['entry_px']) > 0.02:
        mismatches_entry += 1
    if len(xb) > 0 and abs(xb.iloc[0]['close'] - t['exit_px']) > 0.02:
        mismatches_exit += 1
print(f"Entry mismatch: {mismatches_entry}, Exit mismatch: {mismatches_exit}")

# Check: what % of buy signals result in immediate (next day) profitability?
print("\n=== Signal effectiveness check ===")
# Count trades where the stock rose vs fell on the day AFTER entry
up_next = 0
down_next = 0
for t in random.sample(trades, min(200, len(trades))):
    eb = bars[(bars['code'] == t['code']) & (bars['date'] == pd.Timestamp(t['entry_date']).date())]
    if len(eb) == 0: continue
    entry_idx = eb.index[0]
    # Find next bar for this stock
    stock_bars = bars[bars['code'] == t['code']].sort_values('date')
    next_day = stock_bars[stock_bars['date'] > pd.Timestamp(t['entry_date']).date()]
    if len(next_day) == 0: continue
    next_close = next_day.iloc[0]['close']
    entry_close = eb.iloc[0]['close']
    if next_close > entry_close:
        up_next += 1
    else:
        down_next += 1
print(f"Next day up: {up_next}, down: {down_next}, up ratio: {up_next/max(up_next+down_next,1)*100:.1f}%")
