#!/usr/bin/env python
"""上证红盘过滤 on/off 对比 (2023-01-01 至今, D3组合)"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta
from collections import defaultdict, Counter
from pathlib import Path
import time, warnings
warnings.filterwarnings('ignore')

from app.screener.strategies.ma5_angle import generate_signals

START=date(2023,1,1); END=date.today()
ROOT=Path(__file__).parent.parent; DAILY_DIR=ROOT/"data"/"parquet"/"daily"

# D3 params
IC=1_000_000; PS=50_000; MBA=5_000
HS=-0.055; TP1=0.04; TP1R=0.20; TP2=0.14
TA=0.03; TD=0.01; TED=3; TEP=0.03; TFD=9
LSH=3; LSP=5; PD=3; SSC=20

BASE_SIG={"version":"improved","filter_st":True,"filter_bj":True,
    "vol_threshold":1.5,"close_position_threshold":0.8,
    "disable_quality_sort":False,"filter_consecutive_up":False,"filter_gap_quality":False}

class P:
    __slots__=('c','ed','ep','sh','cost','pp','rm','t1','t2','ac','s')
    def __init__(s,c,d,px,sh,cost,st=""): s.c=c;s.ed=d;s.ep=px;s.sh=sh;s.cost=cost;s.pp=px;s.rm=sh;s.t1=False;s.t2=False;s.ac=True;s.s=st
class T:
    __slots__=('c','ed','xd','ep','xp','sh','r','pr','rs','hd','s','tm')
    def __init__(s,c,ed,xd,ep,xp,sh,r,pr,rs,hd,st="",tm="c"): s.c=c;s.ed=ed;s.xd=xd;s.ep=ep;s.xp=xp;s.sh=sh;s.r=r;s.pr=pr;s.rs=rs;s.hd=hd;s.s=st;s.tm=tm

class E:
    def __init__(s,td): s.ca=IC;s.ps={};s.tr=[];s.eq=[];s.cl=0;s.pa=None;s.td=td
    def mp(s): return PS/2 if s.cl>=LSH else PS
    def pn(s): return sum(1 for p in s.ps.values() if p.ac)
    def _td(s,d1,d2): return sum(1 for t in s.td if d1<=t<=d2)
    def ev(s,pxs):
        v=0
        for p in s.ps.values():
            if not p.ac: continue
            b=pxs.get(p.c,{}); x=b.get('close',p.ep) if isinstance(b,dict) else (b if b else p.ep)
            v+=p.rm*x
        return s.ca+v
    def buy(s,d,c,px):
        if c in s.ps: return
        ma=min(s.mp(),s.ca)
        if ma<MBA: return
        sh=int(ma/px/100)*100
        if sh<100: return
        cost=sh*px
        if cost>s.ca: return
        p=P(c,d,px,sh,cost,"ma5_angle");s.ca-=cost;s.ps[c]=p
    def cs(s,d,snap):
        sl=[]
        for c,p in list(s.ps.items()):
            if not p.ac or p.rm<=0: continue
            b=snap.get(c)
            if b is None: continue
            cp=b['close'];hp=b.get('high',cp)
            if hp>p.pp: p.pp=hp
            pp=p.pp/p.ep-1;cur=cp/p.ep-1;hd=s._td(p.ed,d)
            if cur<=HS: sl.append((p,cp,"HS",None));continue
            if hd>TFD: sl.append((p,cp,"TF",None));continue
            if not p.t2 and cur>=TP2: sl.append((p,cp,"TP2",None));continue
            if not p.t1 and cur>=TP1:
                ss=int(p.rm*TP1R/100)*100
                if ss>=100: sl.append((p,cp,"TP1",ss));continue
            if pp>=TA:
                dd=cp/p.pp-1
                if dd<=-TD: sl.append((p,p.pp*(1-TD),"TR",None));continue
            if hd>TED and cur>TEP: sl.append((p,cp,"TC",None));continue
        return sl
    def sell(s,p,px,rs,pt=None,xd=None):
        ss=pt if pt else p.rm;ss=int(ss//100*100)
        if ss<=0: return
        r=(px/p.ep-1)*100;pr=ss*(px-p.ep)
        p.rm-=ss
        if "TP2" in rs: p.t2=True
        if "TP1" in rs: p.t1=True
        if p.rm<=0: p.ac=False;p.rm=0
        s.ca+=ss*px
        return T(p.c,p.ed,xd or date.today(),p.ep,px,ss,r,pr,rs,0,p.s,"c")
    def sp(s,d,snap):
        for p,px,rs,pt in s.cs(d,snap):
            t=s.sell(p,px,rs,pt,d)
            if t: t.hd=s._td(p.ed,d);s.tr.append(t)
            if t:
                if t.r<=0: s.cl+=1
                else: s.cl=0;s.pa=None
                if s.cl>=LSP: s.pa=d+timedelta(days=PD)
        s.ps={k:v for k,v in s.ps.items() if v.ac}
    def rec(s,d,pxs): s.eq.append({'date':d,'equity':s.ev(pxs),'cash':s.ca,'pos':s.pn()})

def load():
    fs=[f for f in DAILY_DIR.glob("*.parquet") if not f.stem.startswith('index_') and len(f.stem)==6 and f.stem.isdigit()]
    dfs=[]
    for f in fs:
        try: df=pd.read_parquet(str(f))
        except: continue
        cm={}
        for c in df.columns:
            cl=c.lower()
            if cl in ('vol','volume') and 'volume' not in df.columns: cm[c]='volume'
            elif cl in ('trade_date','datetime') and c!='date' and 'date' not in df.columns: cm[c]='date'
        if cm: df.rename(columns=cm,inplace=True)
        df=df.loc[:,~df.columns.duplicated()].copy()
        kp=[c for c in ['date','open','high','low','close','volume'] if c in df.columns]
        if 'date' not in kp or 'close' not in kp: continue
        df=df[kp].copy();df['code']=f.stem;df['date']=pd.to_datetime(df['date']).dt.date
        dfs.append(df)
    bars=pd.concat(dfs,ignore_index=True)
    for c in ['open','high','low','close','volume']:
        if c in bars.columns: bars[c]=pd.to_numeric(bars[c],errors='coerce')
    bars=bars.dropna(subset=['close']);bars=bars[(bars['date']>=date(2022,6,1))&(bars['date']<=END)]
    return bars.sort_values(['code','date']).reset_index(drop=True)

def run(sig,bars,td,closes,highs,sbd):
    eng=E(td)
    for d in td:
        snap={}
        for c in eng.ps:
            if d in closes and c in closes[d]:
                snap[c]={'open':closes[d].get(c,0),'high':highs[d].get(c,closes[d].get(c,0)),'low':closes[d].get(c,0),'close':closes[d].get(c,0)}
        eng.sp(d,snap)
        paused=eng.pa is not None and d<=eng.pa
        if d in sbd and not paused:
            for c,px in sbd[d]:
                if eng.ca<min(eng.mp(),MBA): break
                if any(t.c==c and (d-t.ed).days<=SSC for t in eng.tr): continue
                eng.buy(d,c,px)
        eng.rec(d,snap)
    tr=eng.tr
    if not tr: return None
    eq=pd.DataFrame(eng.eq);fe=eq['equity'].iloc[-1];ret=(fe/IC-1)*100
    eq['cmax']=eq['equity'].cummax();eq['dd']=(eq['equity']-eq['cmax'])/eq['cmax']*100;md=eq['dd'].min()
    n=len(tr);ws=[t for t in tr if t.r>0];ls=[t for t in tr if t.r<=0]
    nw,nl=len(ws),len(ls);wr=nw/n*100 if n>0 else 0
    aw=np.mean([t.r for t in ws]) if ws else 0;al=np.mean([t.r for t in ls]) if ls else 0
    ds=(td[-1]-td[0]).days;ar=(1+ret/100)**(365/max(ds,1))-1;cal=ar/abs(md/100) if md!=0 else 0
    rc=Counter(t.rs for t in tr)
    eq['month']=eq['date'].apply(lambda d:d.strftime('%Y-%m'))
    mo=eq.groupby('month').agg(s=('equity','first'),e=('equity','last'))
    mo['ret']=(mo['e']/mo['s']-1)*100
    pm=sum(1 for _,r in mo.iterrows() if r['ret']>0)
    return {'ret':round(ret,2),'dd':round(md,2),'n':n,'nw':nw,'nl':nl,'wr':round(wr,1),'aw':round(aw,2),'al':round(al,2),'calmar':round(cal,2),'fe':round(fe,0),'rc':dict(rc.most_common()),'pm':pm,'tm':len(mo)}

if __name__=="__main__":
    t0=time.time()
    print("="*70)
    print("  上证红盘过滤 on/off 对比 (2023-01-01 ~ today)")
    print("  D3/>3%/F9 | TA=0.03 DD=0.01")
    print("="*70)

    print("\n[1] 加载数据...")
    bars=load()
    print(f"  {bars.code.nunique():,} stocks, {len(bars):,} rows")

    results={}
    for label,sh_filter in [("红盘过滤 ON",True),("红盘过滤 OFF",False)]:
        print(f"\n[2] 生成信号 ({label})...")
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

        t1=time.time()
        r=run(sig,bars,td,closes,highs,sbd)
        elapsed=time.time()-t1
        r['label']=label;r['signals']=len(sig);r['time']=round(elapsed,0)
        results[label]=r

        print(f"  收益:{r['ret']:+.2f}% DD:{r['dd']:+.2f}% 胜率:{r['wr']:.1f}% Calmar:{r['calmar']:.2f} 交易:{r['n']}�?)
        print(f"  退�?{r['rc']}")

    # 对比
    on=results["红盘过滤 ON"];off=results["红盘过滤 OFF"]
    print(f"\n{'='*70}")
    print(f"  对比总结")
    print(f"{'='*70}")
    print(f"  {'指标':<18} {'ON':>12} {'OFF':>12} {'差异':>12}")
    print(f"  {'-'*54}")
    for k,lb,fmt in [('signals','信号�?,',.0f'),('n','交易笔数',',.0f'),('ret','总收�?','+.2f'),('dd','最大回�?','+.2f'),('wr','胜率%','.1f'),('calmar','Calmar','.2f'),('aw','均盈%','+.2f'),('al','均亏%','+.2f'),('pm','盈利�?,'.0f')]:
        print(f"  {lb:<18} {on[k]:{fmt}} {off[k]:{fmt}} {off[k]-on[k]:{fmt}}")
    print(f"\n  总耗时: {time.time()-t0:.0f}s")
