#!/usr/bin/env python
"""
Trail参数网格搜索 + 2023-01-01 完整回测
- trail_activate × trail_dd 5×5 网格
- 日线收盘价，2024-01-01 至今
- 同步�?2023-01-01 至今完整回测
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta
from collections import defaultdict, Counter
from pathlib import Path
import time, json, warnings
warnings.filterwarnings('ignore')

from app.screener.strategies.ma5_angle import generate_signals

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
    "vol_threshold": 1.5, "close_position_threshold": 0.8,
    "disable_quality_sort": False,
    "filter_consecutive_up": False, "filter_gap_quality": False,
}

# ══════════════════════════════════════════════════════════�?# 回测引擎
# ══════════════════════════════════════════════════════════�?
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
        self.p = params

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
                sells.append((p, cp, f"HS", None)); continue
            if hd > self.p['time_force_days']:
                sells.append((p, cp, f"TF", None)); continue
            if not p.tp2 and cur >= self.p['tp2_pct']:
                sells.append((p, cp, f"TP2", None)); continue
            if not p.tp1 and cur >= self.p['tp1_pct']:
                ss = int(p.remaining*self.p['tp1_sell_ratio']/100)*100
                if ss >= 100:
                    sells.append((p, cp, f"TP1", ss)); continue
            if pp >= self.p['trail_activate']:
                dd = cp/p.peak_price-1
                if dd <= -self.p['trail_dd']:
                    tp = p.peak_price*(1-self.p['trail_dd'])
                    sells.append((p, tp, f"TR", None)); continue
            if hd > self.p['time_exit_days'] and cur > 0.01:
                sells.append((p, cp, f"TC", None)); continue
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


# ══════════════════════════════════════════════════════════�?
BASELINE = {
    'hard_stop': -0.055,
    'tp1_pct': 0.04, 'tp1_sell_ratio': 0.20,
    'tp2_pct': 0.14,
    'trail_activate': 0.05, 'trail_dd': 0.02,
    'time_exit_days': 5, 'time_force_days': 10,
    'same_stock_cooldown': 20,
}


def load_daily(start_buffer=date(2022, 6, 1)):
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
    bars = bars[(bars['date']>=start_buffer)]
    return bars.sort_values(['code','date']).reset_index(drop=True)


def run_one(params, bars, sig, td, closes, highs, sbd):
    eng = Engine(td, params)
    for d in td:
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

    days_span = (td[-1] - td[0]).days
    ann_ret = (1 + total_ret/100) ** (365/max(days_span,1)) - 1
    calmar = ann_ret / abs(max_dd/100) if max_dd != 0 else 0

    rc = Counter(t.reason for t in trades)
    profit_factor = sum(t.profit for t in wins) / abs(sum(t.profit for t in loses)) if loses and sum(t.profit for t in loses) != 0 else 0

    return {
        'total_ret': round(total_ret, 2), 'max_dd': round(max_dd, 2),
        'trades': n, 'wins': nw, 'losses': nl,
        'win_rate': round(wr, 1),
        'avg_win': round(aw, 2), 'avg_loss': round(al, 2),
        'calmar': round(calmar, 2),
        'profit_factor': round(profit_factor, 2),
        'final_equity': round(fe, 0),
        'exit_reasons': dict(rc.most_common()),
        'halves': len(eng.halves), 'pauses': len(eng.pauses),
    }


# ══════════════════════════════════════════════════════════�?# Main
# ══════════════════════════════════════════════════════════�?
if __name__ == "__main__":
    t0 = time.time()
    print("=" * 80)
    print("  Trail参数网格搜索 + 2023完整回测")
    print("=" * 80)

    # ── Part A: Trail 网格搜索 (2024-01-01 ~ now) ──
    print("\n" + "=" * 80)
    print("  Part A: Trail 参数 5x5 网格搜索")
    print("=" * 80)

    START_A = date(2024, 1, 1)
    END_A = date.today()

    print("\n[A1] 加载数据...")
    bars_a = load_daily(date(2023, 6, 1))
    bars_a = bars_a[bars_a['date'] <= END_A]
    print(f"  {bars_a.code.nunique():,} stocks, {len(bars_a):,} rows")

    print("[A2] 生成信号...")
    sig_a = generate_signals(bars_a, **SIGNAL_PARAMS)
    sig_a = sig_a[(sig_a['date'] >= START_A) & (sig_a['date'] <= END_A)].copy()
    sig_a['date'] = pd.to_datetime(sig_a['date']).dt.date

    bt_a = bars_a[(bars_a['date'] >= START_A) & (bars_a['date'] <= END_A)]
    closes_a, highs_a = {}, {}
    for d, g in bt_a.groupby('date'):
        closes_a[d] = dict(zip(g['code'], g['close']))
        highs_a[d] = dict(zip(g['code'], g['high']))
    td_a = sorted(closes_a.keys())
    sbd_a = defaultdict(list)
    for _, r in sig_a.iterrows():
        sbd_a[r['date']].append((r['code'], float(r['close'])))

    print(f"  交易�? {len(td_a)} | 信号: {len(sig_a):,}")

    # 5x5 grid
    trail_act_vals = [0.03, 0.04, 0.05, 0.06, 0.07]
    trail_dd_vals  = [0.010, 0.015, 0.020, 0.025, 0.030]

    print(f"\n[A3] 运行 {len(trail_act_vals)}x{len(trail_dd_vals)} = {len(trail_act_vals)*len(trail_dd_vals)} �?..")
    grid_results = []
    best_calmar = -999

    for ta in trail_act_vals:
        for td_val in trail_dd_vals:
            params = dict(BASELINE)
            params['trail_activate'] = ta
            params['trail_dd'] = td_val
            name = f"TA={ta:.2f} DD={td_val:.3f}"

            t1 = time.time()
            r = run_one(params, bars_a, sig_a, td_a, closes_a, highs_a, sbd_a)
            elapsed = time.time() - t1

            if r is None:
                print(f"  {name:<22} 无交�?)
                continue

            r['name'] = name
            r['params'] = dict(params)
            r['time'] = round(elapsed, 1)
            grid_results.append(r)

            marker = " <-- BEST" if r['calmar'] > best_calmar else ""
            if r['calmar'] > best_calmar:
                best_calmar = r['calmar']

            print(f"  {name:<22} 收益{r['total_ret']:>+7.2f}% DD{r['max_dd']:>+6.2f}% "
                  f"胜率{r['win_rate']:>5.1f}% Calmar{r['calmar']:>7.2f} "
                  f"交易{r['trades']:>5} TR退出{r['exit_reasons'].get('TR',0):>4}{marker}")

    # 网格排名
    grid_results.sort(key=lambda x: x['calmar'], reverse=True)
    print(f"\n  ┌{'─'*76}�?)
    print(f"  �? {'Trail参数网格 TOP 10 (按Calmar排名)':<66}�?)
    print(f"  ├{'─'*76}�?)
    print(f"  �?{'激�?':>6} {'回撤%':>6} {'收益%':>8} {'回撤%':>7} {'胜率%':>6} {'Calmar':>7} {'交易':>5} {'TR退�?:>6} �?)
    for r in grid_results[:10]:
        print(f"  �?{r['params']['trail_activate']*100:>5.1f} {r['params']['trail_dd']*100:>5.1f} "
              f"{r['total_ret']:>+7.2f} {r['max_dd']:>+6.2f} "
              f"{r['win_rate']:>5.1f} {r['calmar']:>7.2f} {r['trades']:>5} "
              f"{r['exit_reasons'].get('TR',0):>6} �?)
    print(f"  └{'─'*76}�?)

    # ── Part B: 2023-01-01 完整回测 ──
    print("\n" + "=" * 80)
    print("  Part B: 2023-01-01 至今完整回测 (日线收盘�?")
    print("=" * 80)

    START_B = date(2023, 1, 1)

    print("\n[B1] 加载数据...")
    bars_b = load_daily(date(2022, 6, 1))
    print(f"  {bars_b.code.nunique():,} stocks, {len(bars_b):,} rows")

    print("[B2] 生成信号...")
    sig_b = generate_signals(bars_b, **SIGNAL_PARAMS)
    sig_b = sig_b[(sig_b['date'] >= START_B) & (sig_b['date'] <= END_A)].copy()
    sig_b['date'] = pd.to_datetime(sig_b['date']).dt.date

    bt_b = bars_b[(bars_b['date'] >= START_B) & (bars_b['date'] <= END_A)]
    closes_b, highs_b = {}, {}
    for d, g in bt_b.groupby('date'):
        closes_b[d] = dict(zip(g['code'], g['close']))
        highs_b[d] = dict(zip(g['code'], g['high']))
    td_b = sorted(closes_b.keys())
    sbd_b = defaultdict(list)
    for _, r in sig_b.iterrows():
        sbd_b[r['date']].append((r['code'], float(r['close'])))

    print(f"  交易�? {len(td_b)} | 信号: {len(sig_b):,}")

    # 跑三组：当前最优、baseline(TA=0.08)、网格最�?    configs_2023 = [
        ("当前参数(TA=0.05)", dict(BASELINE)),
        ("旧参�?TA=0.08)", dict(BASELINE, trail_activate=0.08)),
    ]
    if grid_results:
        best_grid = grid_results[0]
        configs_2023.append(("网格最�?, dict(best_grid['params'])))

    print(f"\n[B3] 运行 {len(configs_2023)} �?..")
    results_2023 = []
    for name, params in configs_2023:
        t1 = time.time()
        r = run_one(params, bars_b, sig_b, td_b, closes_b, highs_b, sbd_b)
        elapsed = time.time() - t1

        if r is None:
            print(f"  {name:<25} 无交�?)
            continue

        r['name'] = name
        r['params'] = {k: v for k, v in params.items()}
        r['time'] = round(elapsed, 1)
        results_2023.append(r)

        print(f"\n  {'='*70}")
        print(f"  {name}")
        print(f"  {'='*70}")
        print(f"  区间: {START_B} ~ {END_A}  ({len(td_b)}个交易日)")
        print(f"  总收�? {r['total_ret']:+.2f}%  |  最大回�? {r['max_dd']:+.2f}%")
        print(f"  期末净�? {r['final_equity']:,.0f}  |  年化Calmar: {r['calmar']:.2f}")
        print(f"  交易: {r['trades']}�? |  盈{r['wins']}/亏{r['losses']}")
        print(f"  胜率: {r['win_rate']:.1f}%  |  均盈{r['avg_win']:+.2f}% / 均亏{r['avg_loss']:+.2f}%")
        print(f"  盈亏�? {r['profit_factor']:.2f}")
        print(f"  连亏减仓: {r['halves']}�? |  暂停: {r['pauses']}�?)
        print(f"  退出分�? {dict(r['exit_reasons'])}")

        # 年度拆分
        eq = pd.DataFrame([{'date': td_b[i], 'equity': eng.equity_curve[i]['equity']}
                           for i, eng in enumerate([None])])  # placeholder
        # Actually need to capture the equity curve from the run...
        # Let me add annual breakdown later

    # ── 年度拆分（用当前最优参数重跑一次，记录详细数据�?──
    print(f"\n  [年度拆分] 当前参数(TA=0.05)")
    eng_full = Engine(td_b, dict(BASELINE))
    for d in td_b:
        snap = {}
        for code in eng_full.positions:
            if d in closes_b and code in closes_b[d]:
                snap[code] = {'open': closes_b[d].get(code, 0),
                              'high': highs_b[d].get(code, closes_b[d].get(code, 0)),
                              'low': closes_b[d].get(code, 0),
                              'close': closes_b[d].get(code, 0)}
        eng_full.sell_phase(d, snap)
        paused = eng_full.pause is not None and d <= eng_full.pause
        if d in sbd_b and not paused:
            for code, px in sbd_b[d]:
                if eng_full.cash < min(eng_full.max_pos(), MIN_BUY_AMT): break
                if any(t.code == code and (d - t.entry_date).days <= BASELINE['same_stock_cooldown']
                       for t in eng_full.trades): continue
                eng_full.buy(d, code, px)
        eng_full.record(d, snap)

    eq_full = pd.DataFrame(eng_full.equity)
    eq_full['year'] = eq_full['date'].apply(lambda d: d.year)
    eq_full['cmax'] = eq_full['equity'].cummax()
    eq_full['dd'] = (eq_full['equity'] - eq_full['cmax']) / eq_full['cmax'] * 100

    for yr, g in eq_full.groupby('year'):
        ret = (g['equity'].iloc[-1] / g['equity'].iloc[0] - 1) * 100
        yt = [t for t in eng_full.trades if t.exit_date.year == yr]
        yw = [t for t in yt if t.ret > 0]
        print(f"  {yr}: 收益{ret:>+8.2f}% 回撤{g['dd'].min():>+7.2f}% 交易{len(yt)}�?胜率{len(yw)/len(yt)*100:.0f}%" if yt else f"  {yr}: 收益{ret:>+8.2f}% 回撤{g['dd'].min():>+7.2f}% 交易0�?)

    print(f"\n  总耗时: {time.time()-t0:.0f}s")
