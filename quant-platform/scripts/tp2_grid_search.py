"""TP2 grid search: profit threshold x sell ratio, remaining uses TR"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.backtest.simple_runner import run_backtest, FastEngine
from datetime import date
import numpy as np
from collections import Counter

BASE = {
    'initial_capital': 1_000_000, 'position_size': 50_000, 'min_buy_amt': 5_000,
    'hard_stop': -0.06, 'tp1_pct': 0.04, 'tp1_sell_ratio': 0.15,
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

class ModifiedEngine(FastEngine):
    def check_stops(self, d, snap, prev_snap=None):
        sells = []
        hs = self.p['hard_stop']
        tp1_pct = self.p['tp1_pct']
        tp1_ratio = self.p['tp1_sell_ratio']
        tp2_pct = self.p['tp2_pct']
        tp2_ratio = self.p.get('tp2_sell_ratio', 1.0)
        trail_act = self.p['trail_activate']
        trail_dd = self.p['trail_dd']
        time_exit = self.p['time_exit_days']
        time_exit_profit = self.p['time_exit_profit']
        time_force = self.p['time_force_days']

        for code, p in list(self.positions.items()):
            if not p.active or p.remaining <= 0: continue
            bar = snap.get(code)
            if bar is None: continue
            cp = bar['close']; hp = bar.get('high', cp)
            if hp > p.peak_price: p.peak_price = hp
            pp = p.peak_price / p.entry_price - 1
            cur = cp / p.entry_price - 1
            hd = self._td(p.entry_date, d)

            if prev_snap:
                prev_bar = prev_snap.get(code)
                if prev_bar and prev_bar.get('close', 0) > 0:
                    if cp / prev_bar['close'] - 1 <= -0.20:
                        ratio = cp / prev_bar['close']
                        p.entry_price *= ratio
                        p.peak_price *= ratio
                        cur = cp / p.entry_price - 1

            if cur <= hs:
                sells.append((p, cp, "HS", None)); continue
            if hd > time_force:
                sells.append((p, cp, "TF", None)); continue
            if not p.tp2 and cur >= tp2_pct:
                if tp2_ratio < 1.0:
                    ss = int(p.remaining * tp2_ratio / 100) * 100
                    if ss >= 100:
                        sells.append((p, cp, "TP2", ss)); continue
                else:
                    sells.append((p, cp, "TP2", None)); continue
            if not p.tp1 and cur >= tp1_pct:
                ss = int(p.remaining * tp1_ratio / 100) * 100
                if ss >= 100:
                    sells.append((p, cp, "TP1", ss)); continue
            if pp >= trail_act:
                dd = cp / p.peak_price - 1
                if dd <= -trail_dd:
                    sells.append((p, cp, "TR", None)); continue
            if hd > time_exit and cur > time_exit_profit:
                sells.append((p, cp, "TC", None)); continue
        return sells

import app.backtest.simple_runner as sr
sr.FastEngine = ModifiedEngine

# Phase 1: Wide grid
tp2_profits = [8, 10, 12, 14, 16]
tp2_ratios  = [20, 30, 40, 50, 60, 80, 100]

results = []
total = len(tp2_profits) * len(tp2_ratios)
idx = 0

for profit in tp2_profits:
    for ratio in tp2_ratios:
        idx += 1
        label = f"TP2={profit}%卖{ratio}%"
        pct = profit / 100.0
        r = ratio / 100.0
        params = {**BASE, 'tp2_pct': pct, 'tp2_sell_ratio': r}
        result = run_backtest(params)
        s = result['summary']
        results.append({
            'tp2_profit': profit, 'tp2_ratio': ratio,
            'total_return': s['total_return'], 'max_dd': s['max_drawdown'],
            'win_rate': s['win_rate'], 'calmar': s['calmar'],
            'profit_factor': s['profit_factor'], 'trades': s['trades'],
            'sharpe': s['sharpe'],
            'exit': s['exit_reasons'],
        })
        print(f"  [{idx}/{total}] {label}: ret={s['total_return']:.1f}% dd={s['max_drawdown']:.1f}% wr={s['win_rate']:.0f}% calmar={s['calmar']:.1f}")

sr.FastEngine = FastEngine  # restore

# Print sorted results
print("\n" + "="*80)
print("  RANKED by Calmar (top 10)")
print("="*80)
results.sort(key=lambda x: x['calmar'], reverse=True)
for i, r in enumerate(results[:10]):
    print(f"  #{i+1} TP2={r['tp2_profit']}%卖{r['tp2_ratio']}% | ret={r['total_return']:.1f}% dd={r['max_dd']:.1f}% wr={r['win_rate']:.0f}% calmar={r['calmar']:.1f} pf={r['profit_factor']:.2f} trades={r['trades']}")

print("\n  RANKED by Total Return (top 10)")
print("="*80)
results.sort(key=lambda x: x['total_return'], reverse=True)
for i, r in enumerate(results[:10]):
    print(f"  #{i+1} TP2={r['tp2_profit']}%卖{r['tp2_ratio']}% | ret={r['total_return']:.1f}% dd={r['max_dd']:.1f}% wr={r['win_rate']:.0f}% calmar={r['calmar']:.1f} pf={r['profit_factor']:.2f} trades={r['trades']}")

# Find best combo balancing return and risk
print("\n  RANKED by composite score (ret*0.6 + calmar*0.4)")
print("="*80)
results.sort(key=lambda x: x['total_return']*0.6 + x['calmar']*0.4, reverse=True)
for i, r in enumerate(results[:10]):
    score = r['total_return']*0.6 + r['calmar']*0.4
    print(f"  #{i+1} TP2={r['tp2_profit']}%卖{r['tp2_ratio']}% | ret={r['total_return']:.1f}% dd={r['max_dd']:.1f}% wr={r['win_rate']:.0f}% calmar={r['calmar']:.1f} score={score:.1f}")
