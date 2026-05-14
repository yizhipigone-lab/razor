"""
对比回测：原版策略 vs 改进版策略，2022-01-01 至今天
使用 simple_runner 的 run_backtest，只改 signal_params.version
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import date
from app.backtest.simple_runner import run_backtest
from app.sim_trader.config import (
    INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
    LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE, PAUSE_DAYS,
    HARD_STOP, TRAIL_ACTIVATE, TRAIL_DD,
    USE_ATR_TRAIL, ATR_TRAIL_MULTIPLIER,
    TIME_EXIT_DAYS, TIME_EXIT_PROFIT, TIME_FORCE_DAYS,
    SAME_STOCK_COOLDOWN, TAKE_PROFIT_TIERS,
    SIGNAL_PARAMS,
)

START = date(2022, 1, 1)
END = date.today()

BASE_PARAMS = {
    "start_date": START,
    "end_date": END,
    "initial_capital": INITIAL_CAPITAL,
    "position_size": POSITION_SIZE,
    "min_buy_amt": MIN_BUY_AMT,
    "loss_streak_halve": LOSS_STREAK_HALVE,
    "loss_streak_pause": LOSS_STREAK_PAUSE,
    "pause_days": PAUSE_DAYS,
    "hard_stop": HARD_STOP,
    "trail_activate": TRAIL_ACTIVATE,
    "trail_dd": TRAIL_DD,
    "use_atr_trail": USE_ATR_TRAIL,
    "atr_trail_multiplier": ATR_TRAIL_MULTIPLIER,
    "time_exit_days": TIME_EXIT_DAYS,
    "time_exit_profit": TIME_EXIT_PROFIT,
    "time_force_days": TIME_FORCE_DAYS,
    "same_stock_cooldown": SAME_STOCK_COOLDOWN,
    "take_profit_tiers": TAKE_PROFIT_TIERS,
}

RESULTS = {}

print("=" * 70)
print(f"对比回测: 原版 vs 改进版  ({START} ~ {END})")
print("=" * 70)

for version in ("original", "improved"):
    label = "原版 (ATAN角度 + 价格<26 + 日涨幅>2%)" if version == "original" else "改进版 (斜率 + 放量 + 收盘位)"
    print(f"\n>>> 正在跑: {label}")

    signal_params = dict(SIGNAL_PARAMS)
    signal_params["version"] = version
    # 原版没有 vol_threshold / close_position_threshold 逻辑，但传了也不影响
    params = dict(BASE_PARAMS)
    params["signal_params"] = signal_params

    result = run_backtest(params)
    RESULTS[version] = result

    s = result["summary"]
    print(f"  总收益: {s['total_return']:+.1f}%  |  最大回撤: {s['max_drawdown']:.1f}%")
    print(f"  胜率: {s['win_rate']:.1f}%  |  交易 {s['trades']} 笔 (赢{s['wins']}/亏{s['losses']})")
    print(f"  夏普: {s['sharpe']:.2f}  |  卡玛: {s['calmar']:.2f}")
    print(f"  最终净值: {s['final_equity']:,.0f}  |  盈亏比: {s['profit_factor']:.2f}")
    print(f"  平均盈利: {s['avg_win']:+.2f}%  |  平均亏损: {s['avg_loss']:+.2f}%")
    print(f"  信号数: {s['signals']}  |  买入数: {s['buy_signals']}")

print("\n" + "=" * 70)
print("对比总结")
print("=" * 70)

so = RESULTS["original"]["summary"]
si = RESULTS["improved"]["summary"]

rows = [
    ("总收益 %", so['total_return'], si['total_return'], "↑"),
    ("最大回撤 %", so['max_drawdown'], si['max_drawdown'], "↓"),
    ("胜率 %", so['win_rate'], si['win_rate'], "↑"),
    ("交易笔数", so['trades'], si['trades'], ""),
    ("盈利笔数", so['wins'], si['wins'], "↑"),
    ("亏损笔数", so['losses'], si['losses'], "↓"),
    ("盈亏比", so['profit_factor'], si['profit_factor'], "↑"),
    ("夏普比率", so['sharpe'], si['sharpe'], "↑"),
    ("卡玛比率", so['calmar'], si['calmar'], "↑"),
    ("索提诺比率", so['sortino'], si['sortino'], "↑"),
    ("年化收益 %", so['ann_return'], si['ann_return'], "↑"),
    ("最终净值", so['final_equity'], si['final_equity'], "↑"),
    ("平均盈利 %", so['avg_win'], si['avg_win'], "↑"),
    ("平均亏损 %", so['avg_loss'], si['avg_loss'], "↑"),
    ("平均持仓(赢)天", so['avg_hold_win'], si['avg_hold_win'], ""),
    ("平均持仓(亏)天", so['avg_hold_loss'], si['avg_hold_loss'], ""),
    ("信号总数", so['signals'], si['signals'], ""),
    ("实际买入数", so['buy_signals'], si['buy_signals'], ""),
    ("最佳交易 %", so['best_trade'], si['best_trade'], "↑"),
    ("最差交易 %", so['worst_trade'], si['worst_trade'], "↑"),
    ("最大同时持仓", so.get('max_positions_held', '-'), si.get('max_positions_held', '-'), ""),
    ("平均持仓数", so.get('avg_positions_held', '-'), si.get('avg_positions_held', '-'), ""),
]

print(f"\n{'指标':<20} {'原版':>12} {'改进版':>12} {'更优方向':>8}")
print("-" * 56)
for name, o, i, direction in rows:
    if isinstance(o, float) and isinstance(i, float):
        better = "←" if (direction == "↑" and i > o) or (direction == "↓" and i < o) else ""
        if direction == "":
            better = ""
        print(f"{name:<20} {o:>12.2f} {i:>12.2f} {better:>8}")
    else:
        print(f"{name:<20} {str(o):>12} {str(i):>12}")

# 胜方判定
print(f"\n最终结论: ", end="")
if si['total_return'] > so['total_return'] and si['sharpe'] > so['sharpe']:
    diff = si['total_return'] - so['total_return']
    print(f"改进版更好 (+{diff:.1f}% 总收益, 夏普 {si['sharpe']:.2f} vs {so['sharpe']:.2f})")
elif so['total_return'] > si['total_return'] and so['sharpe'] > si['sharpe']:
    diff = so['total_return'] - si['total_return']
    print(f"原版更好 (+{diff:.1f}% 总收益, 夏普 {so['sharpe']:.2f} vs {si['sharpe']:.2f})")
else:
    print(f"各有优劣: 原版收益{so['total_return']:+.1f}%/夏普{so['sharpe']:.2f}, 改进版收益{si['total_return']:+.1f}%/夏普{si['sharpe']:.2f}")

print(f"\n退出原因分布:")
print(f"  原版: {so.get('exit_reasons', {})}")
print(f"  改进版: {si.get('exit_reasons', {})}")
