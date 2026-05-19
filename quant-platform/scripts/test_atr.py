"""ATR 动态止损回测"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
import numpy as np
from datetime import date, timedelta
from collections import Counter

BASE = {
    'initial_capital': 1_000_000, 'position_size': 50_000, 'min_buy_amt': 5_000,
    'hard_stop': -0.06,
    'take_profit_tiers': [{'profit_pct': 0.04, 'sell_ratio': 0.15},{'profit_pct': 0.07, 'sell_ratio': 0.25}],
    'trail_activate': 0.03, 'trail_dd': 0.01,
    'time_exit_days': 3, 'time_exit_profit': 0.03, 'time_force_days': 9,
    'loss_streak_halve': 3, 'loss_streak_pause': 5, 'pause_days': 3,
    'same_stock_cooldown': 20,
    'signal_params': {"version":"improved","filter_st":True,"filter_bj":True,"vol_threshold":1.5,"close_position_threshold":0.8,"disable_quality_sort":False,"filter_consecutive_up":False,"filter_gap_quality":False},
    'start_date': date(2026, 1, 1), 'end_date': date(2026, 5, 12),
}

# ── 预计算全市场 ATR(14) ──
from app.backtest.simple_runner import load_daily_bars
print("Loading bars + computing ATR...")
bars = load_daily_bars(date(2025, 11, 1), date(2026, 5, 12))
bars = bars.sort_values(['code','date'])

def compute_atr(df):
    high, low, close = df['high'], df['low'], df['close'].shift(1)
    tr1 = high - low
    tr2 = abs(high - close)
    tr3 = abs(low - close)
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(14).mean()

bars['atr14'] = bars.groupby('code', group_keys=False).apply(compute_atr).reset_index(level=0, drop=True)
atr_map = {}
for _, r in bars.iterrows():
    atr_map[(r['code'], r['date'])] = r['atr14'] if not pd.isna(r['atr14']) else 0
print(f"ATR map: {len(atr_map)} entries")

# ── ATR 增强引擎 ──
from app.backtest.simple_runner import run_backtest, FastEngine

class ATREngine(FastEngine):
    def check_stops(self, d, snap, prev_snap=None):
        sells = []
        hs = self.p['hard_stop']
        tp_tiers = self.p.get('take_profit_tiers', [])
        trail_act = self.p['trail_activate']
        trail_dd = self.p['trail_dd']
        time_exit = self.p['time_exit_days']
        time_exit_profit = self.p['time_exit_profit']
        time_force = self.p['time_force_days']
        atr_mul_hs = self.p.get('atr_mul_hs', 0)   # ATR倍数→硬止损, 0=不用
        atr_mul_tr = self.p.get('atr_mul_tr', 0)   # ATR倍数→移动止盈回撤
        atr_map = self.p.get('_atr_map', {})

        for code, p in list(self.positions.items()):
            if not p.active or p.remaining <= 0: continue
            bar = snap.get(code)
            if bar is None: continue
            cp = bar['close']; hp = bar.get('high', cp)
            if hp > p.peak_price: p.peak_price = hp
            pp = p.peak_price / p.entry_price - 1
            cur = cp / p.entry_price - 1
            hd = self._td(p.entry_date, d)

            # 除权保护
            if prev_snap:
                prev_bar = prev_snap.get(code)
                if prev_bar and prev_bar.get('close', 0) > 0:
                    if cp / prev_bar['close'] - 1 <= -0.20:
                        r = cp / prev_bar['close']
                        p.entry_price *= r; p.peak_price *= r
                        cur = cp / p.entry_price - 1

            # ATR 硬止损：取 max(固定%, -ATR倍数*ATR/entry)
            effective_hs = hs
            if atr_mul_hs > 0:
                atr_val = atr_map.get((code, d), 0)
                if atr_val > 0 and p.entry_price > 0:
                    atr_stop_pct = -atr_mul_hs * atr_val / p.entry_price
                    effective_hs = min(hs, atr_stop_pct)  # 两者取更严格

            if cur <= effective_hs:
                label = "HS" if effective_hs == hs else f"HSa({cur*100:.1f}%)"
                sells.append((p, cp, label, None)); continue

            if hd > time_force:
                sells.append((p, cp, "TF", None)); continue

            # 多档止盈
            for idx, tier in enumerate(tp_tiers):
                if idx not in p.tp_triggered and cur >= tier['profit_pct']:
                    ss = int(p.remaining * tier['sell_ratio'] / 100) * 100
                    if ss >= 100:
                        p.tp_triggered.add(idx)
                        sells.append((p, cp, f"TP{idx+1}", ss))
                        break

            # ATR 移动止盈
            effective_trail_dd = trail_dd
            if atr_mul_tr > 0:
                atr_val = atr_map.get((code, d), 0)
                if atr_val > 0 and p.entry_price > 0:
                    atr_trail = atr_mul_tr * atr_val / p.entry_price
                    effective_trail_dd = max(trail_dd, atr_trail)

            if pp >= trail_act:
                dd = cp / p.peak_price - 1
                if dd <= -effective_trail_dd:
                    sells.append((p, cp, "TR", None)); continue

            if hd > time_exit and cur > time_exit_profit:
                sells.append((p, cp, "TC", None)); continue
        return sells

import app.backtest.simple_runner as sr
orig = sr.FastEngine
sr.FastEngine = ATREngine

def run(label, atr_mul_hs, atr_mul_tr):
    p = {**BASE, 'atr_mul_hs': atr_mul_hs, 'atr_mul_tr': atr_mul_tr, '_atr_map': atr_map}
    r = run_backtest(p)
    s = r['summary']
    return {'label':label,'ret':s['total_return'],'dd':s['max_drawdown'],'wr':s['win_rate'],
            'calmar':s['calmar'],'pf':s['profit_factor'],'trades':s['trades'],
            'sharpe':s['sharpe'],'exit':s['exit_reasons']}

# ── Phase 1: 单独 ATR 硬止损 ──
print("\n=== Phase 1: ATR 硬止损 (固定 TR) ===")
for mul in [1.5, 2.0, 2.5, 3.0]:
    label = f"ATR-HS={mul}x"
    r = run(label, mul, 0)
    print(f"  {label}: ret={r['ret']:.1f}% dd={r['dd']:.1f}% calmar={r['calmar']:.1f} wr={r['wr']:.0f}% trades={r['trades']} HS={r['exit'].get('HS',0)}+{r['exit'].get('HSa',0)}")

# ── Phase 2: 单独 ATR 移动止盈 ──
print("\n=== Phase 2: ATR 移动止盈 (固定 HS) ===")
for mul in [1.0, 1.5, 2.0, 2.5]:
    label = f"ATR-TR={mul}x"
    r = run(label, 0, mul)
    print(f"  {label}: ret={r['ret']:.1f}% dd={r['dd']:.1f}% calmar={r['calmar']:.1f} wr={r['wr']:.0f}% trades={r['trades']}")

# ── Phase 3: 组合 ──
print("\n=== Phase 3: ATR HS + ATR TR 组合 ===")
combos = [(2.0, 1.5), (2.5, 1.5), (2.0, 2.0), (2.5, 2.0)]
for hs_mul, tr_mul in combos:
    label = f"HS={hs_mul}x+TR={tr_mul}x"
    r = run(label, hs_mul, tr_mul)
    print(f"  {label}: ret={r['ret']:.1f}% dd={r['dd']:.1f}% calmar={r['calmar']:.1f} wr={r['wr']:.0f}% trades={r['trades']}")

# ── Baseline ──
sr.FastEngine = orig
p0 = {**BASE, 'atr_mul_hs': 0, 'atr_mul_tr': 0, '_atr_map': {}}
r0 = run_backtest(p0)
s0 = r0['summary']
print(f"\n=== Baseline (无ATR) ===")
print(f"  ret={s0['total_return']:.1f}% dd={s0['max_drawdown']:.1f}% calmar={s0['calmar']:.1f} wr={s0['win_rate']:.0f}% trades={s0['trades']} pf={s0['profit_factor']:.2f}")
