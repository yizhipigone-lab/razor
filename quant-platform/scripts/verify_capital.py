"""验证资金占用和仓位限制"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.backtest.simple_runner import run_backtest
from datetime import date

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

print("Running 2026-01-01 ~ 2026-05-12 ...")
result = run_backtest(params)
s = result['summary']

# 仓位分析
print(f"\n=== 仓位分析 ===")
print(f"最大同时持仓: {s.get('max_positions_held', 'N/A')} 只")
print(f"平均持仓:     {s.get('avg_positions_held', 'N/A')} 只")
print(f"单票上限:     {params['position_size']:,}")
print(f"初始资金:     {params['initial_capital']:,}")
theory_max = params['initial_capital'] / params['position_size']
print(f"理论最大持仓: {int(theory_max)} 只 (资金/单票上限)")

# 资金占用率
eq = result['equity']
if eq:
    max_eq = max(e['equity'] for e in eq)
    min_eq = min(e['equity'] for e in eq)
    print(f"\n=== 净值 ===")
    print(f"最低净值: {min_eq:,.0f}")
    print(f"最高净值: {max_eq:,.0f}")
    # 每日仓位占用率估算
    print(f"估算仓位占用: 最大{(theory_max * params['position_size'] / params['initial_capital'] * 100):.0f}% 资金")

print(f"\n=== 核心结果 ===")
print(f"总收益:  {s['total_return']}%")
print(f"最大回撤: {s['max_drawdown']}%")
print(f"交易:    {s['trades']} 笔")
print(f"盈亏比:  {s['profit_factor']}")
print(f"胜率:    {s['win_rate']}%")
print(f"卡玛:    {s['calmar']}")
print(f"区间:    {s['start_date']} ~ {s['end_date']}")
