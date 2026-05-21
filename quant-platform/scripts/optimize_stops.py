"""
止盈止损参数网格搜索 — TDXv2 严格复刻 2026
精简版：~360组合，约10分钟
"""
import sys, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import itertools, copy, warnings
warnings.filterwarnings('ignore')

from datetime import date
from app.backtest.simple_runner import run_backtest

PERIOD = ("2026-01-01", str(date.today()))
print(f"Period: {PERIOD[0]} ~ {PERIOD[1]}")

HS   = [-0.04, -0.05, -0.06, -0.07]
TA   = [0.02, 0.03, 0.04]
TD   = [0.008, 0.01, 0.015]
TED  = [5, 7, 10]
TFD  = [12, 15, 20]
ATR  = [True, False]
TP   = [
    ("off", []),
    ("1t_3/10", [{"profit_pct":0.03,"sell_ratio":0.10}]),
    ("2t_3/10+6/20", [{"profit_pct":0.03,"sell_ratio":0.10},{"profit_pct":0.06,"sell_ratio":0.20}]),
    ("2t_5/15+10/30", [{"profit_pct":0.05,"sell_ratio":0.15},{"profit_pct":0.10,"sell_ratio":0.30}]),
]

total = len(HS)*len(TA)*len(TD)*len(TED)*len(TFD)*len(ATR)*len(TP)
print(f"Combos: {total}")

results = []
for idx, (hs, ta, td, ted, tfd, atr, (tp_lbl, tp_tiers)) in enumerate(itertools.product(HS, TA, TD, TED, TFD, ATR, TP)):
    p = {
        'strategy_name': 'TDXv2_严格复刻',
        'start_date': PERIOD[0], 'end_date': PERIOD[1],
        'initial_capital': 1_000_000, 'position_size': 50000, 'min_buy_amt': 5000,
        'hard_stop': hs, 'trail_activate': ta, 'trail_dd': td,
        'time_exit_days': ted, 'time_exit_profit': 0.03, 'time_force_days': tfd,
        'same_stock_cooldown': 20, 'loss_streak_halve': 3, 'loss_streak_pause': 5,
        'use_atr_trail': atr, 'atr_trail_multiplier': 1.0,
        'take_profit_tiers': copy.deepcopy(tp_tiers),
        'signal_params': {'filter_st':True, 'filter_bj':True, 'skip_limit_up':True},
    }
    try:
        r = run_backtest(p)
        s = r['summary']
        results.append(dict(hs=hs, ta=ta, td=td, ted=ted, tfd=tfd, atr=atr, tp=tp_lbl,
            ret=s['total_return'], dd=s['max_drawdown'], sharpe=s['sharpe'],
            calmar=s['calmar'], win=s['win_rate'], trades=s['trades'], pf=s['profit_factor']))
    except Exception:
        pass
    if (idx+1) % 50 == 0:
        print(f"  {idx+1}/{total}")

print(f"\nDone: {len(results)} results\n")

def show(title, key, top_n=10):
    ranked = sorted(results, key=lambda x: x[key], reverse=True)
    is_pct = key in ('ret','dd','win')
    print(f"=== {title} ===")
    print(f"  {'HS':>5s} {'TA':>5s} {'TD':>5s} {'TED':>4s} {'TFD':>4s} {'ATR':>4s} {'TP':<20s} {'Ret':>8s} {'DD':>7s} {'Sharpe':>7s} {'Calmar':>7s} {'Win':>6s} {'Trd':>5s}")
    print(f"  {'-'*85}")
    for r in ranked[:top_n]:
        pct = lambda v: f"{v:+.2f}%" if is_pct else str(v)
        print(f"  {r['hs']:>5.0%} {r['ta']:>5.0%} {r['td']:>5.1%} {r['ted']:>4d} {r['tfd']:>4d} {str(r['atr']):>4s} {r['tp']:<20s} {r['ret']:>+7.2f}% {r['dd']:>+6.2f}% {r['sharpe']:>6.2f} {r['calmar']:>6.2f} {r['win']:>5.1f}% {r['trades']:>5d}")

show("TOP by Return", 'ret')
show("TOP by Calmar", 'calmar')
show("TOP by Sharpe", 'sharpe')
show("TOP by Win Rate", 'win')

# Current config
cur = [r for r in results if abs(r['hs']+0.06)<0.001 and abs(r['ta']-0.03)<0.001 and abs(r['td']-0.01)<0.001 and r['ted']==7 and r['tfd']==12 and r['atr']==True and r['tp']=='2t_3/10+6/20']
if cur:
    c = cur[0]
    print(f"\n=== Current (HS=-6% TA=3% TD=1% TED=7 TFD=12 ATR=T TP=2t_3/10+6/20) ===")
    print(f"  Ret:{c['ret']:+.2f}% DD:{c['dd']:+.2f}% Sharpe:{c['sharpe']:.2f} Calmar:{c['calmar']:.2f} Win:{c['win']:.1f}% Trades:{c['trades']}")
    # Rank among all results
    for metric in ['ret','calmar','sharpe','win']:
        ranked = sorted(results, key=lambda x: x[metric], reverse=True)
        rank = next(i+1 for i,r in enumerate(ranked) if r is c)
        print(f"  Rank by {metric}: {rank}/{len(results)}")
