import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import json
from app.backtest.simple_runner import load_index_data, load_daily_bars, run_backtest
from datetime import date

print("=== Test index loading ===")
indices = load_index_data()
for n, d in indices.items():
    print(f"  {n}: {len(d)} rows, first={d[0]['date']}")

print("\n=== Test daily bars ===")
bars = load_daily_bars(date(2024,1,1), date(2025,1,1))
print(f"  {bars.code.nunique()} stocks, {len(bars)} rows")

print("\n=== Test run (minimal, first 90 days of 2024) ===")
params = {
    'initial_capital': 1000000, 'position_size': 50000, 'min_buy_amt': 5000,
    'hard_stop': -0.06, 'tp1_pct': 0.04, 'tp1_sell_ratio': 0.15, 'tp2_pct': 0.16,
    'trail_activate': 0.03, 'trail_dd': 0.01,
    'time_exit_days': 3, 'time_exit_profit': 0.03, 'time_force_days': 9,
    'loss_streak_halve': 3, 'loss_streak_pause': 5, 'pause_days': 3,
    'same_stock_cooldown': 20,
    'signal_params': {"version":"improved","filter_st":True,"filter_bj":True,
        "vol_threshold":1.5,"close_position_threshold":0.8,
        "disable_quality_sort":False,"filter_consecutive_up":False,"filter_gap_quality":False},
    'start_date': date(2024,1,1), 'end_date': date(2024,4,1),
}
result = run_backtest(params)
print(f"  Status: {result['status']}")
if result.get('summary'):
    s = result['summary']
    print(f"  Return: {s['total_return']}%  DD: {s['max_drawdown']}%  Trades: {s['trades']}")
    print(f"  Equity points: {len(result['equity'])}")
    print(f"  Indices: {list(result['indices'].keys())}")
    print(f"\n  Full summary keys: {list(result['summary'].keys())}")
    import json
    print(f"  Summary JSON:")
    for k,v in result['summary'].items():
        print(f"    {k}: {v}")
