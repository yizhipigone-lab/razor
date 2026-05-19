#!/usr/bin/env python
"""D3组合：质量排�?on vs off 对比 (2023-01-01 至今)"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta
from collections import defaultdict, Counter
from pathlib import Path
import time, warnings
warnings.filterwarnings('ignore')

from app.screener.strategies.ma5_angle import generate_signals

START = date(2023, 1, 1); END = date.today()
ROOT = Path(__file__).parent.parent; DAILY_DIR = ROOT / "data" / "parquet" / "daily"

# D3组合参数
INITIAL_CAPITAL=1_000_000; POSITION_SIZE=50_000; MIN_BUY_AMT=5_000
HARD_STOP=-0.055; TP1_PCT=0.04; TP1_SELL_RATIO=0.20; TP2_PCT=0.14
TRAIL_ACTIVATE=0.03; TRAIL_DD=0.01
TIME_EXIT_DAYS=3; TIME_EXIT_PROFIT=0.03; TIME_FORCE_DAYS=9
LOSS_STREAK_HALVE=3; LOSS_STREAK_PAUSE=5; PAUSE_DAYS=3
SAME_STOCK_COOLDOWN=20; STRATEGY_NAME="ma5_angle"

BASE_SIGNAL = {
    "version":"improved","filter_st":True,"filter_bj":True,
    "filter_consecutive_up":False,"filter_gap_quality":False,
}

class Position:
    __slots__=('code','entry_date','entry_price','shares','cost','peak_price','remaining','tp1','tp2','active','strategy')
    def __init__(self,c,d,px,sh,cost,s=""): self.code=c;self.entry_date=d;self.entry_price=px;self.shares=sh;self.cost=cost;self.peak_price=px;self.remaining=sh;self.tp1=False;self.tp2=False;self.active=True;self.strategy=s

class Trade:
    __slots__=('code','entry_date','exit_date','entry_px','exit_px','shares','ret','profit','reason','hold','strategy','timing')
    def __init__(self,c,ed,xd,ep,xp,sh,ret,profit,reason,hold,s="",t="close"): self.code=c;self.entry_date=ed;self.exit_date=xd;self.entry_px=ep;self.exit_px=xp;self.shares=sh;self.ret=ret;self.profit=profit;self.reason=reason;self.hold=hold;self.strategy=s;self.timing=t

class Engine:
    def __init__(self,td_list): self.cash=INITIAL_CAPITAL;self.positions={};self.trades=[];self.equity=[];self.cl=0;self.pause=None;self.td_list=td_list
    def max_pos(self): return POSITION_SIZE/2 if self.cl>=LOSS_STREAK_HALVE else POSITION_SIZE
    def pos_n(self): return sum(1 for p in self.positions.values() if p.active)
    def _td(self,d1,d2): return sum(1 for td in self.td_list if d1<=td<=d2)
    def eq(self,prices):
        pv=0
        for p in self.positions.values():
            if not p.active: continue
            bar=prices.get(p.code,{}); px=bar.get('close',p.entry_price) if isinstance(bar,dict) else (bar if bar else p.entry_price)
            pv+=p.remaining*px
        return self.cash+pv
    def buy(self,d,code,px):
        if code in self.positions: return None
        ma=min(self.max_pos(),self.cash)
        if ma<MIN_BUY_AMT: return None
        sh=int(ma/px/100)*100
        if sh<100: return None
        cost=sh*px
        if cost>self.cash: return None
        p=Position(code,d,px,sh,cost,STRATEGY_NAME)
        self.cash-=cost;self.positions[code]=p
        return p
    def check_stops(self,d,snap):
        sells=[]
        for code,p in list(self.positions.items()):
            if not p.active or p.remaining<=0: continue
            bar=snap.get(code)
            if bar is None: continue
            cp=bar['close'];hp=bar.get('high',cp)
            if hp>p.peak_price: p.peak_price=hp
            pp=p.peak_price/p.entry_price-1;cur=cp/p.entry_price-1;hd=self._td(p.entry_date,d)
            if cur<=HARD_STOP: sells.append((p,cp,"HS",None));continue
            if hd>TIME_FORCE_DAYS: sells.append((p,cp,"TF",None));continue
            if not p.tp2 and cur>=TP2_PCT: sells.append((p,cp,"TP2",None));continue
            if not p.tp1 and cur>=TP1_PCT:
                ss=int(p.remaining*TP1_SELL_RATIO/100)*100
                if ss>=100: sells.append((p,cp,"TP1",ss));continue
            if pp>=TRAIL_ACTIVATE:
                dd=cp/p.peak_price-1
                if dd<=-TRAIL_DD: sells.append((p,p.peak_price*(1-TRAIL_DD),"TR",None));continue
            if hd>TIME_EXIT_DAYS and cur>TIME_EXIT_PROFIT: sells.append((p,cp,"TC",None));continue
        return sells
    def sell(self,p,px,reason,partial=None,xd=None):
        ss=partial if partial else p.remaining;ss=int(ss//100*100)
        if ss<=0: return None
        ret=(px/p.entry_price-1)*100;profit=ss*(px-p.entry_price)
        p.remaining-=ss
        if "TP2" in reason: p.tp2=True
        if "TP1" in reason: p.tp1=True
        if p.remaining<=0: p.active=False;p.remaining=0
        self.cash+=ss*px
        return Trade(p.code,p.entry_date,xd or date.today(),p.entry_price,px,ss,ret,profit,reason,0,p.strategy,"close")
    def sell_phase(self,d,snap):
        for p,px,reason,partial in self.check_stops(d,snap):
            t=self.sell(p,px,reason,partial,d)
            if t: t.hold=self._td(p.entry_date,d);self.trades.append(t)
            if t:
                if t.ret<=0: self.cl+=1
                else: self.cl=0;self.pause=None
                if self.cl>=LOSS_STREAK_PAUSE: self.pause=d+timedelta(days=PAUSE_DAYS)
        self.positions={k:v for k,v in self.positions.items() if v.active}
    def record(self,d,prices): self.equity.append({'date':d,'equity':self.eq(prices),'cash':self.cash,'pos':self.pos_n()})

def load_daily():
    files=[f for f in DAILY_DIR.glob("*.parquet") if not f.stem.startswith('index_') and len(f.stem)==6 and f.stem.isdigit()]
    dfs=[]
    for f in files:
        try: df=pd.read_parquet(str(f))
        except: continue
        cmap={}
        for c in df.columns:
            cl=c.lower()
            if cl in ('vol','volume') and 'volume' not in df.columns: cmap[c]='volume'
            elif cl in ('trade_date','datetime') and c!='date' and 'date' not in df.columns: cmap[c]='date'
        if cmap: df.rename(columns=cmap,inplace=True)
        df=df.loc[:,~df.columns.duplicated()].copy()
        keep=[c for c in ['date','open','high','low','close','volume'] if c in df.columns]
        if 'date' not in keep or 'close' not in keep: continue
        df=df[keep].copy();df['code']=f.stem;df['date']=pd.to_datetime(df['date']).dt.date
        dfs.append(df)
    bars=pd.concat(dfs,ignore_index=True)
    for c in ['open','high','low','close','volume']:
        if c in bars.columns: bars[c]=pd.to_numeric(bars[c],errors='coerce')
    bars=bars.dropna(subset=['close']);bars=bars[(bars['date']>=date(2022,6,1))&(bars['date']<=END)]
    return bars.sort_values(['code','date']).reset_index(drop=True)

def run_one(quality_sort, bars, sig, td, closes, highs, sbd):
    eng=Engine(td)
    for d in td:
        snap={}
        for code in eng.positions:
            if d in closes and code in closes[d]:
                snap[code]={'open':closes[d].get(code,0),'high':highs[d].get(code,closes[d].get(code,0)),'low':closes[d].get(code,0),'close':closes[d].get(code,0)}
        eng.sell_phase(d,snap)
        paused=eng.pause is not None and d<=eng.pause
        if d in sbd and not paused:
            for code,px in sbd[d]:
                if eng.cash<min(eng.max_pos(),MIN_BUY_AMT): break
                if any(t.code==code and (d-t.entry_date).days<=SAME_STOCK_COOLDOWN for t in eng.trades): continue
                eng.buy(d,code,px)
        eng.record(d,snap)

    trades=eng.trades
    if not trades: return None
    eq=pd.DataFrame(eng.equity);fe=eq['equity'].iloc[-1];tr=(fe/INITIAL_CAPITAL-1)*100
    eq['cmax']=eq['equity'].cummax();eq['dd']=(eq['equity']-eq['cmax'])/eq['cmax']*100;md=eq['dd'].min()
    n=len(trades);wins=[t for t in trades if t.ret>0];loses=[t for t in trades if t.ret<=0]
    nw,nl=len(wins),len(loses);wr=nw/n*100 if n>0 else 0
    aw=np.mean([t.ret for t in wins]) if wins else 0;al=np.mean([t.ret for t in loses]) if loses else 0
    ds=(td[-1]-td[0]).days;ar=(1+tr/100)**(365/max(ds,1))-1;calmar=ar/abs(md/100) if md!=0 else 0
    rc=Counter(t.reason for t in trades)
    eq_m=eq.copy();eq_m['month']=eq_m['date'].apply(lambda d:d.strftime('%Y-%m'))
    monthly=eq_m.groupby('month').agg(s=('equity','first'),e=('equity','last'))
    monthly['ret']=(monthly['e']/monthly['s']-1)*100
    pos_months=sum(1 for _,r in monthly.iterrows() if r['ret']>0)
    return {'tr':round(tr,2),'dd':round(md,2),'n':n,'nw':nw,'nl':nl,'wr':round(wr,1),'aw':round(aw,2),'al':round(al,2),'calmar':round(calmar,2),'fe':round(fe,0),'reasons':dict(rc.most_common()),'pos_months':pos_months,'total_months':len(monthly)}

if __name__=="__main__":
    t0=time.time()
    print("="*80)
    print("  D3组合：质量排�?on vs off 对比")
    print("  D3/>3%/F9 | TA=0.03 DD=0.01 | 2023-01-01 ~ today")
    print("="*80)

    print("\n[1/3] 加载数据...")
    bars=load_daily()
    print(f"  {bars.code.nunique():,} stocks, {len(bars):,} rows")

    for label, sort_flag in [("质量排序 ON",False),("质量排序 OFF",True)]:
        print(f"\n[2/3] 生成信号 ({label})...")
        sp=dict(BASE_SIGNAL);sp['disable_quality_sort']=sort_flag
        sig=generate_signals(bars,**sp)
        sig=sig[(sig['date']>=START)&(sig['date']<=END)].copy()
        sig['date']=pd.to_datetime(sig['date']).dt.date

        bt=bars[(bars['date']>=START)&(bars['date']<=END)]
        closes,highs={},{}
        for d,g in bt.groupby('date'): closes[d]=dict(zip(g['code'],g['close']));highs[d]=dict(zip(g['code'],g['high']))
        td=sorted(closes.keys())
        sbd=defaultdict(list)
        for _,r in sig.iterrows(): sbd[r['date']].append((r['code'],float(r['close'])))

        print(f"  交易�?{len(td)} 信号:{len(sig):,}")

        print(f"[3/3] 回测 ({label})...")
        t1=time.time()
        r=run_one(sort_flag,bars,sig,td,closes,highs,sbd)
        elapsed=time.time()-t1
        r['label']=label;r['signals']=len(sig);r['time']=round(elapsed,0)
        print(f"  耗时{elapsed:.0f}s")

        print(f"\n  {'='*60}")
        print(f"  {label}")
        print(f"  {'='*60}")
        print(f"  信号: {r['signals']:,}  |  交易: {r['n']}�?)
        print(f"  总收�? {r['tr']:+.2f}%  |  最大回�? {r['dd']:+.2f}%")
        print(f"  期末净�? {r['fe']:,.0f}  |  Calmar: {r['calmar']:.2f}")
        print(f"  胜率: {r['wr']:.1f}%  |  盈{r['nw']}/亏{r['nl']}")
        print(f"  均盈: {r['aw']:+.2f}%  |  均亏: {r['al']:+.2f}%")
        print(f"  盈利�? {r['pos_months']}/{r['total_months']}")
        print(f"  退�? {r['reasons']}")

        if sort_flag:
            off_r=r
        else:
            on_r=r

    # 对比
    print(f"\n{'='*80}")
    print(f"  质量排序影响对比")
    print(f"{'='*80}")
    print(f"  {'指标':<20} {'ON':>15} {'OFF':>15} {'差异':>15}")
    print(f"  {'-'*65}")
    for key,label,fmt in [('signals','信号�?,',.0f'),('n','交易笔数',',.0f'),('tr','总收�?','+.2f'),('dd','最大回�?','+.2f'),('wr','胜率%','.1f'),('calmar','Calmar','.2f'),('aw','均盈%','+.2f'),('al','均亏%','+.2f')]:
        ov=on_r[key];ofv=off_r[key];diff=ov-ofv
        print(f"  {label:<20} {ov:{fmt}} {ofv:{fmt}} {diff:{fmt}}")

    print(f"\n  总耗时: {time.time()-t0:.0f}s")
