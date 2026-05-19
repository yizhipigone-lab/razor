"""
原版(取消close<26全价位) vs 改进版 回测对比  2022.1.1 ~ 今天
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
)

START = date(2022, 1, 1)
END = date.today()

BASE = {
    "start_date": START, "end_date": END,
    "initial_capital": INITIAL_CAPITAL, "position_size": POSITION_SIZE,
    "min_buy_amt": MIN_BUY_AMT,
    "loss_streak_halve": LOSS_STREAK_HALVE,
    "loss_streak_pause": LOSS_STREAK_PAUSE, "pause_days": PAUSE_DAYS,
    "hard_stop": HARD_STOP, "trail_activate": TRAIL_ACTIVATE, "trail_dd": TRAIL_DD,
    "use_atr_trail": USE_ATR_TRAIL, "atr_trail_multiplier": ATR_TRAIL_MULTIPLIER,
    "time_exit_days": TIME_EXIT_DAYS, "time_exit_profit": TIME_EXIT_PROFIT,
    "time_force_days": TIME_FORCE_DAYS, "same_stock_cooldown": SAME_STOCK_COOLDOWN,
    "take_profit_tiers": TAKE_PROFIT_TIERS,
}

RESULTS = {}

print("=" * 64)
print(f"原版(全价位,取消close<26) vs 改进版  2022-01-01 ~ {END}")
print("=" * 64)

for label, sp in [
    ("原版全价位(取消<26)", {"version": "original", "filter_st": True, "filter_bj": True, "max_price": 99999}),
    ("改进版", {"version": "improved", "filter_st": True, "filter_bj": True,
                "vol_threshold": 1.5, "close_position_threshold": 0.8}),
]:
    print(f"\n>>> {label}")
    params = dict(BASE)
    params["signal_params"] = sp
    result = run_backtest(params)
    RESULTS[label] = result
    s = result["summary"]
    print(f"  总收益: {s['total_return']:+.1f}%  回撤: {s['max_drawdown']:.1f}%  胜率: {s['win_rate']:.1f}%")
    print(f"  交易: {s['trades']}笔  夏普: {s['sharpe']:.2f}  卡玛: {s['calmar']:.2f}")
    print(f"  最终净值: {s['final_equity']:,.0f}  盈亏比: {s['profit_factor']:.2f}")
    print(f"  均盈: {s['avg_win']:+.2f}%  均亏: {s['avg_loss']:+.2f}%  信号: {s['signals']}")

print("\n" + "=" * 64)
print("对比")
print("=" * 64)
so = RESULTS["原版全价位(取消<26)"]["summary"]
si = RESULTS["改进版"]["summary"]

for name, o, i, better in [
    ("总收益 %", so['total_return'], si['total_return'], "↑"),
    ("最大回撤 %", so['max_drawdown'], si['max_drawdown'], "↓"),
    ("胜率 %", so['win_rate'], si['win_rate'], "↑"),
    ("交易笔数", so['trades'], si['trades'], ""),
    ("盈亏比", so['profit_factor'], si['profit_factor'], "↑"),
    ("夏普比率", so['sharpe'], si['sharpe'], "↑"),
    ("卡玛比率", so['calmar'], si['calmar'], "↑"),
    ("年化收益 %", so['ann_return'], si['ann_return'], "↑"),
    ("均盈 %", so['avg_win'], si['avg_win'], "↑"),
    ("均亏 %", so['avg_loss'], si['avg_loss'], "↑"),
    ("信号数", so['signals'], si['signals'], ""),
    ("最终净值", so['final_equity'], si['final_equity'], "↑"),
]:
    o_str = f"{o:.2f}" if isinstance(o, float) else f"{o:,}"
    i_str = f"{i:.2f}" if isinstance(i, float) else f"{i:,}"
    marker = "←" if (better == "↑" and i > o) or (better == "↓" and i < o) else ""
    print(f"  {name:<18} {o_str:>12}  {i_str:>12}  {marker}")

print(f"\n退出原因: 原版={so.get('exit_reasons',{})}")
print(f"退出原因: 改进版={si.get('exit_reasons',{})}")
print(f"\n原版(全价位)信号价格: 改进版信号价格:")
print(f"  (见上方信号数差异)")
