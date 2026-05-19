"""TP2最优组合: 7%卖25%+TR, 2023.1.1至今"""
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
    'start_date': date(2023, 1, 1), 'end_date': date(2026, 5, 12),
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
                        r = cp / prev_bar['close']
                        p.entry_price *= r; p.peak_price *= r
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

print("Running TP2=7%卖25%+TR  [2023.1.1 ~ 2026.5.12] ...")
params_new = {**BASE, 'tp2_pct': 0.07, 'tp2_sell_ratio': 0.25}
result = run_backtest(params_new)
s = result['summary']
print(f"\n=== TP2改良: 7%卖25%+TR ===")
print(f"总收益: {s['total_return']:.2f}%  最大回撤: {s['max_drawdown']:.2f}%  胜率: {s['win_rate']:.1f}%")
print(f"卡玛: {s['calmar']:.2f}  夏普: {s['sharpe']:.2f}  索提诺: {s['sortino']:.2f}")
print(f"年化: {s['ann_return']:.2f}%  盈利因子: {s['profit_factor']:.2f}")
print(f"交易: {s['trades']}笔  赢{s['wins']} 亏{s['losses']}")
print(f"均赢: +{s['avg_win']:.2f}%  均亏: {s['avg_loss']:.2f}%")
print(f"均赢持{s['avg_hold_win']:.1f}天  均亏持{s['avg_hold_loss']:.1f}天")
print(f"最佳: +{s['best_trade']:.2f}%  最差: {s['worst_trade']:.2f}%")
print(f"退出: {s['exit_reasons']}")
print(f"区间: {s['start_date']} ~ {s['end_date']}  {s['trading_days']}天")
print(f"净值: {s['initial_capital']:,} -> {s['final_equity']:,.0f}")
print(f"盈利月: {s['positive_months']}")

# Baseline for comparison
sr.FastEngine = FastEngine
print("\nRunning baseline TP2=16%全清 ...")
params_base = {**BASE, 'tp2_pct': 0.16}
result2 = run_backtest(params_base)
s2 = result2['summary']
print(f"\n=== 基线: TP2=16%全清 ===")
print(f"总收益: {s2['total_return']:.2f}%  最大回撤: {s2['max_drawdown']:.2f}%  胜率: {s2['win_rate']:.1f}%")
print(f"卡玛: {s2['calmar']:.2f}  夏普: {s2['sharpe']:.2f}  盈利因子: {s2['profit_factor']:.2f}")
print(f"交易: {s2['trades']}笔  年化: {s2['ann_return']:.2f}%")
print(f"退出: {s2['exit_reasons']}")
print(f"净值: {s2['initial_capital']:,} -> {s2['final_equity']:,.0f}")

print(f"\n=== 对比 ===")
print(f"收益: {s2['total_return']:.1f}% -> {s['total_return']:.1f}%  ({(s['total_return']-s2['total_return']):+.1f}%)")
print(f"回撤: {s2['max_drawdown']:.1f}% -> {s['max_drawdown']:.1f}%  ({(abs(s2['max_drawdown'])-abs(s['max_drawdown'])):+.1f}%)")
print(f"卡玛: {s2['calmar']:.1f} -> {s['calmar']:.1f}  ({(s['calmar']-s2['calmar']):+.1f})")
print(f"胜率: {s2['win_rate']:.0f}% -> {s['win_rate']:.0f}%")
