#!/usr/bin/env python
"""MA5 策略 — 2023Q3~2026 回测：盘中监控 vs 尾盘价"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from collections import Counter, defaultdict
import time, gc, warnings
warnings.filterwarnings('ignore')

INITIAL = 1_000_000; POS_CAP = 50_000
HARD_STOP, TP1_PCT, TP1_RATIO, TP2_PCT = -0.055, 0.04, 0.20, 0.14
TRAIL_ACT, TRAIL_DD = 0.08, 0.02
TIME_EXIT, TIME_FORCE = 7, 10
LOSS_S2, PAUSE_D = 5, 3
SAME_COOLDOWN = 20
START, END = date(2023,7,1), date(2026,5,1)
ROOT = Path(__file__).parent.parent
DAILY_DIR = ROOT/"data"/"parquet"/"daily"
MIN5_DIR  = ROOT/"data"/"parquet"/"min5"

SP = {"version":"improved","filter_st":True,"filter_bj":True,"sh_red_filter":False,
      "vol_threshold":1.2,"close_position_threshold":0.6,"disable_quality_sort":True,
      "filter_consecutive_up":False,"filter_gap_quality":False}

@dataclass
class P:
    code:str; edate:date; eprice:float; etime:str; shares:int; cost:float
    peak:float=0.0; rem:int=0; tp1:bool=False; tp2:bool=False; active:bool=True
    def __post_init__(self): self.peak=self.eprice; self.rem=self.shares

@dataclass
class T:
    code:str; edate:date; eprice:float; xdate:date; xprice:float
    shares:int; ret:float; profit:float; reason:str; hold:int; mode:str=""

def load_daily():
    print("[1/5] 加载日线...", end=" ", flush=True); t0=time.time()
    files=[f for f in DAILY_DIR.glob("*.parquet") if not f.stem.startswith('index_') and len(f.stem)==6 and f.stem.isdigit()]
    dfs=[]
    for f in files:
        try:
            df=pd.read_parquet(str(f), columns=['date','open','high','low','close','volume'])
            df['code']=f.stem; dfs.append(df)
        except: pass
    bars=pd.concat(dfs,ignore_index=True)
    for c in ['open','high','low','close','volume']: bars[c]=pd.to_numeric(bars[c],errors='coerce')
    bars=bars.dropna(subset=['close']); bars['date']=pd.to_datetime(bars['date']).dt.date
    bars=bars.sort_values(['code','date'])
    print(f"{len(bars):,}行 {bars['code'].nunique()}只 {time.time()-t0:.0f}s"); return bars

def load_min5_day(code, d):
    """加载某只股票某一天的5分钟K线"""
    for name in [code, code+'SH', code+'SZ']:
        f = MIN5_DIR / f"{name}.parquet"
        if f.exists():
            try:
                df = pd.read_parquet(str(f), columns=['datetime','open','high','low','close'])
                if df.empty: continue
                df['datetime'] = pd.to_datetime(df['datetime'])
                mask = df['datetime'].dt.date == d
                if mask.sum() < 10: continue
                r = df[mask].copy()
                r['time_str'] = r['datetime'].dt.strftime('%H:%M')
                return r.sort_values('datetime')
            except: pass
    return pd.DataFrame()

def check_bar(pos, bar):
    h=float(bar.get('high',bar.get('close',0))); l=float(bar.get('low',bar.get('close',0)))
    c=float(bar.get('close',0))
    if h>pos.peak: pos.peak=h
    pp,cpp=pos.peak/pos.eprice-1, c/pos.eprice-1
    if l<=pos.eprice*(1+HARD_STOP): return(pos.eprice*(1+HARD_STOP),"硬止损",None)
    if not pos.tp2 and h>=pos.eprice*(1+TP2_PCT): return(pos.eprice*(1+TP2_PCT),"TP2",None)
    if not pos.tp1 and h>=pos.eprice*(1+TP1_PCT):
        ss=int(pos.rem*TP1_RATIO/100)*100
        if ss>=100: return(pos.eprice*(1+TP1_PCT),"TP1",ss)
    if pp>=TRAIL_ACT and l<=pos.peak*(1-TRAIL_DD): return(pos.peak*(1-TRAIL_DD),"移动止盈",None)
    if pp>=0.03 and l<=pos.eprice: return(pos.eprice,"保本",None)
    return None

def check_eod(pos,c,d):
    cp,hd=c/pos.eprice-1,(d-pos.edate).days
    if hd>TIME_FORCE: return(c,f"时间强制({hd}天)")
    if hd>TIME_EXIT and cp>0.01: return(c,f"时间条件({hd}天)")
    return None

class E:
    def __init__(self,mode,tdates):
        self.mode=mode; self.cash=INITIAL; self.pos={}; self.tr=[]; self.eq=[]
        self.cl=0; self.pu=None; self.tdates=tdates; self.ie=0; self.ee=0
    def eqv(self,px): return self.cash+sum(p.rem*px.get(p.code,p.eprice) for p in self.pos.values() if p.active)
    def np(self): return len([p for p in self.pos.values() if p.active])
    def mp(self): return POS_CAP/2 if self.cl>=3 else POS_CAP
    def td(self,d1,d2): return sum(1 for t in self.tdates if d1<=t<=d2)
    def sell(self,pos,px,reason,partial=None,ed=None,et="15:00"):
        ss=int((partial or pos.rem)//100*100)
        if ss<=0: return None
        pos.rem-=ss
        if "TP2" in reason: pos.tp2=True
        if "TP1" in reason: pos.tp1=True
        if pos.rem<=0: pos.active=False; pos.rem=0
        self.cash+=ss*px
        rp=(px/pos.eprice-1)*100
        return T(pos.code,pos.edate,pos.eprice,ed or date.today(),px,ss,rp,ss*(px-pos.eprice),reason,self.td(pos.edate,ed) if ed else 0,self.mode)
    def buy(self,d,code,px):
        if code in self.pos: return None
        ma=min(self.mp(),self.cash)
        if ma<5000: return None
        sh=int(ma/px/100)*100
        if sh<100 and self.cash>=px*100: sh=100
        if sh<100 or sh*px>self.cash: return None
        p=P(code,d,px,"09:30",sh,sh*px); self.cash-=p.cost; self.pos[code]=p; return p
    def rec(self,d,px): self.eq.append({'date':d,'equity':self.eqv(px),'cash':self.cash,'pos':self.np()})

def run_mode(mode, sig, dd, tdates, label):
    """mode: 'intraday' (盘中监控) or 'close' (尾盘价)"""
    t0=time.time(); print(f"\n{'='*50}\n  [{label}]"); eng=E(mode,tdates)
    sbd=defaultdict(list)
    for _,r in sig.iterrows(): sbd[r['date']].append((r['code'],float(r['close'])))

    missing_5min = 0
    used_5min = 0

    for di,d in enumerate(tdates):
        epx={}
        for code,pos in list(eng.pos.items()):
            if not pos.active: continue
            exited=False

            if mode == 'intraday':
                # 盘中监控：用5分钟线逐根检查
                intra = load_min5_day(code, d)
                if not intra.empty:
                    for _,bar in intra.iterrows():
                        if exited: break
                        r=check_bar(pos,bar.to_dict())
                        if r:
                            t=eng.sell(pos,r[0],r[1],r[2] if len(r)>2 else None,d,bar['time_str'])
                            if t: eng.tr.append(t); eng.ie+=1; exited=True
                            if t and t.ret<=0: eng.cl+=1
                            else: eng.cl=0; eng.pu=None
                            if eng.cl>=LOSS_S2: eng.pu=d+timedelta(days=PAUSE_D)
                    last=intra.iloc[-1]; epx[code]=float(last['close'])
                    used_5min += 1
                    if not exited:
                        r=check_eod(pos,float(last['close']),d)
                        if r: t=eng.sell(pos,r[0],r[1],ed=d,et=last['time_str'])
                        if r and t: eng.tr.append(t); eng.ee+=1
                else:
                    # Fallback to daily OHLC
                    missing_5min += 1
                    sd=dd.get(d,{}).get(code)
                    if sd:
                        r=check_bar(pos,{'high':sd['high'],'low':sd['low'],'close':sd['close']})
                        if r:
                            t=eng.sell(pos,r[0],r[1],r[2] if len(r)>2 else None,d,"15:00")
                            if t: eng.tr.append(t); eng.ie+=1; exited=True
                            if t and t.ret<=0: eng.cl+=1
                            else: eng.cl=0; eng.pu=None
                            if eng.cl>=LOSS_S2: eng.pu=d+timedelta(days=PAUSE_D)
                        if not exited:
                            r=check_eod(pos,sd['close'],d)
                            if r: t=eng.sell(pos,r[0],r[1],ed=d)
                            if r and t: eng.tr.append(t); eng.ee+=1
                        epx[code]=sd['close']
                    else: epx[code]=pos.eprice
            else:
                # 尾盘价：只用收盘价判断是否触发止损/止盈，按收盘价卖出
                sd=dd.get(d,{}).get(code)
                t = None
                if sd:
                    cp=sd['close']; cpp=cp/pos.eprice-1
                    # 更新峰值（用收盘价）
                    if cp>pos.peak: pos.peak=cp
                    pp=pos.peak/pos.eprice-1
                    # 1. 收盘价触发硬止损
                    if cp <= pos.eprice*(1+HARD_STOP):
                        t=eng.sell(pos,cp,"硬止损",ed=d)
                        if t: eng.tr.append(t); eng.ie+=1; exited=True
                    # 2. 收盘价触发TP2
                    if not exited and not pos.tp2 and cp >= pos.eprice*(1+TP2_PCT):
                        t=eng.sell(pos,cp,"TP2",ed=d)
                        if t: eng.tr.append(t); eng.ie+=1; exited=True
                    # 3. 收盘价触发TP1
                    if not exited and not pos.tp1 and cp >= pos.eprice*(1+TP1_PCT):
                        ss=int(pos.rem*TP1_RATIO/100)*100
                        if ss>=100:
                            t=eng.sell(pos,cp,"TP1",ss,ed=d)
                            if t: eng.tr.append(t); eng.ie+=1; exited=True
                    # 4. 移动止盈：峰值曾触发+回撤（用收盘价判断回撤）
                    if not exited and pp>=TRAIL_ACT and cp<=pos.peak*(1-TRAIL_DD):
                        t=eng.sell(pos,cp,"移动止盈",ed=d)
                        if t: eng.tr.append(t); eng.ie+=1; exited=True
                    # 5. 保本：曾盈利3%，收盘跌破成本
                    if not exited and pp>=0.03 and cp<=pos.eprice:
                        t=eng.sell(pos,cp,"保本",ed=d)
                        if t: eng.tr.append(t); eng.ie+=1; exited=True
                    # 6. 时间止损
                    if not exited:
                        r=check_eod(pos,cp,d)
                        if r: t=eng.sell(pos,r[0],r[1],ed=d)
                        if r and t: eng.tr.append(t); eng.ee+=1
                    if t and t.ret<=0: eng.cl+=1
                    elif t: eng.cl=0; eng.pu=None
                    if eng.cl>=LOSS_S2: eng.pu=d+timedelta(days=PAUSE_D)
                    epx[code]=cp
                else: epx[code]=pos.eprice

        eng.pos={k:v for k,v in eng.pos.items() if v.active}

        # 买入（两种模式相同：次日开盘价买入）
        if d in sbd and (eng.pu is None or d>eng.pu):
            for code,px in sbd[d][:50]:
                if any(t.code==code and (d-t.edate).days<=SAME_COOLDOWN for t in eng.tr): continue
                # Use next-day open from daily data
                sd=dd.get(d,{}).get(code)
                entry_px = sd['open'] if sd else px
                if pd.isna(entry_px) or entry_px<=0: continue
                eng.buy(d,code,entry_px)
        eng.rec(d,epx)

        if (di+1)%150==0:
            eq=eng.eqv(epx)
            print(f"  {d} | {di+1}/{len(tdates)} | 净值 {eq:,.0f} | 持仓 {eng.np()} | 退出 {eng.ie+eng.ee} | 缺5min:{missing_5min} 用5min:{used_5min}")

    elapsed=time.time()-t0
    print(f"  完成({elapsed:.0f}s) | 盘中退出{eng.ie} 尾盘退出{eng.ee} | 缺5min:{missing_5min} 用5min:{used_5min}")
    return eng,elapsed,missing_5min,used_5min

def stats(eng,label,elapsed):
    eq=pd.DataFrame(eng.eq)
    if eq.empty or not eng.tr: return{"label":label,"trades":0}
    fe=eq['equity'].iloc[-1]; tr=(fe/INITIAL-1)*100
    eq['cm']=eq['equity'].cummax(); eq['dd']=(eq['equity']-eq['cm'])/eq['cm']*100
    md=eq['dd'].min()
    days=max((END-START).days,1)
    ann=((fe/INITIAL)**(365.25/days)-1)*100
    ts=eng.tr; w=[t for t in ts if t.ret>0]; l=[t for t in ts if t.ret<=0]
    n,nw,nl=len(ts),len(w),len(l); wr=nw/n*100 if n else 0
    aw=np.mean([t.ret for t in w]) if w else 0; al=np.mean([t.ret for t in l]) if l else 0
    at=np.mean([t.ret for t in ts]) if ts else 0; med=np.median([t.ret for t in ts]) if ts else 0
    tg=sum(t.ret for t in w); tl=abs(sum(t.ret for t in l))
    pf=tg/tl if tl>0 else float('inf')
    tp=sum(t.profit for t in ts); ah=np.mean([t.hold for t in ts]) if ts else 0
    ed=Counter(t.reason.split('(')[0] for t in ts)
    eq['year']=pd.to_datetime(eq['date']).dt.year
    yrly=[]
    for yr,g in eq.groupby('year'):
        if len(g)<10: continue
        yrly.append({'year':int(yr),'ret':(g['equity'].iloc[-1]/g['equity'].iloc[0]-1)*100,'dd':g['dd'].min()})
    return{"label":label,"mode":eng.mode,"trades":n,"wins":nw,"losses":nl,"win_rate":wr,"avg_win":aw,"avg_loss":al,"avg_return":at,"median_return":med,"final_equity":fe,"total_return":tr,"annual_return":ann,"max_dd":md,"profit_factor":pf,"total_profit":tp,"avg_hold":ah,"exit_reasons":ed,"yearly":yrly,"ie":eng.ie,"ee":eng.ee,"elapsed":elapsed}

def report(ss):
    print("\n"+"█"*72)
    print("█  MA5 策略 — 盘中监控 vs 尾盘价 (2023Q3~2026)")
    print("█"*72)
    print(f"\n  {'指标':<20}",end="")
    for s in ss: print(f" {s['label']:>16}",end="")
    print("\n  "+"-"*56)
    for lbl,k,fmt in[("总成交","trades","d"),("胜率%","win_rate",".1f"),("总收益%","total_return",".2f"),("年化%","annual_return",".2f"),("最大回撤%","max_dd",".2f"),("盈亏比","profit_factor",".2f"),("总盈亏","total_profit",",.0f"),("均盈%","avg_win",".2f"),("均亏%","avg_loss",".2f"),("均持天","avg_hold",".1f"),("盘中退出","ie","d"),("尾盘退出","ee","d"),("最终净值","final_equity",",.0f"),("耗时s","elapsed",".0f")]:
        print(f"  {lbl:<20}",end="")
        for s in ss:
            v=s.get(k,0)
            if fmt=="d": print(f" {v:>16,}",end="")
            elif fmt==",.0f": print(f" {v:>16,.0f}",end="")
            else: print(f" {v:>16{fmt}}",end="")
        print()
    for i,s in enumerate(ss):
        print(f"\n  ── {s['label']} 退出原因 ──")
        for reason,count in s['exit_reasons'].most_common(): print(f"  {reason:<32} {count:>6} ({count/s['trades']*100:5.1f}%)" if s['trades'] else "")
    print(f"\n  ── 年度收益 ──")
    print(f"  {'年份':<8}",end="")
    for s in ss: print(f" {'收益%':>8} {'回撤%':>8}",end="")
    print()
    for yr in sorted(set(y['year'] for s in ss for y in s['yearly'])):
        print(f"  {yr:<8}",end="")
        for s in ss:
            y=next((y for y in s['yearly'] if y['year']==yr),None)
            print(f" {y['ret']:>+7.1f} {y['dd']:>7.1f}" if y else f" {'-':>8} {'-':>8}",end="")
        print()

def main():
    t0=time.time()
    print("="*72); print("  MA5 策略 — 盘中监控 vs 尾盘价 (2023Q3~2026)"); print("="*72)

    bars=load_daily()
    print("[2/5] 信号...",end=" ",flush=True); t1=time.time()
    from app.screener.strategies.ma5_angle import generate_signals
    sb=bars[(bars["date"]>=START-timedelta(days=365))&(bars["date"]<=END)].copy()
    sig=generate_signals(sb,**SP)
    sig=sig[(sig["date"]>=START)&(sig["date"]<=END)].copy()
    sig["date"]=pd.to_datetime(sig["date"]).dt.date; sig=sig.sort_values(["date","code"])
    print(f"{len(sig):,}信号 {sig['code'].nunique()}只 {time.time()-t1:.0f}s")

    print("[3/5] 快照...",end=" ",flush=True); t1=time.time()
    bt=bars[(bars["date"]>=START)&(bars["date"]<=END)]
    dd=defaultdict(dict)
    for d_,g in bt.groupby("date"):
        for _,r in g.iterrows(): dd[d_][r['code']]={'open':float(r['open']),'high':float(r['high']),'low':float(r['low']),'close':float(r['close'])}
    tdates=sorted(dd.keys())
    print(f"{len(tdates)}天 {time.time()-t1:.0f}s")
    del bars,bt,sb; gc.collect()

    print("[4/5] 盘中监控...")
    eng_i,el_i,miss_i,use_i=run_mode('intraday',sig,dd,tdates,"盘中监控(5分钟线)")
    gc.collect()
    print("\n[5/5] 尾盘价...")
    eng_c,el_c,_,_=run_mode('close',sig,dd,tdates,"尾盘价判断")

    s_i=stats(eng_i,"盘中监控(5min)",el_i)
    s_c=stats(eng_c,"尾盘价判断",el_c)
    s_i['missing_5min']=miss_i; s_i['used_5min']=use_i
    report([s_i,s_c])

    print(f"\n  盘中监控: 缺5分钟数据 {miss_i} 次, 使用5分钟数据 {use_i} 次")
    print(f"  总耗时: {time.time()-t0:.0f}s")

if __name__=="__main__": main()
