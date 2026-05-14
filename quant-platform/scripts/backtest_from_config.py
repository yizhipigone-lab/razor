#!/usr/bin/env python
"""
从 config.py 读取参数的统一回测 (2024-01-01 ~ 今)
确保与模拟盘系统配置完全一致
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Optional
from pathlib import Path
from collections import Counter, defaultdict
import time, gc, warnings
warnings.filterwarnings('ignore')

# ═══════ 从 config 读取所有参数 ═══════
from app.sim_trader.config import (
    INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
    LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE, PAUSE_DAYS,
    HARD_STOP, TAKE_PROFIT_TIERS,
    TRAIL_ACTIVATE, TRAIL_DD, TIME_EXIT_DAYS, TIME_FORCE_DAYS,
    SAME_STOCK_COOLDOWN, STRATEGY_NAME, SIGNAL_PARAMS, BUY_TIME, SELL_TIME,
)
# 从 TAKE_PROFIT_TIERS 提取传统变量名（config 已废弃独立 TP1_PCT/TP2_PCT）
TP1_PCT = TAKE_PROFIT_TIERS[0]["profit_pct"]
TP1_SELL_RATIO = TAKE_PROFIT_TIERS[0]["sell_ratio"]
TP2_PCT = TAKE_PROFIT_TIERS[1]["profit_pct"]

START = date(2024,1,1)
END   = date(2026,5,1)
ROOT  = Path(__file__).parent.parent
DAILY_DIR = ROOT/"data"/"parquet"/"daily"
MIN5_DIR  = ROOT/"data"/"parquet"/"min5"

print(f"=== 从 config.py 读取的参数 ===")
print(f"初始资金: {INITIAL_CAPITAL:,}  单笔上限: {POSITION_SIZE:,}  最小买入: {MIN_BUY_AMT:,}")
print(f"连亏半仓: {LOSS_STREAK_HALVE}笔  暂停: {LOSS_STREAK_PAUSE}笔/{PAUSE_DAYS}天")
print(f"硬止损: {HARD_STOP*100:.1f}%  TP1: +{TP1_PCT*100:.0f}%卖{TP1_SELL_RATIO*100:.0f}%  TP2: +{TP2_PCT*100:.0f}%")
print(f"移动止盈: 激活{int(TRAIL_ACTIVATE*100)}% 回撤{int(TRAIL_DD*100)}%")
print(f"时间: {TIME_EXIT_DAYS}天条件/{TIME_FORCE_DAYS}天强制  冷却: {SAME_STOCK_COOLDOWN}天")
print(f"策略: {STRATEGY_NAME}  买卖时间: {BUY_TIME}/{SELL_TIME}")
print(f"信号参数: vol_threshold={SIGNAL_PARAMS['vol_threshold']} close_position={SIGNAL_PARAMS['close_position_threshold']}")

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
    print("\n[1/4] 加载日线...", end=" ", flush=True); t0=time.time()
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
    bars=bars.sort_values(['code','date']); print(f"{len(bars):,}行 {bars['code'].nunique()}只 {time.time()-t0:.0f}s"); return bars

def load_min5_day(code, d):
    for name in [code, code+'SH', code+'SZ']:
        f=MIN5_DIR/f"{name}.parquet"
        if f.exists():
            try:
                df=pd.read_parquet(str(f), columns=['datetime','open','high','low','close'])
                if df.empty: continue
                df['datetime']=pd.to_datetime(df['datetime'])
                mask=df['datetime'].dt.date==d
                if mask.sum()<10: continue
                r=df[mask].copy(); r['time_str']=r['datetime'].dt.strftime('%H:%M')
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
        ss=int(pos.rem*TP1_SELL_RATIO/100)*100
        if ss>=100: return(pos.eprice*(1+TP1_PCT),"TP1",ss)
    if pp>=TRAIL_ACTIVATE and l<=pos.peak*(1-TRAIL_DD): return(pos.peak*(1-TRAIL_DD),"移动止盈",None)
    if pp>=0.03 and l<=pos.eprice: return(pos.eprice,"保本",None)
    return None

def check_eod(pos,c,d):
    cp,hd=c/pos.eprice-1,(d-pos.edate).days
    if hd>TIME_FORCE_DAYS: return(c,f"时间强制({hd}天)")
    if hd>TIME_EXIT_DAYS and cp>0.01: return(c,f"时间条件({hd}天)")
    return None

class E:
    def __init__(self,mode,tdates):
        self.mode=mode; self.cash=INITIAL_CAPITAL; self.pos={}; self.tr=[]; self.eq=[]
        self.cl=0; self.pu=None; self.tdates=tdates; self.ie=0; self.ee=0
    def eqv(self,px): return self.cash+sum(p.rem*px.get(p.code,p.eprice) for p in self.pos.values() if p.active)
    def np(self): return len([p for p in self.pos.values() if p.active])
    def mp(self): return (POSITION_SIZE/2 if self.cl>=LOSS_STREAK_HALVE else POSITION_SIZE) if LOSS_STREAK_HALVE>0 else POSITION_SIZE
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
        if ma<MIN_BUY_AMT: return None
        sh=int(ma/px/100)*100
        if sh<100 and self.cash>=px*100: sh=100
        if sh<100 or sh*px>self.cash: return None
        p=P(code,d,px,"09:30",sh,sh*px); self.cash-=p.cost; self.pos[code]=p; return p
    def rec(self,d,px): self.eq.append({'date':d,'equity':self.eqv(px),'cash':self.cash,'pos':self.np()})

def run_mode(mode,sig,dd,tdates,label):
    t0=time.time(); print(f"\n{'='*50}\n  [{label}]"); eng=E(mode,tdates)
    sbd=defaultdict(list)
    for _,r in sig.iterrows(): sbd[r['date']].append((r['code'],float(r['close'])))
    missing_5min=0; used_5min=0
    for di,d in enumerate(tdates):
        epx={}
        for code,pos in list(eng.pos.items()):
            if not pos.active: continue
            exited=False; t=None
            if mode=='intraday':
                intra=load_min5_day(code,d)
                if not intra.empty:
                    for _,bar in intra.iterrows():
                        if exited: break
                        r=check_bar(pos,bar.to_dict())
                        if r:
                            t=eng.sell(pos,r[0],r[1],r[2] if len(r)>2 else None,d,bar['time_str'])
                            if t: eng.tr.append(t); eng.ie+=1; exited=True
                            if t and t.ret<=0: eng.cl+=1
                            else: eng.cl=0; eng.pu=None
                            if eng.cl>=LOSS_STREAK_PAUSE: eng.pu=d+timedelta(days=PAUSE_DAYS)
                    last=intra.iloc[-1]; epx[code]=float(last['close']); used_5min+=1
                    if not exited:
                        r=check_eod(pos,float(last['close']),d)
                        if r: t=eng.sell(pos,r[0],r[1],ed=d,et=last['time_str'])
                        if r and t: eng.tr.append(t); eng.ee+=1
                else:
                    missing_5min+=1
                    sd=dd.get(d,{}).get(code)
                    if sd:
                        r=check_bar(pos,{'high':sd['high'],'low':sd['low'],'close':sd['close']})
                        if r:
                            t=eng.sell(pos,r[0],r[1],r[2] if len(r)>2 else None,d,"15:00")
                            if t: eng.tr.append(t); eng.ie+=1; exited=True
                            if t and t.ret<=0: eng.cl+=1
                            else: eng.cl=0; eng.pu=None
                            if eng.cl>=LOSS_STREAK_PAUSE: eng.pu=d+timedelta(days=PAUSE_DAYS)
                        if not exited:
                            r=check_eod(pos,sd['close'],d)
                            if r: t=eng.sell(pos,r[0],r[1],ed=d)
                            if r and t: eng.tr.append(t); eng.ee+=1
                        epx[code]=sd['close']
                    else: epx[code]=pos.eprice
            else:
                sd=dd.get(d,{}).get(code); t=None
                if sd:
                    cp=sd['close']; cpp=cp/pos.eprice-1
                    if cp>pos.peak: pos.peak=cp
                    pp=pos.peak/pos.eprice-1
                    if cp<=pos.eprice*(1+HARD_STOP):
                        t=eng.sell(pos,cp,"硬止损",ed=d)
                        if t: eng.tr.append(t); eng.ie+=1; exited=True
                    if not exited and not pos.tp2 and cp>=pos.eprice*(1+TP2_PCT):
                        t=eng.sell(pos,cp,"TP2",ed=d)
                        if t: eng.tr.append(t); eng.ie+=1; exited=True
                    if not exited and not pos.tp1 and cp>=pos.eprice*(1+TP1_PCT):
                        ss=int(pos.rem*TP1_SELL_RATIO/100)*100
                        if ss>=100: t=eng.sell(pos,cp,"TP1",ss,ed=d)
                        if ss>=100 and t: eng.tr.append(t); eng.ie+=1; exited=True
                    if not exited and pp>=TRAIL_ACTIVATE and cp<=pos.peak*(1-TRAIL_DD):
                        t=eng.sell(pos,cp,"移动止盈",ed=d)
                        if t: eng.tr.append(t); eng.ie+=1; exited=True
                    if not exited and pp>=0.03 and cp<=pos.eprice:
                        t=eng.sell(pos,cp,"保本",ed=d)
                        if t: eng.tr.append(t); eng.ie+=1; exited=True
                    if not exited:
                        r=check_eod(pos,cp,d)
                        if r: t=eng.sell(pos,r[0],r[1],ed=d)
                        if r and t: eng.tr.append(t); eng.ee+=1
                    if t and t.ret<=0: eng.cl+=1
                    elif t: eng.cl=0; eng.pu=None
                    if eng.cl>=LOSS_STREAK_PAUSE: eng.pu=d+timedelta(days=PAUSE_DAYS)
                    epx[code]=cp
                else: epx[code]=pos.eprice
        eng.pos={k:v for k,v in eng.pos.items() if v.active}
        if d in sbd and (eng.pu is None or d>eng.pu):
            for code,px in sbd[d][:50]:
                if any(t.code==code and (d-t.edate).days<=SAME_STOCK_COOLDOWN for t in eng.tr): continue
                sd=dd.get(d,{}).get(code)
                entry_px=sd['open'] if sd else px
                if pd.isna(entry_px) or entry_px<=0: continue
                eng.buy(d,code,entry_px)
        eng.rec(d,epx)
        if (di+1)%120==0:
            print(f"  {d} | {di+1}/{len(tdates)} | 净值 {eng.eqv(epx):,.0f} | 持仓 {eng.np()} | 退出 {eng.ie+eng.ee}")
    elapsed=time.time()-t0
    print(f"  完成({elapsed:.0f}s) 盘中{eng.ie} 尾盘{eng.ee} | 缺5min:{missing_5min} 用5min:{used_5min}")
    return eng,elapsed

def stats(eng,label,elapsed):
    eq=pd.DataFrame(eng.eq)
    if eq.empty or not eng.tr: return{"label":label,"trades":0}
    fe=eq['equity'].iloc[-1]; tr=(fe/INITIAL_CAPITAL-1)*100
    eq['cm']=eq['equity'].cummax(); eq['dd']=(eq['equity']-eq['cm'])/eq['cm']*100; md=eq['dd'].min()
    days=max((END-START).days,1); ann=((fe/INITIAL_CAPITAL)**(365.25/days)-1)*100
    ts=eng.tr; w=[t for t in ts if t.ret>0]; l=[t for t in ts if t.ret<=0]
    n,nw,nl=len(ts),len(w),len(l); wr=nw/n*100 if n else 0
    aw=np.mean([t.ret for t in w]) if w else 0; al=np.mean([t.ret for t in l]) if l else 0
    at=np.mean([t.ret for t in ts]) if ts else 0; med=np.median([t.ret for t in ts]) if ts else 0
    tg=sum(t.ret for t in w); tl=abs(sum(t.ret for t in l)); pf=tg/tl if tl>0 else float('inf')
    tp=sum(t.profit for t in ts); ah=np.mean([t.hold for t in ts]) if ts else 0
    ed=Counter(t.reason.split('(')[0] for t in ts)
    eq['year']=pd.to_datetime(eq['date']).dt.year; yrly=[]
    for yr,g in eq.groupby('year'):
        if len(g)<10: continue
        yrly.append({'year':int(yr),'ret':(g['equity'].iloc[-1]/g['equity'].iloc[0]-1)*100,'dd':g['dd'].min()})
    return{"label":label,"mode":eng.mode,"trades":n,"wins":nw,"losses":nl,"win_rate":wr,"avg_win":aw,"avg_loss":al,"avg_return":at,"final_equity":fe,"total_return":tr,"annual_return":ann,"max_dd":md,"profit_factor":pf,"total_profit":tp,"avg_hold":ah,"exit_reasons":ed,"yearly":yrly,"ie":eng.ie,"ee":eng.ee,"elapsed":elapsed}

def report(ss):
    print("\n"+"█"*68)
    print("█  MA5 策略 — 使用 config.py 参数 (2024-01 ~ 今)")
    print("█"*68)
    print(f"\n  {'指标':<20}",end="")
    for s in ss: print(f" {s['label']:>16}",end="")
    print("\n  "+"-"*52)
    for lbl,k,fmt in[("总成交","trades","d"),("胜率%","win_rate",".1f"),("总收益%","total_return",".2f"),("年化%","annual_return",".2f"),("最大回撤%","max_dd",".2f"),("盈亏比","profit_factor",".2f"),("总盈亏","total_profit",",.0f"),("均盈%","avg_win",".2f"),("均亏%","avg_loss",".2f"),("均持天","avg_hold",".1f"),("盘中退出","ie","d"),("尾盘退出","ee","d"),("最终净值","final_equity",",.0f"),("耗时s","elapsed",".0f")]:
        print(f"  {lbl:<20}",end="")
        for s in ss:
            v=s.get(k,0)
            if fmt=="d": print(f" {v:>16,}",end="")
            elif fmt==",.0f": print(f" {v:>16,.0f}",end="")
            else: print(f" {v:>16{fmt}}",end="")
        print()
    for i,s in enumerate(ss):
        print(f"\n  [{s['label']}] 退出原因:")
        for reason,count in s['exit_reasons'].most_common(): print(f"    {reason:<30} {count:>6} ({count/s['trades']*100:5.1f}%)" if s['trades'] else "")
    print(f"\n  [年度]")
    for yr in sorted(set(y['year'] for s in ss for y in s['yearly'])):
        parts="  "+str(yr)
        for s in ss:
            y=next((y for y in s['yearly'] if y['year']==yr),None)
            parts+=f"  {y['ret']:>+6.1f}%/{y['dd']:>+5.1f}%" if y else "       -       "
        print(parts)

def main():
    t0=time.time()
    bars=load_daily()
    print("[2/4] 信号...",end=" ",flush=True); t1=time.time()
    from app.screener.strategies.ma5_angle import generate_signals
    sb=bars[(bars["date"]>=START-timedelta(days=365))&(bars["date"]<=END)].copy()
    sig=generate_signals(sb,**SIGNAL_PARAMS)
    sig=sig[(sig["date"]>=START)&(sig["date"]<=END)].copy()
    sig["date"]=pd.to_datetime(sig["date"]).dt.date; sig=sig.sort_values(["date","code"])
    print(f"{len(sig):,}信号 {sig['code'].nunique()}只 {time.time()-t1:.0f}s")
    print("[3/4] 快照...",end=" ",flush=True); t1=time.time()
    bt=bars[(bars["date"]>=START)&(bars["date"]<=END)]
    dd=defaultdict(dict)
    for d_,g in bt.groupby("date"):
        for _,r in g.iterrows(): dd[d_][r['code']]={'open':float(r['open']),'high':float(r['high']),'low':float(r['low']),'close':float(r['close'])}
    tdates=sorted(dd.keys()); print(f"{len(tdates)}天 {time.time()-t1:.0f}s")
    del bars,bt,sb; gc.collect()
    all_stats=[]
    for mode,label in[("intraday","盘中监控(5min)"),("close","尾盘价判断")]:
        eng,el=run_mode(mode,sig,dd,tdates,label); gc.collect()
        all_stats.append(stats(eng,label,el))
    report(all_stats)
    print(f"\n总耗时: {time.time()-t0:.0f}s")

if __name__=="__main__": main()
