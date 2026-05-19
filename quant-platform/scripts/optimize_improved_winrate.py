"""
改进版策略赢率优化 — 测试多种增强方案
每个方案只跑 2024-01-01 ~ today 加速迭代
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import date
from app.backtest.simple_runner import run_backtest, load_daily_bars
from app.sim_trader.config import *  # star import fine here
import pandas as pd
import numpy as np

START = date(2024, 1, 1)
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

def test(name, sp):
    p = dict(BASE); p["signal_params"] = sp
    r = run_backtest(p)
    s = r["summary"]
    return {"name":name, "ret":s['total_return'], "dd":s['max_drawdown'],
            "wr":s['win_rate'], "trades":s['trades'], "sharpe":s['sharpe'],
            "calmar":s['calmar'], "signals":s['signals'], "pf":s['profit_factor'],
            "aw":s['avg_win'], "al":s['avg_loss']}

IMPROVED_BASE = {"version":"improved","filter_st":True,"filter_bj":True,
                 "vol_threshold":1.5,"close_position_threshold":0.8}

# ============================================================
# 1. 量比阈值扫描
# ============================================================
print("="*60)
print("1. 量比阈值扫描")
print("="*60)
for vt in [1.0, 1.3, 1.5, 1.8, 2.0, 2.5]:
    sp = dict(IMPROVED_BASE); sp["vol_threshold"] = vt
    r = test(f"量比={vt}", sp)
    print(f"  {r['name']}: WR={r['wr']:.1f}% 收益={r['ret']:+.1f}% 交易={r['trades']} Shar={r['sharpe']:.2f}")

# ============================================================
# 2. 收盘位置阈值扫描
# ============================================================
print("\n"+"="*60)
print("2. 收盘位置阈值扫描")
print("="*60)
for cp in [0.5, 0.6, 0.7, 0.8, 0.85, 0.9]:
    sp = dict(IMPROVED_BASE); sp["close_position_threshold"] = cp
    r = test(f"收盘位={cp}", sp)
    print(f"  {r['name']}: WR={r['wr']:.1f}% 收益={r['ret']:+.1f}% 交易={r['trades']} Shar={r['sharpe']:.2f}")

# ============================================================
# 3. 质量排序阈值 (disable_quality_sort + 取topN)
# ============================================================
# 这个需要改信号生成逻辑，暂用 disable_quality_sort 对比
print("\n"+"="*60)
print("3. 质量排序对比")
print("="*60)
for qs in [False, True]:
    sp = dict(IMPROVED_BASE); sp["disable_quality_sort"] = qs
    r = test(f"质量排序={'开' if not qs else '关'}", sp)
    print(f"  {r['name']}: WR={r['wr']:.1f}% 收益={r['ret']:+.1f}% 交易={r['trades']}")

# ============================================================
# 4. 组合最佳参数
# ============================================================
print("\n"+"="*60)
print("4. 组合参数")
print("="*60)
combos = [
    ("VT1.8+CP0.85", {"vol_threshold":1.8,"close_position_threshold":0.85}),
    ("VT2.0+CP0.85", {"vol_threshold":2.0,"close_position_threshold":0.85}),
    ("VT1.8+CP0.9", {"vol_threshold":1.8,"close_position_threshold":0.9}),
    ("VT2.0+CP0.8", {"vol_threshold":2.0,"close_position_threshold":0.8}),
]
for name, extra in combos:
    sp = dict(IMPROVED_BASE); sp.update(extra)
    r = test(name, sp)
    print(f"  {r['name']}: WR={r['wr']:.1f}% 收益={r['ret']:+.1f}% 交易={r['trades']} Shar={r['sharpe']:.2f} DD={r['dd']:.1f}%")

# ============================================================
# 5. 可选过滤器
# ============================================================
print("\n"+"="*60)
print("5. 可选过滤器")
print("="*60)
filters = [
    ("连续阳线过滤", {"filter_consecutive_up":True}),
    ("跳空过滤", {"filter_gap_quality":True}),
    ("两者都开", {"filter_consecutive_up":True,"filter_gap_quality":True}),
]
for name, extra in filters:
    sp = dict(IMPROVED_BASE); sp.update(extra)
    r = test(name, sp)
    print(f"  {r['name']}: WR={r['wr']:.1f}% 收益={r['ret']:+.1f}% 交易={r['trades']}")

# ============================================================
# 6. 硬止损放宽 + 时间条件调整
# ============================================================
print("\n"+"="*60)
print("6. 退出参数调整")
print("="*60)
exit_tests = [
    ("HS-8%", {"hard_stop":-0.08}),
    ("HS-5%", {"hard_stop":-0.05}),
    ("T_force=7d", {"time_force_days":7}),
    ("T_force=12d", {"time_force_days":12}),
    ("T_exit=5d", {"time_exit_days":5}),
]
for name, extra in exit_tests:
    p = dict(BASE); p["signal_params"] = dict(IMPROVED_BASE); p.update(extra)
    r = run_backtest(p)
    s = r["summary"]
    print(f"  {name}: WR={s['win_rate']:.1f}% 收={s['total_return']:+.1f}% 交={s['trades']} DD={s['max_drawdown']:.1f}%")

# ============================================================
# 7. TP tiers 调整
# ============================================================
print("\n"+"="*60)
print("7. TP 止盈档位")
print("="*60)
tp_tests = [
    ("TP1=3%/15%", [{"profit_pct":0.03,"sell_ratio":0.15},{"profit_pct":0.07,"sell_ratio":0.25}]),
    ("TP1=5%/20%", [{"profit_pct":0.05,"sell_ratio":0.20},{"profit_pct":0.08,"sell_ratio":0.25}]),
    ("TP1=3%/10% TP2=6%/20%", [{"profit_pct":0.03,"sell_ratio":0.10},{"profit_pct":0.06,"sell_ratio":0.20}]),
]
for name, tiers in tp_tests:
    p = dict(BASE); p["signal_params"] = dict(IMPROVED_BASE); p["take_profit_tiers"] = tiers
    r = run_backtest(p)
    s = r["summary"]
    print(f"  {name}: WR={s['win_rate']:.1f}% 收={s['total_return']:+.1f}% 交={s['trades']}")

# ============================================================
# 8. 终极组合
# ============================================================
print("\n"+"="*60)
print("8. 终极组合（取前几轮最佳）")
print("="*60)
ultimate_sp = {
    "version":"improved","filter_st":True,"filter_bj":True,
    "vol_threshold":2.0,"close_position_threshold":0.85,
    "filter_consecutive_up":True,"filter_gap_quality":True,
}
ultimate_p = dict(BASE)
ultimate_p["signal_params"] = ultimate_sp
ultimate_p["hard_stop"] = -0.05
r = run_backtest(ultimate_p)
s = r["summary"]
print(f"  终极: WR={s['win_rate']:.1f}% 收={s['total_return']:+.1f}% 交={s['trades']} DD={s['max_drawdown']:.1f}% Shar={s['sharpe']:.2f} Cal={s['calmar']:.2f}")
