#!/usr/bin/env python
"""
参数优化扫描：测试多组止盈止损边界条件，找出最优组�?使用日线收盘价回测，2024-01-01 至今
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta
from collections import defaultdict, Counter
from pathlib import Path
import time, json, warnings
warnings.filterwarnings('ignore')

from app.screener.strategies.ma5_angle import generate_signals

START = date(2024, 1, 1)
END   = date.today()
ROOT  = Path(__file__).parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"

# ── 固定参数 ──
INITIAL_CAPITAL = 1_000_000
POSITION_SIZE   = 50_000
MIN_BUY_AMT     = 5_000
LOSS_STREAK_HALVE = 3
LOSS_STREAK_PAUSE = 5
PAUSE_DAYS        = 3
STRATEGY_NAME = "ma5_angle"

SIGNAL_PARAMS = {
    "version": "improved",
    "filter_st": True,
    "filter_bj": True,
    "vol_threshold": 1.5,
    "close_position_threshold": 0.8,
    "disable_quality_sort": False,
    "filter_consecutive_up": False,
    "filter_gap_quality": False,
}

# ══════════════════════════════════════════════════════════�?# 回测引擎（参数化版本�?# ══════════════════════════════════════════════════════════�?
class Position:
    __slots__ = ('code','entry_date','entry_price','shares','cost','peak_price','remaining','tp1','tp2','active','strategy')
    def __init__(self, c, d, px, sh, cost, s=""):
        self.code=c; self.entry_date=d; self.entry_price=px; self.shares=sh; self.cost=cost
        self.peak_price=px; self.remaining=sh; self.tp1=False; self.tp2=False; self.active=True; self.strategy=s

class Trade:
    __slots__ = ('code','entry_date','exit_date','entry_px','exit_px','shares','ret','profit','reason','hold','strategy','timing')
    def __init__(self, c, ed, xd, ep, xp, sh, ret, profit, reason, hold, s="", t="close"):
        self.code=c; self.entry_date=ed; self.exit_date=xd; self.entry_px=ep; self.exit_px=xp
        self.shares=sh; self.ret=ret; self.profit=profit; self.reason=reason; self.hold=hold
        self.strategy=s; self.timing=t

class Engine:
    def __init__(self, td_list, params):
        self.cash = INITIAL_CAPITAL
        self.positions = {}; self.trades = []; self.equity = []
        self.cl = 0; self.pause = None; self.td_list = td_list
        self.halves = []; self.pauses = []
        self.p = params  # 参数字典

    def max_pos(self):
        return POSITION_SIZE/2 if self.cl >= LOSS_STREAK_HALVE else POSITION_SIZE

    def pos_n(self):
        return sum(1 for p in self.positions.values() if p.active)

    def eq(self, prices):
        pv = 0
        for p in self.positions.values():
            if not p.active: continue
            bar = prices.get(p.code, {})
            px = bar.get('close', p.entry_price) if isinstance(bar, dict) else (bar if bar else p.entry_price)
            pv += p.remaining * px
        return self.cash + pv

    def _td(self, d1, d2):
        return sum(1 for td in self.td_list if d1 <= td <= d2)

    def buy(self, d, code, px):
        if code in self.positions: return None
        ma = min(self.max_pos(), self.cash)
        if ma < MIN_BUY_AMT: return None
        sh = int(ma/px/100)*100
        if sh < 100: return None
        cost = sh * px
        if cost > self.cash: return None
        p = Position(code, d, px, sh, cost, STRATEGY_NAME)
        self.cash -= cost; self.positions[code] = p
        return p

    def stops(self, d, snap):
        sells = []
        for code, p in list(self.positions.items()):
            if not p.active or p.remaining <= 0: continue
            bar = snap.get(code)
            if bar is None: continue
            cp = bar['close']; hp = bar.get('high', cp)
            if hp > p.peak_price: p.peak_price = hp
            pp = p.peak_price/p.entry_price-1
            cur = cp/p.entry_price-1
            hd = self._td(p.entry_date, d)

            if cur <= self.p['hard_stop']:
                sells.append((p, cp, f"HS({cur*100:.1f}%)", None)); continue
            if hd > self.p['time_force_days']:
                sells.append((p, cp, f"TF({hd}d)", None)); continue
            if not p.tp2 and cur >= self.p['tp2_pct']:
                sells.append((p, cp, f"TP2({cur*100:.1f}%)", None)); continue
            if not p.tp1 and cur >= self.p['tp1_pct']:
                ss = int(p.remaining*self.p['tp1_sell_ratio']/100)*100
                if ss >= 100:
                    sells.append((p, cp, f"TP1({cur*100:.1f}%)", ss)); continue
            if pp >= self.p['trail_activate']:
                dd = cp/p.peak_price-1
                if dd <= -self.p['trail_dd']:
                    tp = p.peak_price*(1-self.p['trail_dd'])
                    sells.append((p, tp, f"TR({pp*100:.1f}%>{dd*100:.1f}%)", None)); continue
            if hd > self.p['time_exit_days'] and cur > 0.01:
                sells.append((p, cp, f"TC({hd}d+{cur*100:.1f}%)", None)); continue
        return sells

    def sell(self, p, px, reason, partial=None, xd=None):
        ss = partial if partial else p.remaining
        ss = int(ss//100*100)
        if ss <= 0: return None
        ret = (px/p.entry_price-1)*100
        profit = ss*(px-p.entry_price)
        p.remaining -= ss
        if "TP2" in reason: p.tp2 = True
        if "TP1" in reason: p.tp1 = True
        if p.remaining <= 0: p.active = False; p.remaining = 0
        self.cash += ss*px
        return Trade(p.code, p.entry_date, xd or date.today(),
                     p.entry_price, px, ss, ret, profit, reason, 0, p.strategy, "close")

    def sell_phase(self, d, snap):
        for p, px, reason, partial in self.stops(d, snap):
            t = self.sell(p, px, reason, partial, d)
            if t:
                t.hold = self._td(p.entry_date, d); self.trades.append(t)
                if t.ret <= 0:
                    self.cl += 1
                    if self.cl == LOSS_STREAK_HALVE: self.halves.append((d, self.cl, t.code, t.ret))
                    if self.cl >= LOSS_STREAK_PAUSE: self.pause = d + timedelta(days=PAUSE_DAYS); self.pauses.append((d, self.pause, self.cl))
                else: self.cl = 0; self.pause = None
        self.positions = {k:v for k,v in self.positions.items() if v.active}

    def record(self, d, prices):
        eq = self.eq(prices)
        self.equity.append({'date':d,'equity':eq,'cash':self.cash,'pos':self.pos_n()})


# ══════════════════════════════════════════════════════════�?# 数据加载
# ══════════════════════════════════════════════════════════�?
def load_daily():
    files = [f for f in DAILY_DIR.glob("*.parquet")
             if not f.stem.startswith('index_') and len(f.stem)==6 and f.stem.isdigit()]
    dfs = []
    for f in files:
        try: df = pd.read_parquet(str(f))
        except: continue
        cmap = {}
        for c in df.columns:
            cl = c.lower()
            if cl in ('vol','volume') and 'volume' not in df.columns: cmap[c] = 'volume'
            elif cl in ('trade_date','datetime') and c != 'date' and 'date' not in df.columns: cmap[c] = 'date'
        if cmap: df.rename(columns=cmap, inplace=True)
        df = df.loc[:, ~df.columns.duplicated()].copy()
        keep = [c for c in ['date','open','high','low','close','volume'] if c in df.columns]
        if 'date' not in keep or 'close' not in keep: continue
        df = df[keep].copy(); df['code'] = f.stem
        df['date'] = pd.to_datetime(df['date']).dt.date
        dfs.append(df)
    bars = pd.concat(dfs, ignore_index=True)
    for c in ['open','high','low','close','volume']:
        if c in bars.columns: bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=['close'])
    bars = bars[(bars['date']>=date(2023,6,1))&(bars['date']<=END)]
    return bars.sort_values(['code','date']).reset_index(drop=True)


def run_one(params, bars, sig, td, closes, highs, sbd):
    """运行单次回测，返回结果字�?""
    eng = Engine(td, params)
    for i, d in enumerate(td):
        snap = {}
        for code in eng.positions:
            if d in closes and code in closes[d]:
                snap[code] = {'open': closes[d].get(code, 0),
                              'high': highs[d].get(code, closes[d].get(code, 0)),
                              'low': closes[d].get(code, 0),
                              'close': closes[d].get(code, 0)}
        eng.sell_phase(d, snap)
        paused = eng.pause is not None and d <= eng.pause
        if d in sbd and not paused:
            for code, px in sbd[d]:
                if eng.cash < min(eng.max_pos(), MIN_BUY_AMT): break
                if any(t.code == code and (d - t.entry_date).days <= params['same_stock_cooldown']
                       for t in eng.trades): continue
                eng.buy(d, code, px)
        eng.record(d, snap)

    trades = eng.trades
    if not trades:
        return None

    eq = pd.DataFrame(eng.equity)
    fe = eq['equity'].iloc[-1]
    total_ret = (fe / INITIAL_CAPITAL - 1) * 100
    eq['cmax'] = eq['equity'].cummax()
    eq['dd'] = (eq['equity'] - eq['cmax']) / eq['cmax'] * 100
    max_dd = eq['dd'].min()

    n = len(trades)
    wins = [t for t in trades if t.ret > 0]
    loses = [t for t in trades if t.ret <= 0]
    nw, nl = len(wins), len(loses)
    wr = nw / n * 100 if n > 0 else 0
    aw = np.mean([t.ret for t in wins]) if wins else 0
    al = np.mean([t.ret for t in loses]) if loses else 0

    # 计算 Calmar 比率（年化）
    days = (td[-1] - td[0]).days
    ann_ret = (1 + total_ret/100) ** (365/max(days,1)) - 1
    calmar = ann_ret / abs(max_dd/100) if max_dd != 0 else 0

    # 盈亏�?    profit_factor = sum(t.profit for t in wins) / abs(sum(t.profit for t in loses)) if loses and sum(t.profit for t in loses) != 0 else 0

    # 退出原因分�?    rc = Counter(t.reason.split('(')[0] for t in trades)

    return {
        'total_ret': round(total_ret, 2),
        'max_dd': round(max_dd, 2),
        'trades': n, 'wins': nw, 'losses': nl,
        'win_rate': round(wr, 1),
        'avg_win': round(aw, 2), 'avg_loss': round(al, 2),
        'calmar': round(calmar, 2),
        'profit_factor': round(profit_factor, 2),
        'final_equity': round(fe, 0),
        'exit_reasons': dict(rc.most_common()),
        'halves': len(eng.halves),
        'pauses': len(eng.pauses),
    }


# ══════════════════════════════════════════════════════════�?# 参数空间定义
# ══════════════════════════════════════════════════════════�?
BASELINE = {
    'hard_stop': -0.055,
    'tp1_pct': 0.04, 'tp1_sell_ratio': 0.20,
    'tp2_pct': 0.14,
    'trail_activate': 0.08, 'trail_dd': 0.02,
    'time_exit_days': 5, 'time_force_days': 10,
    'same_stock_cooldown': 20,
}

# 各参数候选�?CANDIDATES = {
    'hard_stop': [-0.045, -0.050, -0.055, -0.060, -0.065, -0.070],
    'tp1_pct': [0.03, 0.04, 0.05, 0.06],
    'tp2_pct': [0.10, 0.12, 0.14, 0.16, 0.18],
    'trail_activate': [0.05, 0.06, 0.08, 0.10, 0.12],
    'trail_dd': [0.015, 0.02, 0.025, 0.03],
    'time_exit_days': [3, 5, 7, 10],
    'time_force_days': [8, 10, 12, 15],
    'same_stock_cooldown': [10, 15, 20, 25, 30],
}

def make_params(**overrides):
    p = dict(BASELINE)
    p.update(overrides)
    return p


# ══════════════════════════════════════════════════════════�?# Main
# ══════════════════════════════════════════════════════════�?
if __name__ == "__main__":
    t0 = time.time()
    print("=" * 70)
    print("  参数优化扫描 �?多组边界条件回测")
    print(f"  区间: {START} ~ {END}")
    print("=" * 70)

    # 加载数据（只做一次）
    print("\n[1/3] 加载数据...")
    bars = load_daily()
    print(f"  {bars.code.nunique():,} stocks, {len(bars):,} rows")

    print("[2/3] 生成信号...")
    sig = generate_signals(bars, **SIGNAL_PARAMS)
    sig = sig[(sig['date'] >= START) & (sig['date'] <= END)].copy()
    sig['date'] = pd.to_datetime(sig['date']).dt.date

    bt = bars[(bars['date'] >= START) & (bars['date'] <= END)]
    closes, highs = {}, {}
    for d, g in bt.groupby('date'):
        closes[d] = dict(zip(g['code'], g['close']))
        highs[d] = dict(zip(g['code'], g['high']))
    td = sorted(closes.keys())
    sbd = defaultdict(list)
    for _, r in sig.iterrows():
        sbd[r['date']].append((r['code'], float(r['close'])))

    print(f"  交易�? {len(td)} | 信号: {len(sig):,}")

    # ── 构建待测参数列表 ──
    trials = []

    # 1. Baseline
    trials.append(("BASELINE", BASELINE))

    # 2. 单参数灵敏度：每个参数取�?baseline 的�?    sensitivity_keys = [
        ('hard_stop', 'HS'),
        ('tp1_pct', 'TP1%'),
        ('tp2_pct', 'TP2%'),
        ('trail_activate', 'Trail激�?),
        ('trail_dd', 'Trail回撤'),
        ('time_exit_days', '时间止盈�?),
        ('time_force_days', '强制清仓�?),
        ('same_stock_cooldown', '冷却�?),
    ]

    for key, label in sensitivity_keys:
        for val in CANDIDATES[key]:
            if val == BASELINE[key]:
                continue
            name = f"{label}={val}"
            trials.append((name, make_params(**{key: val})))

    # 3. 组合测试：有意义的参数组�?    combos = [
        # 激进止�?+ 紧止�?        ("激进止�?, make_params(tp1_pct=0.03, tp2_pct=0.10, trail_activate=0.05, trail_dd=0.015)),
        # 保守止盈 + 宽松止损
        ("保守止盈", make_params(tp1_pct=0.06, tp2_pct=0.18, trail_activate=0.12, trail_dd=0.03, hard_stop=-0.070)),
        # 短线快进快出
        ("短线快出", make_params(time_exit_days=3, time_force_days=8, same_stock_cooldown=10)),
        # 长线持有
        ("长线持有", make_params(time_exit_days=10, time_force_days=15, same_stock_cooldown=30)),
        # 紧止�?+ 快出
        ("紧止损快�?, make_params(hard_stop=-0.040, time_exit_days=3, trail_activate=0.05, trail_dd=0.015)),
        # 宽止�?+ 慢出
        ("宽止损慢�?, make_params(hard_stop=-0.070, time_exit_days=10, trail_activate=0.10, trail_dd=0.03)),
        # 当前最优猜测（基于之前回测�?        ("优化组合A", make_params(hard_stop=-0.055, tp1_pct=0.04, tp2_pct=0.14,
                                   trail_activate=0.08, trail_dd=0.02,
                                   time_exit_days=5, time_force_days=10,
                                   same_stock_cooldown=20)),
        # 紧止�?中等止损
        ("优化组合B", make_params(hard_stop=-0.050, tp1_pct=0.03, tp2_pct=0.12,
                                   trail_activate=0.06, trail_dd=0.02,
                                   time_exit_days=5, time_force_days=10,
                                   same_stock_cooldown=15)),
        # 中等止盈+紧止�?        ("优化组合C", make_params(hard_stop=-0.045, tp1_pct=0.04, tp2_pct=0.14,
                                   trail_activate=0.08, trail_dd=0.015,
                                   time_exit_days=5, time_force_days=10,
                                   same_stock_cooldown=20)),
    ]
    trials.extend(combos)

    # 4. 关键维度交叉：止盈空�?× 止损空间
    for hs in [-0.050, -0.060]:
        for tp2 in [0.12, 0.16]:
            for trail_dd in [0.015, 0.025]:
                trials.append((f"交叉 HS={hs} TP2={tp2} DD={trail_dd}",
                               make_params(hard_stop=hs, tp2_pct=tp2, trail_dd=trail_dd)))

    print(f"\n[3/3] 运行 {len(trials)} 组参�?..")
    results = []
    best_score = -999

    for idx, (name, params) in enumerate(trials):
        t1 = time.time()
        r = run_one(params, bars, sig, td, closes, highs, sbd)
        elapsed = time.time() - t1

        if r is None:
            print(f"  [{idx+1:>3}/{len(trials)}] {name:<30} 无交易，跳过")
            continue

        # 综合评分：Calmar × 胜率因子 × 交易充足�?        # Calmar 是核心，但需要惩罚交易太少的情况
        trade_bonus = min(1.0, r['trades'] / 200)  # 少于200笔折�?        score = r['calmar'] * (r['win_rate'] / 50) * trade_bonus

        r['name'] = name
        r['params'] = {k: v for k, v in params.items()}
        r['score'] = round(score, 2)
        r['time'] = round(elapsed, 1)
        results.append(r)

        marker = " �? if score > best_score else ""
        if score > best_score:
            best_score = score

        print(f"  [{idx+1:>3}/{len(trials)}] {name:<30} "
              f"收益{r['total_ret']:>+7.2f}% DD{r['max_dd']:>+6.2f}% "
              f"胜率{r['win_rate']:>5.1f}% Calmar{r['calmar']:>6.2f} "
              f"交易{r['trades']:>4} 耗时{elapsed:.0f}s{marker}")

    # ── 排序输出 ──
    results.sort(key=lambda x: x['score'], reverse=True)

    print(f"\n{'='*70}")
    print(f"  TOP 20 排名（评�?= Calmar × 胜率因子 × 交易充足度）")
    print(f"{'='*70}")
    print(f"  {'排名':<5} {'名称':<32} {'收益%':>7} {'回撤%':>6} {'胜率%':>6} {'Calmar':>7} {'交易':>5} {'评分':>6}")
    print(f"  {'-'*80}")

    for i, r in enumerate(results[:20]):
        print(f"  {i+1:<5} {r['name']:<32} {r['total_ret']:>+7.2f} {r['max_dd']:>+6.2f} "
              f"{r['win_rate']:>5.1f} {r['calmar']:>7.2f} {r['trades']:>5} {r['score']:>6.2f}")

    # 输出最佳参数详�?    best = results[0]
    print(f"\n{'='*70}")
    print(f"  🏆 最优参数组�? {best['name']}")
    print(f"{'='*70}")
    print(f"  总收�? {best['total_ret']:+.2f}%  |  最大回�? {best['max_dd']:+.2f}%")
    print(f"  胜率: {best['win_rate']:.1f}%  |  Calmar: {best['calmar']:.2f}")
    print(f"  交易: {best['trades']}�? |  盈利{best['wins']}/亏损{best['losses']}")
    print(f"  均盈: {best['avg_win']:+.2f}%  |  均亏: {best['avg_loss']:+.2f}%")
    print(f"  盈亏�? {best['profit_factor']:.2f}")
    print(f"  连亏减仓: {best['halves']}�? |  暂停: {best['pauses']}�?)
    print(f"\n  最佳参�?")
    for k, v in best['params'].items():
        base = BASELINE.get(k)
        mark = f" (基线={base})" if base is not None and v != base else ""
        print(f"    {k}: {v}{mark}")
    print(f"\n  退出原因分�?")
    for reason, count in sorted(best['exit_reasons'].items(), key=lambda x: -x[1]):
        print(f"    {reason}: {count}�?)

    # 保存结果
    out_dir = ROOT / "output" / "param_sweep"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"sweep_{date.today().strftime('%Y%m%d')}.json"

    # 清理不可序列化的类型
    clean_results = []
    for r in results:
        cr = dict(r)
        cr['params'] = {k: float(v) if isinstance(v, (np.floating,)) else v for k, v in r['params'].items()}
        clean_results.append(cr)

    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(clean_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  结果已保存至: {out_file}")
    print(f"  总耗时: {time.time()-t0:.0f}s")
