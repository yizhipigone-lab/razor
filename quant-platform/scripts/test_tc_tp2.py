"""Test TC variations and TP2 modification"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.backtest.simple_runner import run_backtest, FastEngine
from datetime import date
import numpy as np
from collections import Counter

BASE = {
    'initial_capital': 1_000_000, 'position_size': 50_000, 'min_buy_amt': 5_000,
    'hard_stop': -0.06, 'tp1_pct': 0.04, 'tp1_sell_ratio': 0.15,
    'trail_activate': 0.03, 'trail_dd': 0.01,
    'time_exit_profit': 0.03, 'time_force_days': 9,
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

def run_and_print(label, params):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    result = run_backtest(params)
    s = result['summary']
    print(f"  总收益: {s['total_return']:.2f}%  最大回撤: {s['max_drawdown']:.2f}%  胜率: {s['win_rate']:.1f}%")
    print(f"  交易: {s['trades']}笔  盈利因子: {s['profit_factor']:.2f}  卡玛: {s['calmar']:.2f}")
    print(f"  均盈: +{s['avg_win']:.2f}%  均亏: {s['avg_loss']:.2f}%")
    print(f"  年化: {s['ann_return']:.2f}%  夏普: {s['sharpe']:.2f}")
    print(f"  退出: {s['exit_reasons']}")
    print(f"  净值: {s['initial_capital']:,} -> {s['final_equity']:,.0f}")
    return s

# ── 基线 ──
base_params = {**BASE, 'tp2_pct': 0.16, 'time_exit_days': 3}
baseline = run_and_print("基线 (D3: TC=3天, TP2=16%全清)", base_params)

# ── TC 变体 ──
for tc_days in [5, 7]:
    p = {**BASE, 'tp2_pct': 0.16, 'time_exit_days': tc_days}
    run_and_print(f"TC={tc_days}天 (TP2=16%全清)", p)

# ── TP2 改良: 10% 卖 50%，剩余用 TR ──
# 需要修改引擎逻辑：TP2 改为部分卖出
class ModifiedEngine(FastEngine):
    """TP2 改良版: 10% 卖 50%，剩余用 TR"""
    def check_stops(self, d, snap, prev_snap=None):
        sells = []
        hs = self.p['hard_stop']
        tp1_pct = self.p['tp1_pct']
        tp1_ratio = self.p['tp1_sell_ratio']
        tp2_pct = self.p['tp2_pct']
        tp2_ratio = self.p.get('tp2_sell_ratio', 1.0)  # 新增：TP2 卖出比例
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

            # 除权保护
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

            # TP2 改良: 卖 tp2_ratio%，不清仓
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

# Monkey-patch
import app.backtest.simple_runner as sr
_orig_engine = sr.FastEngine
sr.FastEngine = ModifiedEngine

tp2_params = {**BASE, 'tp2_pct': 0.10, 'tp2_sell_ratio': 0.50, 'time_exit_days': 3}
run_and_print("TP2改良: 10%卖50%+剩余TR (TC=3天)", tp2_params)

# 恢复
sr.FastEngine = _orig_engine
