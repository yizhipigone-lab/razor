"""
信号打分分析：量能放量 + 收盘位置 对赢率的影响
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from datetime import date, timedelta
from app.backtest.simple_runner import run_backtest, load_daily_bars
from app.sim_trader.config import (
    INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
    HARD_STOP, TAKE_PROFIT_TIERS, TRAIL_ACTIVATE, TRAIL_DD,
    TIME_EXIT_DAYS, TIME_EXIT_PROFIT, TIME_FORCE_DAYS,
    LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE,
    SAME_STOCK_COOLDOWN, USE_ATR_TRAIL, ATR_TRAIL_MULTIPLIER,
)
import copy

end = date(2026, 5, 20)
start = date(2026, 1, 1)
buf = start - timedelta(days=365)  # extra buffer for volume MA
bars = load_daily_bars(buf, end)

# Precompute volume MA and close position for ALL bars
bars = bars.copy()
bars['_dt'] = pd.to_datetime(bars['date']).dt.date
bars = bars.sort_values(['code', '_dt'])

g = bars.groupby('code', group_keys=False)
bars['vol_ma20'] = g['volume'].transform(lambda x: x.rolling(20).mean())
bars['vol_ma5'] = g['volume'].transform(lambda x: x.rolling(5).mean())
bars['vol_ratio_20'] = bars['volume'] / bars['vol_ma20']
bars['vol_ratio_5'] = bars['volume'] / bars['vol_ma5']

# Close position: (close - low) / (high - low), 0~1
bars['range'] = bars['high'] - bars['low']
bars['close_pos'] = np.where(bars['range'] > 0, (bars['close'] - bars['low']) / bars['range'], 0.5)

# Run backtest
print("Running backtest (TDXv2 strict, 2026)...")
result = run_backtest({
    'strategy_name': 'TDXv2_严格复刻',
    'start_date': '2026-01-01',
    'end_date': str(end),
    'initial_capital': INITIAL_CAPITAL,
    'position_size': POSITION_SIZE, 'min_buy_amt': MIN_BUY_AMT,
    'hard_stop': HARD_STOP, 'trail_activate': TRAIL_ACTIVATE, 'trail_dd': TRAIL_DD,
    'time_exit_days': TIME_EXIT_DAYS, 'time_exit_profit': TIME_EXIT_PROFIT,
    'time_force_days': TIME_FORCE_DAYS,
    'same_stock_cooldown': SAME_STOCK_COOLDOWN,
    'loss_streak_halve': LOSS_STREAK_HALVE, 'loss_streak_pause': LOSS_STREAK_PAUSE,
    'use_atr_trail': USE_ATR_TRAIL, 'atr_trail_multiplier': ATR_TRAIL_MULTIPLIER,
    'take_profit_tiers': copy.deepcopy(TAKE_PROFIT_TIERS),
    'signal_params': {'filter_st': True, 'filter_bj': True, 'skip_limit_up': True},
})

trades = result['trades']
s = result['summary']
print(f"\n=== Baseline (all signals) ===")
print(f"Trades: {s['trades']}  Win: {s['win_rate']:.1f}%  Ret: {s['total_return']:+.2f}%  DD: {s['max_drawdown']:.2f}%")

# Attach volume ratio and close position to each trade
trade_data = []
for t in trades:
    code = t['code']
    entry_date = t['entry_date']
    # Find the bar for this code on entry date
    entry_bar = bars[(bars['code'] == code) & (bars['_dt'] == pd.to_datetime(entry_date).date())]
    if len(entry_bar) == 0:
        continue
    row = entry_bar.iloc[0]
    trade_data.append({
        'code': code,
        'entry_date': entry_date,
        'ret': t['ret_pct'],
        'reason': t['reason'],
        'vol_ratio_20': float(row['vol_ratio_20']) if pd.notna(row['vol_ratio_20']) else 0,
        'vol_ratio_5': float(row['vol_ratio_5']) if pd.notna(row['vol_ratio_5']) else 0,
        'close_pos': float(row['close_pos']) if pd.notna(row['close_pos']) else 0.5,
    })

tdf = pd.DataFrame(trade_data)
print(f"\nTrades with score data: {len(tdf)}")

# Analyze by scoring combinations
print(f"\n{'='*80}")
print(f"{'Filter':<40s} {'Trades':>6s} {'Win%':>7s} {'AvgRet%':>8s} {'TotalRet%':>9s}")
print(f"{'='*80}")

# Baseline
all_trades = len(tdf)
all_win = (tdf['ret'] > 0).mean() * 100
all_avg_ret = tdf['ret'].mean()
all_total = tdf['ret'].sum()
print(f"{'All signals (baseline)':<40s} {all_trades:>6d} {all_win:>6.1f}% {all_avg_ret:>+7.2f}% {all_total:>+8.1f}%")

# Test different thresholds
for vol_col, vol_label in [('vol_ratio_5', 'Vol/MA5'), ('vol_ratio_20', 'Vol/MA20')]:
    for vol_thresh in [1.0, 1.2, 1.5, 2.0]:
        filtered = tdf[tdf[vol_col] >= vol_thresh]
        if len(filtered) < 5:
            continue
        win = (filtered['ret'] > 0).mean() * 100
        avg_ret = filtered['ret'].mean()
        total_ret = filtered['ret'].sum()
        label = f'{vol_label}>={vol_thresh:.1f}'
        print(f"{label:<40s} {len(filtered):>6d} {win:>6.1f}% {avg_ret:>+7.2f}% {total_ret:>+8.1f}%")

# Close position filters
for pos_thresh in [0.5, 0.6, 0.7, 0.8, 0.9]:
    filtered = tdf[tdf['close_pos'] >= pos_thresh]
    if len(filtered) < 5:
        continue
    win = (filtered['ret'] > 0).mean() * 100
    avg_ret = filtered['ret'].mean()
    total_ret = filtered['ret'].sum()
    label = f'ClosePos>={pos_thresh:.1f} (upper {pos_thresh*100:.0f}%)'
    print(f"{label:<40s} {len(filtered):>6d} {win:>6.1f}% {avg_ret:>+7.2f}% {total_ret:>+8.1f}%")

# Combined filters
print(f"\n{'='*80}")
print(f"{'Combined Filters':<50s} {'Trades':>6s} {'Win%':>7s} {'AvgRet%':>8s} {'TotalRet%':>9s}")
print(f"{'='*80}")

combos = [
    ('Vol/MA5>=1.2 & ClosePos>=0.8', lambda d: (d['vol_ratio_5'] >= 1.2) & (d['close_pos'] >= 0.8)),
    ('Vol/MA5>=1.5 & ClosePos>=0.8', lambda d: (d['vol_ratio_5'] >= 1.5) & (d['close_pos'] >= 0.8)),
    ('Vol/MA20>=1.2 & ClosePos>=0.8', lambda d: (d['vol_ratio_20'] >= 1.2) & (d['close_pos'] >= 0.8)),
    ('Vol/MA20>=1.5 & ClosePos>=0.8', lambda d: (d['vol_ratio_20'] >= 1.5) & (d['close_pos'] >= 0.8)),
    ('Vol/MA5>=1.2 & ClosePos>=0.7', lambda d: (d['vol_ratio_5'] >= 1.2) & (d['close_pos'] >= 0.7)),
    ('Vol/MA20>=1.2 & ClosePos>=0.7', lambda d: (d['vol_ratio_20'] >= 1.2) & (d['close_pos'] >= 0.7)),
]

for label, condition in combos:
    filtered = tdf[condition(tdf)]
    if len(filtered) < 5:
        print(f"{label:<50s} too few trades ({len(filtered)})")
        continue
    win = (filtered['ret'] > 0).mean() * 100
    avg_ret = filtered['ret'].mean()
    total_ret = filtered['ret'].sum()
    print(f"{label:<50s} {len(filtered):>6d} {win:>6.1f}% {avg_ret:>+7.2f}% {total_ret:>+8.1f}%")

# Correlation analysis
print(f"\n=== Correlation with return ===")
for col in ['vol_ratio_5', 'vol_ratio_20', 'close_pos']:
    corr = tdf[col].corr(tdf['ret'])
    print(f"  {col} vs ret: r={corr:.4f}")
