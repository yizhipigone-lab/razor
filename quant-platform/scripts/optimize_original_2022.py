"""
原版策略优化 2022-01-01 ~ today
分两个版本：close<26 / 全价位(max_price=-1)
只调 止盈止损+退出参数，不改MA5公式
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import date
from app.backtest.simple_runner import run_backtest
from app.sim_trader.config import *

START = date(2022, 1, 1); END = date.today()

BASE = {
    "start_date": START, "end_date": END,
    "initial_capital": INITIAL_CAPITAL, "position_size": POSITION_SIZE,
    "min_buy_amt": MIN_BUY_AMT,
    "loss_streak_halve": LOSS_STREAK_HALVE,
    "loss_streak_pause": LOSS_STREAK_PAUSE, "pause_days": PAUSE_DAYS,
    "hard_stop": HARD_STOP, "trail_activate": TRAIL_ACTIVATE, "trail_dd": TRAIL_DD,
    "time_exit_days": TIME_EXIT_DAYS, "time_exit_profit": TIME_EXIT_PROFIT,
    "time_force_days": TIME_FORCE_DAYS, "same_stock_cooldown": SAME_STOCK_COOLDOWN,
    "take_profit_tiers": TAKE_PROFIT_TIERS,
}

def bt(version_label, max_p, name, sp_extra={}, exit_extra={}, tp_extra=None):
    p = dict(BASE)
    sp = {"version":"original","filter_st":True,"filter_bj":True}
    if max_p >= 0: sp["max_price"] = max_p
    sp.update(sp_extra)
    p.update(exit_extra)
    p["signal_params"] = sp
    if tp_extra: p["take_profit_tiers"] = tp_extra
    r = run_backtest(p)
    s = r["summary"]
    print(f"  {version_label:<8} {name:<30} WR={s['win_rate']:.1f}%  收益={s['total_return']:+.1f}%  交易={s['trades']:>5}  Shar={s['sharpe']:.2f}  DD={s['max_drawdown']:.1f}%  盈亏比={s['profit_factor']:.2f}  均盈={s['avg_win']:+.1f}%  均亏={s['avg_loss']:+.1f}%")
    return s

VERSIONS = [("close<26", 0), ("全价位", -1)]

for vlabel, maxp in VERSIONS:
    print(f"\n{'='*80}")
    print(f"  {vlabel} 版本  2022-01-01 ~ {END}")
    print(f"{'='*80}")

    print("\n【基线】")
    baseline = bt(vlabel, maxp, "基线(默认参数)")

    print("\n【1. TP 止盈档位】")
    tp_tests = [
        ("默认 TP4%/15%+7%/25%", TAKE_PROFIT_TIERS),
        ("TP3%/15%+7%/25%", [{"profit_pct":0.03,"sell_ratio":0.15},{"profit_pct":0.07,"sell_ratio":0.25}]),
        ("TP3%/10%+6%/20%", [{"profit_pct":0.03,"sell_ratio":0.10},{"profit_pct":0.06,"sell_ratio":0.20}]),
        ("TP3%/10%+5%/15%", [{"profit_pct":0.03,"sell_ratio":0.10},{"profit_pct":0.05,"sell_ratio":0.15}]),
        ("TP4%/10%+7%/20%", [{"profit_pct":0.04,"sell_ratio":0.10},{"profit_pct":0.07,"sell_ratio":0.20}]),
    ]
    for name, tp in tp_tests:
        bt(vlabel, maxp, name, tp_extra=tp)

    print("\n【2. 硬止损】")
    for hs in [-0.05, -0.07, -0.08, -0.10]:
        bt(vlabel, maxp, f"HS={hs*100:.0f}%", exit_extra={"hard_stop":hs})

    print("\n【3. 时间退出】")
    for td in [5, 7, 12]:
        bt(vlabel, maxp, f"T_force={td}d", exit_extra={"time_force_days":td})
    for te in [5]:
        bt(vlabel, maxp, f"T_exit={te}d", exit_extra={"time_exit_days":te})

    print("\n【4. TP + HS 组合（取前几轮最优）】")
    combos = [
        ("TP3/10+6/20  HS-8%", [{"profit_pct":0.03,"sell_ratio":0.10},{"profit_pct":0.06,"sell_ratio":0.20}], {"hard_stop":-0.08}),
        ("TP3/10+5/15  HS-8%", [{"profit_pct":0.03,"sell_ratio":0.10},{"profit_pct":0.05,"sell_ratio":0.15}], {"hard_stop":-0.08}),
        ("TP3/10+6/20  HS-7%", [{"profit_pct":0.03,"sell_ratio":0.10},{"profit_pct":0.06,"sell_ratio":0.20}], {"hard_stop":-0.07}),
        ("TP4/10+7/20  HS-8%", [{"profit_pct":0.04,"sell_ratio":0.10},{"profit_pct":0.07,"sell_ratio":0.20}], {"hard_stop":-0.08}),
        ("TP3/10+6/20  Tf=12d", [{"profit_pct":0.03,"sell_ratio":0.10},{"profit_pct":0.06,"sell_ratio":0.20}], {"time_force_days":12}),
    ]
    for name, tp, exit_extra in combos:
        bt(vlabel, maxp, name, exit_extra=exit_extra, tp_extra=tp)

    print("\n【5. 可选过滤】")
    bt(vlabel, maxp, "+连续阳线过滤", sp_extra={"filter_consecutive_up": True})

    # 终极：最佳TP + 最佳HS + 可选过滤
    print("\n【6. 终极组合】")
    best_tp = [{"profit_pct":0.03,"sell_ratio":0.10},{"profit_pct":0.06,"sell_ratio":0.20}]
    bt(vlabel, maxp, "终极: TP3/10+6/20 HS-8% Tf=12d +连续阳线",
       sp_extra={"filter_consecutive_up": True},
       exit_extra={"hard_stop":-0.08, "time_force_days":12},
       tp_extra=best_tp)

print("\n\n原版 vs 改进版 最终对比(2022至今):")
print("="*80)
# baseline results from earlier are already in scope
