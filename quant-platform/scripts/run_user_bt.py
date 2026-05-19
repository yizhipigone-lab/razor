import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.backtest.simple_runner import run_backtest
from datetime import date
import json

params = {
    'initial_capital': 1_000_000,
    'position_size': 50_000,
    'min_buy_amt': 5_000,
    'hard_stop': -0.06,
    'tp1_pct': 0.04,
    'tp1_sell_ratio': 0.15,
    'tp2_pct': 0.16,
    'trail_activate': 0.03,
    'trail_dd': 0.01,
    'time_exit_days': 3,
    'time_exit_profit': 0.03,
    'time_force_days': 9,
    'loss_streak_halve': 3,
    'loss_streak_pause': 5,
    'pause_days': 3,
    'same_stock_cooldown': 20,
    'signal_params': {
        "version": "improved", "filter_st": True, "filter_bj": True,
        "vol_threshold": 1.5, "close_position_threshold": 0.8,
        "disable_quality_sort": False,
        "filter_consecutive_up": False, "filter_gap_quality": False,
    },
    'start_date': date(2024, 1, 1),
    'end_date': date(2026, 5, 9),
}

print("Starting backtest 2024-01-01 ~ 2026-05-09 ...")
result = run_backtest(params)

if result.get('status') == 'ok':
    s = result['summary']
    print(f"\n=== Summary ===")
    for k, v in s.items():
        print(f"  {k}: {v}")
    print(f"\n  Equity points: {len(result['equity'])}")
    print(f"  Trades: {len(result['trades'])}")
    print(f"  Daily trades: {len(result.get('daily_trades', {}))}")
    print(f"  Indices: {list(result.get('indices', {}).keys())}")
else:
    print(f"Status: {result.get('status')}")
