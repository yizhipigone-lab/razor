"""TP2 Phase 2: fine grid around best region (8-10%, 25-40%)"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.backtest.simple_runner import run_backtest, FastEngine
from datetime import date

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
                        p.entry_price *= ratio; p.peak_price *= ratio
                        cur = cp / p.entry_price - 1

            tp2_r = self.p.get('tp2_sell_ratio', 1.0)
            if cur <= self.p['hard_stop']:
                sells.append((p, cp, "HS", None)); continue
            if hd > self.p['time_force_days']:
                sells.append((p, cp, "TF", None)); continue
            if not p.tp2 and cur >= self.p['tp2_pct']:
                if tp2_r < 1.0:
                    ss = int(p.remaining * tp2_r / 100) * 100
                    if ss >= 100: sells.append((p, cp, "TP2", ss))
                else:
                    sells.append((p, cp, "TP2", None))
                continue
            if not p.tp1 and cur >= self.p['tp1_pct']:
                ss = int(p.remaining * self.p['tp1_sell_ratio'] / 100) * 100
                if ss >= 100: sells.append((p, cp, "TP1", ss)); continue
            if pp >= self.p['trail_activate']:
                dd = cp / p.peak_price - 1
                if dd <= -self.p['trail_dd']:
                    sells.append((p, cp, "TR", None)); continue
            if hd > self.p['time_exit_days'] and cur > self.p['time_exit_profit']:
                sells.append((p, cp, "TC", None)); continue
        return sells

import app.backtest.simple_runner as sr
sr.FastEngine = ModifiedEngine

# Phase 2 fine grid
tp2_profits = [7, 8, 9, 10, 11]
tp2_ratios  = [25, 30, 35, 40]

results = []
total = len(tp2_profits) * len(tp2_ratios)
idx = 0

for profit in tp2_profits:
    for ratio in tp2_ratios:
        idx += 1
        pct = profit / 100.0; r = ratio / 100.0
        params = {**BASE, 'tp2_pct': pct, 'tp2_sell_ratio': r}
        result = run_backtest(params)
        s = result['summary']
        results.append({
            'tp2_profit': profit, 'tp2_ratio': ratio,
            'ret': s['total_return'], 'dd': s['max_drawdown'],
            'wr': s['win_rate'], 'calmar': s['calmar'],
            'pf': s['profit_factor'], 'trades': s['trades'],
            'sharpe': s['sharpe'],
        })
        print(f"  [{idx}/{total}] TP2={profit}%卖{ratio}%: ret={s['total_return']:.1f}% dd={s['max_drawdown']:.1f}% wr={s['win_rate']:.0f}% calmar={s['calmar']:.1f}")

# Also run baseline for comparison
sr.FastEngine = FastEngine
base_params = {**BASE, 'tp2_pct': 0.16, 'time_exit_days': 3}
baseline = run_backtest(base_params)
bs = baseline['summary']
print(f"\n  [基线] TP2=16%全清: ret={bs['total_return']:.1f}% dd={bs['max_drawdown']:.1f}% wr={bs['win_rate']:.0f}% calmar={bs['calmar']:.1f}")

# Print sorted
print("\n" + "="*70)
print("  Phase 2 FINAL: sorted by Calmar")
print("="*70)
results.sort(key=lambda x: x['calmar'], reverse=True)
for i, r in enumerate(results[:15]):
    print(f"  #{i+1} TP2={r['tp2_profit']}%卖{r['tp2_ratio']}% | ret={r['ret']:.1f}% dd={r['dd']:.1f}% calmar={r['calmar']:.1f} pf={r['pf']:.2f} wr={r['wr']:.0f}% trades={r['trades']}")

print(f"\n  基线 TP2=16%全清      | ret={bs['total_return']:.1f}% dd={bs['max_drawdown']:.1f}% calmar={bs['calmar']:.1f} pf={bs['profit_factor']:.2f} wr={bs['win_rate']:.0f}% trades={bs['trades']}")
