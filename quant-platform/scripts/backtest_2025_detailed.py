#!/usr/bin/env python
"""
统一回测：2025-01-01 至今，日线 + 5分钟线
读取 config.py 所有参数，输出月度盈亏/连亏减仓/暂停事件
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta
from collections import defaultdict, Counter
from pathlib import Path
import time, gc, warnings
warnings.filterwarnings('ignore')

from app.sim_trader.config import *
# 覆盖回测起止
START = date(2024, 1, 1)
END   = date.today()

ROOT  = Path(__file__).parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
MIN5_DIR  = ROOT / "data" / "parquet" / "min5"

# ==================================================================
class Position:
    __slots__ = ('code','entry_date','entry_price','shares','cost',
                 'peak_price','remaining','tp1','tp2','active','strategy')
    def __init__(self, code, d, px, shares, cost, strategy=""):
        self.code = code; self.entry_date = d; self.entry_price = px
        self.shares = shares; self.cost = cost
        self.peak_price = px; self.remaining = shares
        self.tp1 = False; self.tp2 = False; self.active = True
        self.strategy = strategy

class Trade:
    __slots__ = ('code','entry_date','exit_date','entry_px','exit_px',
                 'shares','ret','profit','reason','hold','strategy','timing')
    def __init__(self, code, ed, xd, ep, xp, sh, ret, profit, reason, hold,
                 strategy="", timing="close"):
        self.code = code; self.entry_date = ed; self.exit_date = xd
        self.entry_px = ep; self.exit_px = xp; self.shares = sh
        self.ret = ret; self.profit = profit; self.reason = reason
        self.hold = hold; self.strategy = strategy; self.timing = timing

class Engine:
    def __init__(self, td_list):
        self.cash = INITIAL_CAPITAL
        self.positions = {}; self.trades = []; self.equity = []
        self.cl = 0; self.pause = None; self.td_list = td_list
        self.halves = []; self.pauses = []

    def max_pos(self):
        return POSITION_SIZE/2 if self.cl >= LOSS_STREAK_HALVE else POSITION_SIZE

    def pos_n(self):
        return sum(1 for p in self.positions.values() if p.active)

    def eq(self, prices):
        pv = 0
        for p in self.positions.values():
            if not p.active: continue
            bar = prices.get(p.code, {})
            if isinstance(bar, dict):
                px = bar.get('close', p.entry_price)
            else:
                px = bar if bar else p.entry_price
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

            if cur <= HARD_STOP:
                sells.append((p, cp, f"HS({cur*100:.1f}%)", None)); continue
            if hd > TIME_FORCE_DAYS:
                sells.append((p, cp, f"TF({hd}d)", None)); continue
            if not p.tp2 and cur >= TP2_PCT:
                sells.append((p, cp, f"TP2({cur*100:.1f}%)", None)); continue
            if not p.tp1 and cur >= TP1_PCT:
                ss = int(p.remaining*TP1_SELL_RATIO/100)*100
                if ss >= 100:
                    sells.append((p, cp, f"TP1({cur*100:.1f}%)", ss)); continue
            if pp >= TRAIL_ACTIVATE:
                dd = cp/p.peak_price-1
                if dd <= -TRAIL_DD:
                    tp = p.peak_price*(1-TRAIL_DD)
                    sells.append((p, tp, f"TR({pp*100:.1f}%>{dd*100:.1f}%)", None)); continue
            if hd > TIME_EXIT_DAYS and cur > TIME_EXIT_PROFIT:
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
                     p.entry_price, px, ss, ret, profit, reason, 0,
                     p.strategy, "close")

    def sell_phase(self, d, snap):
        sells = self.stops(d, snap)
        for p, px, reason, partial in sells:
            t = self.sell(p, px, reason, partial, d)
            if t:
                t.hold = self._td(p.entry_date, d)
                self.trades.append(t)
                if t.ret <= 0:
                    self.cl += 1
                    if self.cl == LOSS_STREAK_HALVE:
                        self.halves.append((d, self.cl, t.code, t.ret))
                    if self.cl >= LOSS_STREAK_PAUSE:
                        self.pause = d + timedelta(days=PAUSE_DAYS)
                        self.pauses.append((d, self.pause, self.cl))
                else:
                    self.cl = 0; self.pause = None
        self.positions = {k:v for k,v in self.positions.items() if v.active}

    def record(self, d, prices):
        eq = self.eq(prices)
        self.equity.append({'date':d,'equity':eq,'cash':self.cash,'pos':self.pos_n()})

# ==================================================================
def load_daily():
    print("[1/3] Loading daily bars...")
    files = [f for f in DAILY_DIR.glob("*.parquet")
             if not f.stem.startswith('index_') and len(f.stem)==6 and f.stem.isdigit()]
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(str(f))
        except Exception:
            continue
        # Normalize columns (avoid duplicate keys)
        cmap = {}
        for c in df.columns:
            cl = c.lower()
            if cl in ('vol','volume') and 'volume' not in df.columns:
                cmap[c] = 'volume'
            elif cl in ('trade_date','datetime') and c != 'date' and 'date' not in df.columns:
                cmap[c] = 'date'
        if cmap:
            df.rename(columns=cmap, inplace=True)
        # Ensure no duplicate columns
        df = df.loc[:, ~df.columns.duplicated()].copy()
        keep = [c for c in ['date','open','high','low','close','volume'] if c in df.columns]
        if 'date' not in keep or 'close' not in keep:
            continue
        df = df[keep].copy()
        df['code'] = f.stem
        df['date'] = pd.to_datetime(df['date']).dt.date
        dfs.append(df)
    bars = pd.concat(dfs, ignore_index=True)
    for c in ['open','high','low','close','volume']:
        if c in bars.columns: bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=['close'])
    bars = bars[(bars['date']>=date(2023,6,1))&(bars['date']<=END)]
    bars = bars.sort_values(['code','date']).reset_index(drop=True)
    print(f"  {bars.code.nunique():,} stocks, {len(bars):,} rows")
    return bars

def gen_signals(bars):
    from app.screener.strategies.ma5_angle import generate_signals
    sig = generate_signals(bars, **SIGNAL_PARAMS)
    sig = sig[(sig['date']>=START)&(sig['date']<=END)].copy()
    sig['date'] = pd.to_datetime(sig['date']).dt.date
    return sig.sort_values(['date','code'])

def run_bt(name, bars, sig, intraday=False):
    print(f"\n{'='*60}")
    print(f"  {name}  |  {START} ~ {END}  |  信号 {len(sig):,}")
    print(f"{'='*60}")
    bt = bars[(bars['date']>=START)&(bars['date']<=END)]
    closes, highs, opens, lows = {}, {}, {}, {}
    for d,g in bt.groupby('date'):
        closes[d]=dict(zip(g['code'],g['close']))
        highs[d]=dict(zip(g['code'],g['high']))
        opens[d]=dict(zip(g['code'],g['open']))
        lows[d]=dict(zip(g['code'],g['low']))
    td = sorted(closes.keys())
    print(f"  交易日: {len(td)}")
    sbd = defaultdict(list)
    for _,r in sig.iterrows():
        sbd[r['date']].append((r['code'],float(r['close'])))
    eng = Engine(td)
    for i,d in enumerate(td):
        snap = {}
        for code in eng.positions:
            if d in closes and code in closes[d]:
                snap[code] = {'open':opens.get(d,{}).get(code,closes[d].get(code,0)),
                              'high':highs.get(d,{}).get(code,closes[d].get(code,0)),
                              'low':lows.get(d,{}).get(code,closes[d].get(code,0)),
                              'close':closes[d].get(code,0)}
        eng.sell_phase(d, snap)
        paused = eng.pause is not None and d <= eng.pause
        if d in sbd and not paused:
            for code,px in sbd[d]:
                if eng.cash < min(eng.max_pos(), MIN_BUY_AMT):
                    break
                if any(t.code==code and (d-t.entry_date).days<=SAME_STOCK_COOLDOWN
                       for t in eng.trades): continue
                eng.buy(d, code, px)
        eng.record(d, snap)
        if (i+1)%80==0:
            print(f"  {d} | {i+1}/{len(td)} | 净值 {eng.eq(snap):,.0f} | "
                  f"持仓 {eng.pos_n()} | 连亏 {eng.cl}")
    return eng

def analyze(eng, name):
    trades = eng.trades
    eq = pd.DataFrame(eng.equity)
    if not trades or eq.empty:
        print("  无交易"); return
    fe = eq['equity'].iloc[-1]
    tr = (fe/INITIAL_CAPITAL-1)*100
    eq['cmax']=eq['equity'].cummax()
    eq['dd']=(eq['equity']-eq['cmax'])/eq['cmax']*100
    md=eq['dd'].min()
    n=len(trades); wins=[t for t in trades if t.ret>0]
    loses=[t for t in trades if t.ret<=0]
    nw,nl=len(wins),len(loses)
    wr=nw/n*100 if n>0 else 0
    aw=np.mean([t.ret for t in wins]) if wins else 0
    al=np.mean([t.ret for t in loses]) if loses else 0
    tp_=sum(t.profit for t in trades)
    print(f"\n  [总览] {name}")
    print(f"  净值 {fe:,.0f} | 总收益 {tr:+.2f}% | 最大回撤 {md:.2f}%")
    print(f"  成交 {n} | 盈{nw}/亏{nl} | 胜率{wr:.1f}% | 均盈{aw:+.2f}% 均亏{al:+.2f}% | 盈亏额{tp_:+,.0f}")

    # 月度
    eq['month']=eq['date'].apply(lambda d: d.strftime('%Y-%m'))
    monthly=eq.groupby('month').agg(s=('equity','first'),e=('equity','last'),dd=('dd','min'))
    monthly['ret']=(monthly['e']/monthly['s']-1)*100
    tm=defaultdict(lambda:{'t':0,'w':0,'p':0.0})
    for t in trades:
        m=t.exit_date.strftime('%Y-%m'); tm[m]['t']+=1
        if t.ret>0: tm[m]['w']+=1
        tm[m]['p']+=t.profit
    print(f"\n  [月度盈亏]")
    print(f"  {'月':<8} {'起始':>11} {'期末':>11} {'收益%':>8} {'回撤%':>7} {'笔':>4} {'盈':>4} {'盈亏额':>11}")
    for m,r in monthly.iterrows():
        t=tm.get(m,{})
        print(f"  {m:<8} {r['s']:>11,.0f} {r['e']:>11,.0f} {r['ret']:>+7.2f} {r['dd']:>6.2f} "
              f"{t.get('t',0):>4} {t.get('w',0):>4} {t.get('p',0):>+11,.0f}")

    # 连亏减仓
    print(f"\n  [连亏减仓: 连亏{LOSS_STREAK_HALVE}笔→半仓]")
    if eng.halves:
        for d,cl,code,rp in eng.halves:
            print(f"  {d} 连亏{cl}笔 触发股{code}({rp:+.2f}%)")
    else: print(f"  无")
    # 暂停
    print(f"\n  [暂停交易: 连亏{LOSS_STREAK_PAUSE}笔→停{PAUSE_DAYS}天]")
    if eng.pauses:
        for d,pu,cl in eng.pauses:
            print(f"  {d} → {pu}  连亏{cl}笔")
    else: print(f"  无")

    # 退出原因
    rc=Counter(t.reason.split('(')[0] for t in trades)
    print(f"\n  [退出原因]")
    for r,c in rc.most_common():
        ar=np.mean([t.ret for t in trades if t.reason.startswith(r)])
        print(f"  {r:<8} {c:>5}笔 {c/n*100:>5.1f}% 均{ar:>+7.2f}%")

    # 年度
    eq['year']=eq['date'].apply(lambda d: d.year)
    print(f"\n  [年度]")
    for yr,g in eq.groupby('year'):
        ret=(g['equity'].iloc[-1]/g['equity'].iloc[0]-1)*100
        yt=[t for t in trades if t.exit_date.year==yr]
        print(f"  {yr}: 收益{ret:>+8.2f}% 回撤{g['dd'].min():>+7.2f}% 交易{len(yt)}笔")

# ==================================================================
if __name__=="__main__":
    t0=time.time()
    bars=load_daily()
    print("[2/3] Generating signals...")
    sig=gen_signals(bars)
    print(f"  信号: {len(sig):,} ({sig.code.nunique():,} stocks, {sig.date.nunique()} days)")
    print("[3/3] Running backtest...")
    eng=run_bt("[日线尾盘价]", bars, sig)
    analyze(eng, "日线尾盘价")
    print(f"\n  耗时 {time.time()-t0:.0f}s")
