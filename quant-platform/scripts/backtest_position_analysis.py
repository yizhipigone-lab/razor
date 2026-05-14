#!/usr/bin/env python
"""月度平均持仓金额 + 中证A500月涨跌对比"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta
from collections import defaultdict
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')

from app.sim_trader.config import *
# 从 TAKE_PROFIT_TIERS 提取传统变量名
TP1_PCT = TAKE_PROFIT_TIERS[0]["profit_pct"]
TP1_SELL_RATIO = TAKE_PROFIT_TIERS[0]["sell_ratio"]
TP2_PCT = TAKE_PROFIT_TIERS[1]["profit_pct"]
from app.screener.strategies.ma5_angle import generate_signals

START = date(2024, 1, 1); END = date.today()
ROOT = Path(__file__).parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"

class Position:
    __slots__ = ('code','entry_date','entry_price','shares','cost','peak_price','remaining','tp1','tp2','active','strategy')
    def __init__(self, c, d, px, sh, cost, s=""): self.code=c; self.entry_date=d; self.entry_price=px; self.shares=sh; self.cost=cost; self.peak_price=px; self.remaining=sh; self.tp1=False; self.tp2=False; self.active=True; self.strategy=s

class Trade:
    __slots__ = ('code','entry_date','exit_date','entry_px','exit_px','shares','ret','profit','reason','hold','strategy','timing')
    def __init__(self, c, ed, xd, ep, xp, sh, ret, profit, reason, hold, s="", t="close"): self.code=c; self.entry_date=ed; self.exit_date=xd; self.entry_px=ep; self.exit_px=xp; self.shares=sh; self.ret=ret; self.profit=profit; self.reason=reason; self.hold=hold; self.strategy=s; self.timing=t

class Engine:
    def __init__(self, td_list):
        self.cash=INITIAL_CAPITAL; self.positions={}; self.trades=[]; self.equity=[]; self.cl=0; self.pause=None; self.td_list=td_list
    def max_pos(self): return POSITION_SIZE/2 if self.cl>=LOSS_STREAK_HALVE else POSITION_SIZE
    def pos_n(self): return sum(1 for p in self.positions.values() if p.active)
    def pos_cost(self): return sum(p.cost*p.remaining/p.shares for p in self.positions.values() if p.active)  # 剩余成本
    def pos_value(self, prices):
        v=0
        for p in self.positions.values():
            if not p.active: continue
            bar=prices.get(p.code,{})
            px=bar.get('close',p.entry_price) if isinstance(bar,dict) else (bar if bar else p.entry_price)
            v+=p.remaining*px
        return v
    def eq(self, prices): return self.cash+self.pos_value(prices)
    def _td(self, d1, d2): return sum(1 for td in self.td_list if d1<=td<=d2)
    def buy(self, d, code, px):
        if code in self.positions: return None
        ma=min(self.max_pos(),self.cash)
        if ma<MIN_BUY_AMT: return None
        sh=int(ma/px/100)*100
        if sh<100: return None
        cost=sh*px
        if cost>self.cash: return None
        p=Position(code,d,px,sh,cost,STRATEGY_NAME)
        self.cash-=cost; self.positions[code]=p
        return p
    def stops(self, d, snap):
        sells=[]
        for code,p in list(self.positions.items()):
            if not p.active or p.remaining<=0: continue
            bar=snap.get(code)
            if bar is None: continue
            cp=bar['close']; hp=bar.get('high',cp)
            if hp>p.peak_price: p.peak_price=hp
            pp=p.peak_price/p.entry_price-1; cur=cp/p.entry_price-1; hd=self._td(p.entry_date,d)
            if cur<=HARD_STOP: sells.append((p,cp,f"HS",None)); continue
            if hd>TIME_FORCE_DAYS: sells.append((p,cp,f"TF",None)); continue
            if not p.tp2 and cur>=TP2_PCT: sells.append((p,cp,f"TP2",None)); continue
            if not p.tp1 and cur>=TP1_PCT:
                ss=int(p.remaining*TP1_SELL_RATIO/100)*100
                if ss>=100: sells.append((p,cp,f"TP1",ss)); continue
            if pp>=TRAIL_ACTIVATE:
                dd=cp/p.peak_price-1
                if dd<=-TRAIL_DD:
                    tp=p.peak_price*(1-TRAIL_DD)
                    sells.append((p,tp,f"TR",None)); continue
            if hd>TIME_EXIT_DAYS and cur>TIME_EXIT_PROFIT: sells.append((p,cp,f"TC",None)); continue
        return sells
    def sell(self, p, px, reason, partial=None, xd=None):
        ss=partial if partial else p.remaining; ss=int(ss//100*100)
        if ss<=0: return None
        ret=(px/p.entry_price-1)*100; profit=ss*(px-p.entry_price)
        p.remaining-=ss
        if reason=="TP2": p.tp2=True
        if reason=="TP1": p.tp1=True
        if p.remaining<=0: p.active=False; p.remaining=0
        self.cash+=ss*px
        return Trade(p.code,p.entry_date,xd or date.today(),p.entry_price,px,ss,ret,profit,reason,0,p.strategy,"close")
    def sell_phase(self, d, snap):
        for p,px,reason,partial in self.stops(d,snap):
            t=self.sell(p,px,reason,partial,d)
            if t:
                t.hold=self._td(p.entry_date,d); self.trades.append(t)
                if t.ret<=0:
                    self.cl+=1
                    if self.cl>=LOSS_STREAK_PAUSE: self.pause=d+timedelta(days=PAUSE_DAYS)
                else: self.cl=0; self.pause=None
        self.positions={k:v for k,v in self.positions.items() if v.active}
    def record(self, d, prices):
        eq=self.eq(prices); self.equity.append({'date':d,'equity':eq,'cash':self.cash,'pos':self.pos_n(),'pos_val':self.pos_value(prices)})

def load_daily():
    files = [f for f in DAILY_DIR.glob("*.parquet") if not f.stem.startswith('index_') and len(f.stem)==6 and f.stem.isdigit()]
    dfs = []
    for f in files:
        try: df = pd.read_parquet(str(f))
        except: continue
        cmap = {}
        for c in df.columns:
            cl=c.lower()
            if cl in ('vol','volume') and 'volume' not in df.columns: cmap[c]='volume'
            elif cl in ('trade_date','datetime') and c!='date' and 'date' not in df.columns: cmap[c]='date'
        if cmap: df.rename(columns=cmap, inplace=True)
        df = df.loc[:, ~df.columns.duplicated()].copy()
        keep = [c for c in ['date','open','high','low','close','volume'] if c in df.columns]
        if 'date' not in keep or 'close' not in keep: continue
        df = df[keep].copy(); df['code']=f.stem
        df['date']=pd.to_datetime(df['date']).dt.date
        dfs.append(df)
    bars = pd.concat(dfs, ignore_index=True)
    for c in ['open','high','low','close','volume']:
        if c in bars.columns: bars[c]=pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=['close'])
    bars = bars[(bars['date']>=date(2023,6,1))&(bars['date']<=END)]
    return bars.sort_values(['code','date']).reset_index(drop=True)

def load_index_monthly(name):
    """加载指数月度涨跌"""
    for prefix in ['index_', '']:
        fp = DAILY_DIR / f"{prefix}{name}.parquet"
        if fp.exists():
            df = pd.read_parquet(str(fp))
            # 统一列名
            if 'trade_date' in df.columns: df['date'] = pd.to_datetime(df['trade_date']).dt.date
            elif 'date' in df.columns: df['date'] = pd.to_datetime(df['date']).dt.date
            else: continue
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df = df.dropna(subset=['close']).sort_values('date')
            df['month'] = df['date'].apply(lambda d: d.strftime('%Y-%m'))
            monthly = df.groupby('month').agg(first=('close','first'), last=('close','last'))
            monthly['ret'] = (monthly['last']/monthly['first']-1)*100
            return monthly['ret'].to_dict(), f"index_{name}" if prefix else name
    return {}, ""

a500_ret, a500_src = load_index_monthly("000510")
sh_ret, sh_src = load_index_monthly("000001")

# ── run ──
bars = load_daily()
print(f"Loaded: {bars.code.nunique():,} stocks")

sig = generate_signals(bars, **SIGNAL_PARAMS)
sig = sig[(sig['date']>=START)&(sig['date']<=END)].copy()
sig['date']=pd.to_datetime(sig['date']).dt.date
sig = sig.sort_values(['date','code'])

bt = bars[(bars['date']>=START)&(bars['date']<=END)]
closes, highs = {}, {}
for d,g in bt.groupby('date'):
    closes[d]=dict(zip(g['code'],g['close']))
    highs[d]=dict(zip(g['code'],g['high']))
td = sorted(closes.keys())
sbd = defaultdict(list)
for _,r in sig.iterrows(): sbd[r['date']].append((r['code'],float(r['close'])))

eng = Engine(td)
print(f"Running backtest ({len(td)} days)...")
for i,d in enumerate(td):
    snap = {}
    for code in eng.positions:
        if d in closes and code in closes[d]:
            snap[code] = {'open':closes[d].get(code,0),'high':highs[d].get(code,closes[d].get(code,0)),'low':closes[d].get(code,0),'close':closes[d].get(code,0)}
    eng.sell_phase(d, snap)
    paused = eng.pause is not None and d<=eng.pause
    if d in sbd and not paused:
        for code,px in sbd[d]:
            if eng.cash < min(eng.max_pos(), MIN_BUY_AMT): break
            if any(t.code==code and (d-t.entry_date).days<=SAME_STOCK_COOLDOWN for t in eng.trades): continue
            eng.buy(d, code, px)
    eng.record(d, snap)

# ── 月度分析 ──
eq = pd.DataFrame(eng.equity)
eq['month'] = eq['date'].apply(lambda d: d.strftime('%Y-%m'))

# 月度平均持仓金额
monthly_avg = eq.groupby('month')['pos_val'].mean()
monthly_max = eq.groupby('month')['pos_val'].max()
monthly_first = eq.groupby('month').agg(s=('equity','first'), e=('equity','last'), dd=('equity', lambda x: (x/x.cummax()-1).min()*100))
monthly_first['ret'] = (monthly_first['e']/monthly_first['s']-1)*100

# 指数月度数据
a500_ret, a500_src = load_index_monthly("000510")
sh_ret, sh_src = load_index_monthly("000001")

print(f"\n{'='*100}")
print(f"  月度持仓与指数涨跌对比")
print(f"  A500数据源: {a500_src} ({len(a500_ret)}月)  |  上证数据源: {sh_src} ({len(sh_ret)}月)")
print(f"{'='*100}")
print(f"  {'月':<8} {'月初净值':>10} {'策略%':>7} {'A500%':>7} {'上证%':>7} {'均持仓':>10} {'仓位%':>6} {'回撤%':>7}")
print(f"  {'-'*85}")

for m in sorted(monthly_first.index):
    r = monthly_first.loc[m]
    avg_pv = monthly_avg.get(m, 0)
    a5 = a500_ret.get(m, float('nan'))
    sh = sh_ret.get(m, float('nan'))
    pos_pct = avg_pv / r['s'] * 100 if r['s'] > 0 else 0
    a5_str = f"{a5:>+7.2f}" if not np.isnan(a5) else "    N/A"
    print(f"  {m:<8} {r['s']:>10,.0f} {r['ret']:>+6.2f} {a5_str} {sh:>+7.2f} {avg_pv:>10,.0f} {pos_pct:>5.1f}% {r['dd']:>+7.2f}")

# 汇总
print(f"\n  [汇总]")
print(f"  期末净值: {eq['equity'].iloc[-1]:,.0f}")
print(f"  总收益: {(eq['equity'].iloc[-1]/INITIAL_CAPITAL-1)*100:+.2f}%")
print(f"  整体平均仓位: {eq['pos_val'].mean()/eq['equity'].mean()*100:.1f}%")
print(f"  最高仓位占比: {(eq['pos_val']/eq['equity']).max()*100:.1f}%")
positive_months = sum(1 for m in monthly_first.index if monthly_first.loc[m,'ret'] > 0)
total_months = len(monthly_first)
print(f"  策略盈利月: {positive_months}/{total_months}")
# 与上证相关性
common_sh = [(monthly_first.loc[m,'ret'], sh_ret[m]) for m in monthly_first.index if m in sh_ret]
if common_sh:
    sr = [x[0] for x in common_sh]; ir = [x[1] for x in common_sh]
    print(f"  策略与上证月收益相关系数: {np.corrcoef(sr, ir)[0,1]:.3f}")
