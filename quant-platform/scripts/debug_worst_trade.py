import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.backtest.simple_runner import run_backtest, load_daily_bars
from datetime import date
import pandas as pd

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
    'start_date': date(2024, 1, 1), 'end_date': date(2026, 5, 9),
}

result = run_backtest(params)

# 找最差 10 笔
trades = result['trades']
trades_sorted = sorted(trades, key=lambda t: t['ret_pct'])
print("\n=== 最差 10 笔交易 ===")
for t in trades_sorted[:10]:
    print(f"  {t['code']} 买入{t['entry_date']}@{t['entry_px']} → 卖出{t['exit_date']}@{t['exit_px']} 盈亏{t['ret_pct']}% 原因:{t['reason']} 持{t['hold_days']}天")

print("\n=== 亏损最大的前20笔 HS 退出 ===")
hs_trades = [t for t in trades_sorted if t['reason'] == 'HS']
for t in hs_trades[:20]:
    print(f"  {t['code']} 买入{t['entry_date']}@{t['entry_px']} → 卖出{t['exit_date']}@{t['exit_px']} 盈亏{t['ret_pct']}% 持{t['hold_days']}天")

# 加载原始数据检查极端亏损的股票
print("\n=== 检查最差交易股票的日线数据 ===")
bars = load_daily_bars(date(2024, 1, 1), date(2026, 5, 9))
worst = trades_sorted[0]
code = worst['code']
entry_d = pd.Timestamp(worst['entry_date']).date()
exit_d = pd.Timestamp(worst['exit_date']).date()
stock_bars = bars[(bars['code'] == code) & (bars['date'] >= entry_d) & (bars['date'] <= exit_d)]
print(f"\n{code} 从 {entry_d} 到 {exit_d}:")
if len(stock_bars) > 0:
    for _, row in stock_bars.iterrows():
        chg = (row['close'] / worst['entry_px'] - 1) * 100
        print(f"  {row['date']} O:{row['open']:.2f} H:{row['high']:.2f} L:{row['low']:.2f} C:{row['close']:.2f} 盈亏%:{chg:.1f}%")
else:
    print(f"  无日线数据！")
