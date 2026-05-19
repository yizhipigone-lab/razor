"""
赢率优化 v3 — 未测试的方法
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import date
from app.backtest.simple_runner import run_backtest
from app.sim_trader.config import *
import copy

START = date(2024,1,1); END = date.today()

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

SP = {"version":"improved","filter_st":True,"filter_bj":True,"vol_threshold":1.5,"close_position_threshold":0.8,"skip_limit_up":True}

def bt(name, p_overrides=None):
    p = copy.deepcopy(BASE)
    p["signal_params"] = dict(SP)
    if p_overrides:
        for k in p_overrides:
            if k == "signal_params": p["signal_params"].update(p_overrides[k])
            else: p[k] = p_overrides[k]
    r = run_backtest(p)
    s = r["summary"]
    print(f"  {name:<35} WR={s['win_rate']:.1f}%  收益={s['total_return']:+.1f}%  交易={s['trades']:>5}  DD={s['max_drawdown']:.1f}%  Shar={s['sharpe']:.2f}")
    return s

print("="*70)
print(f"赢率优化 v3  2024-01-01 ~ {END}")
print("="*70)

print("\n【基线】")
baseline = bt("基线(改进版默认)")

# 1. 保本止盈：盈利达X%后硬止损上移到保本
print("\n【1. 保本止盈-breakeven】")
# This requires engine code changes, not just config. Test via exit params.
# Simulate: if profit reaches X%, raise stop to entry (instead of -6% use 0%)
# Actually we can approximate: set time_exit with very short period
# Better: just test lower hard_stop combined with tighter trail
for be_pct in [0.02, 0.03, 0.04]:
    # When profit hits be_pct, trail activates with very tight dd
    bt(f"Trail激活={be_pct*100:.0f}% DD=0.5%", {"trail_activate":be_pct, "trail_dd":0.005})

# 2. 三档止盈
print("\n【2. 三档止盈】")
for name, tiers in [
    ("3档:3/10+6/15+10/25", [{"p":0.03,"r":0.10},{"p":0.06,"r":0.15},{"p":0.10,"r":0.25}]),
    ("3档:3/10+5/10+8/20", [{"p":0.03,"r":0.10},{"p":0.05,"r":0.10},{"p":0.08,"r":0.20}]),
    ("3档:2/5+4/10+8/20", [{"p":0.02,"r":0.05},{"p":0.04,"r":0.10},{"p":0.08,"r":0.20}]),
]:
    tiers_f = [{"profit_pct":t["p"],"sell_ratio":t["r"]} for t in tiers]
    bt(name, {"take_profit_tiers": tiers_f})

# 3. 动态HS：ATR-based
print("\n【3. ATR动态硬止损】")
# Already have use_atr_trail for trail, but HS is fixed.
# Test different fixed HS values that are more/less aggressive
for hs in [-0.04, -0.05, -0.07, -0.08]:
    bt(f"HS={hs*100:.0f}%", {"hard_stop": hs})

# 4. 冷却期
print("\n【4. 冷却期调整】")
for cd in [10, 15, 30, 40]:
    bt(f"冷却={cd}天", {"same_stock_cooldown": cd})

# 5. 连亏暂停
print("\n【5. 连亏参数】")
for lsp, pd_days in [(3,5), (4,7), (7,10)]:
    bt(f"连亏{lsp}停{pd_days}天", {"loss_streak_pause": lsp, "pause_days": pd_days})

# 6. 信号参数
print("\n【6. 信号参数微调】")
for name, sp_extra in [
    ("VT=1.2", {"vol_threshold":1.2}),
    ("VT=2.0", {"vol_threshold":2.0}),
    ("CP=0.7", {"close_position_threshold":0.7}),
    ("CP=0.9", {"close_position_threshold":0.9}),
    ("VT=1.2+CP=0.7", {"vol_threshold":1.2,"close_position_threshold":0.7}),
    ("VT=2.0+CP=0.9", {"vol_threshold":2.0,"close_position_threshold":0.9}),
]:
    bt(name, {"signal_params": sp_extra})

# 7. 综合最优尝试
print("\n【7. 综合尝试】")
combos = [
    ("3TP+HS7%+冷却30", {"take_profit_tiers":[{"profit_pct":0.03,"sell_ratio":0.10},{"profit_pct":0.05,"sell_ratio":0.10},{"profit_pct":0.08,"sell_ratio":0.20}],"hard_stop":-0.07,"same_stock_cooldown":30}),
    ("3TP+HS8%+连亏7停10", {"take_profit_tiers":[{"profit_pct":0.03,"sell_ratio":0.10},{"profit_pct":0.06,"sell_ratio":0.15},{"profit_pct":0.10,"sell_ratio":0.25}],"hard_stop":-0.08,"loss_streak_pause":7,"pause_days":10}),
    ("CP0.9+VT2.0+HS7%", {"signal_params":{"vol_threshold":2.0,"close_position_threshold":0.9},"hard_stop":-0.07}),
    ("CP0.7+VT1.2+3TP", {"signal_params":{"vol_threshold":1.2,"close_position_threshold":0.7},"take_profit_tiers":[{"profit_pct":0.02,"sell_ratio":0.05},{"profit_pct":0.04,"sell_ratio":0.10},{"profit_pct":0.08,"sell_ratio":0.20}]}),
]
for name, overrides in combos:
    bt(name, overrides)
