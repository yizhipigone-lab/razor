#!/usr/bin/env python
"""
时间条件退�?× 时间强制退�?�?联合网格搜索
参数:
  time_exit_days  �?持仓N天后检�?  time_exit_profit �?盈利超过X%触发清仓
  time_force_days �?M天无条件强制清仓
�?2023-01-01 至今，日线收盘价
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta
from collections import defaultdict, Counter
from pathlib import Path
import time, json, warnings
warnings.filterwarnings('ignore')

from app.screener.strategies.ma5_angle import generate_signals

START = date(2023, 1, 1)
END   = date.today()
ROOT  = Path(__file__).parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"

# ── 固定参数（Trail等其他参数保持最优）──
INITIAL_CAPITAL = 1_000_000; POSITION_SIZE = 50_000; MIN_BUY_AMT = 5_000
HARD_STOP = -0.055; TP1_PCT = 0.04; TP1_SELL_RATIO = 0.20; TP2_PCT = 0.14
TRAIL_ACTIVATE = 0.03; TRAIL_DD = 0.01
LOSS_STREAK_HALVE = 3; LOSS_STREAK_PAUSE = 5; PAUSE_DAYS = 3
SAME_STOCK_COOLDOWN = 20; STRATEGY_NAME = "ma5_angle"
SIGNAL_PARAMS = {
    "version": "improved", "filter_st": True, "filter_bj": True,
    "disable_quality_sort": False, "filter_consecutive_up": False, "filter_gap_quality": False,
}

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
    def __init__(self, td_list, p):
        self.cash=INITIAL_CAPITAL; self.positions={}; self.trades=[]; self.equity=[]
        self.cl=0; self.pause=None; self.td_list=td_list; self.p=p

    def max_pos(self): return POSITION_SIZE/2 if self.cl>=LOSS_STREAK_HALVE else POSITION_SIZE
    def pos_n(self): return sum(1 for p in self.positions.values() if p.active)
    def _td(self, d1, d2): return sum(1 for td in self.td_list if d1<=td<=d2)
    def eq(self, prices):
        pv=0
        for p in self.positions.values():
            if not p.active: continue
            bar=prices.get(p.code,{}); px=bar.get('close',p.entry_price) if isinstance(bar,dict) else (bar if bar else p.entry_price)
            pv+=p.remaining*px
        return self.cash+pv

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

    def check_stops(self, d, snap):
        sells=[]
        for code,p in list(self.positions.items()):
            if not p.active or p.remaining<=0: continue
            bar=snap.get(code)
            if bar is None: continue
            cp=bar['close']; hp=bar.get('high',cp)
            if hp>p.peak_price: p.peak_price=hp
            pp=p.peak_price/p.entry_price-1; cur=cp/p.entry_price-1; hd=self._td(p.entry_date,d)

            # 1. 硬止�?            if cur<=HARD_STOP: sells.append((p,cp,"HS",None)); continue
            # 2. 时间强制清仓（无条件�?            if hd>self.p['force_days']: sells.append((p,cp,"TF",None)); continue
            # 3. TP2
            if not p.tp2 and cur>=TP2_PCT: sells.append((p,cp,"TP2",None)); continue
            # 4. TP1
            if not p.tp1 and cur>=TP1_PCT:
                ss=int(p.remaining*TP1_SELL_RATIO/100)*100
                if ss>=100: sells.append((p,cp,"TP1",ss)); continue
            # 5. 移动止盈
            if pp>=TRAIL_ACTIVATE:
                dd=cp/p.peak_price-1
                if dd<=-TRAIL_DD: sells.append((p,p.peak_price*(1-TRAIL_DD),"TR",None)); continue
            # 6. 时间条件退出（N天后盈利>X%�?            if hd>self.p['exit_days'] and cur>self.p['exit_profit']:
                sells.append((p,cp,"TC",None)); continue
        return sells

    def sell(self, p, px, reason, partial=None, xd=None):
        ss=partial if partial else p.remaining; ss=int(ss//100*100)
        if ss<=0: return None
        ret=(px/p.entry_price-1)*100; profit=ss*(px-p.entry_price)
        p.remaining-=ss
        if "TP2" in reason: p.tp2=True
        if "TP1" in reason: p.tp1=True
        if p.remaining<=0: p.active=False; p.remaining=0
        self.cash+=ss*px
        return Trade(p.code,p.entry_date,xd or date.today(),p.entry_price,px,ss,ret,profit,reason,0,p.strategy,"close")

    def sell_phase(self, d, snap):
        for p,px,reason,partial in self.check_stops(d,snap):
            t=self.sell(p,px,reason,partial,d)
            if t:
                t.hold=self._td(p.entry_date,d); self.trades.append(t)
                if t.ret<=0: self.cl+=1
                else: self.cl=0; self.pause=None
                if self.cl>=LOSS_STREAK_PAUSE: self.pause=d+timedelta(days=PAUSE_DAYS)
        self.positions={k:v for k,v in self.positions.items() if v.active}

    def record(self, d, prices):
        eq=self.eq(prices); self.equity.append({'date':d,'equity':eq,'cash':self.cash,'pos':self.pos_n()})


def load_daily():
    files=[f for f in DAILY_DIR.glob("*.parquet")
           if not f.stem.startswith('index_') and len(f.stem)==6 and f.stem.isdigit()]
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
        df=df[keep].copy(); df['code']=f.stem; df['date']=pd.to_datetime(df['date']).dt.date
        dfs.append(df)
    bars=pd.concat(dfs,ignore_index=True)
    for c in ['open','high','low','close','volume']:
        if c in bars.columns: bars[c]=pd.to_numeric(bars[c],errors='coerce')
    bars=bars.dropna(subset=['close'])
    bars=bars[(bars['date']>=date(2022,6,1))&(bars['date']<=END)]
    return bars.sort_values(['code','date']).reset_index(drop=True)


def run_one(p, bars, sig, td, closes, highs, sbd):
    eng=Engine(td,p)
    for d in td:
        snap={}
        for code in eng.positions:
            if d in closes and code in closes[d]:
                snap[code]={'open':closes[d].get(code,0),'high':highs[d].get(code,closes[d].get(code,0)),
                            'low':closes[d].get(code,0),'close':closes[d].get(code,0)}
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
    eq=pd.DataFrame(eng.equity)
    fe=eq['equity'].iloc[-1]; tr=(fe/INITIAL_CAPITAL-1)*100
    eq['cmax']=eq['equity'].cummax(); eq['dd']=(eq['equity']-eq['cmax'])/eq['cmax']*100; md=eq['dd'].min()
    n=len(trades); wins=[t for t in trades if t.ret>0]; loses=[t for t in trades if t.ret<=0]
    nw,nl=len(wins),len(loses); wr=nw/n*100 if n>0 else 0
    aw=np.mean([t.ret for t in wins]) if wins else 0; al=np.mean([t.ret for t in loses]) if loses else 0
    ds=(td[-1]-td[0]).days; ar=(1+tr/100)**(365/max(ds,1))-1
    calmar=ar/abs(md/100) if md!=0 else 0
    rc=Counter(t.reason for t in trades)
    prof_factor=sum(t.profit for t in wins)/abs(sum(t.profit for t in loses)) if loses and sum(t.profit for t in loses)!=0 else 0

    return {
        'ret':round(tr,2),'dd':round(md,2),'n':n,'nw':nw,'nl':nl,
        'wr':round(wr,1),'aw':round(aw,2),'al':round(al,2),
        'calmar':round(calmar,2),'pf':round(prof_factor,2),
        'fe':round(fe,0),'reasons':dict(rc.most_common()),
    }


# ══════════════════════════════════════════════════════════�?# Main
# ══════════════════════════════════════════════════════════�?
if __name__=="__main__":
    t0=time.time()
    print("="*90)
    print("  时间条件退�?× 时间强制退�?�?联合优化")
    print("  区间: 2023-01-01 ~ today")
    print("="*90)

    print("\n[1/3] 加载数据...")
    bars=load_daily()
    print(f"  {bars.code.nunique():,} stocks, {len(bars):,} rows")

    print("[2/3] 生成信号...")
    sig=generate_signals(bars,**SIGNAL_PARAMS)
    sig=sig[(sig['date']>=START)&(sig['date']<=END)].copy()
    sig['date']=pd.to_datetime(sig['date']).dt.date

    bt=bars[(bars['date']>=START)&(bars['date']<=END)]
    closes,highs={},{}
    for d,g in bt.groupby('date'):
        closes[d]=dict(zip(g['code'],g['close']))
        highs[d]=dict(zip(g['code'],g['high']))
    td=sorted(closes.keys())
    sbd=defaultdict(list)
    for _,r in sig.iterrows(): sbd[r['date']].append((r['code'],float(r['close'])))
    print(f"  交易�? {len(td)} | 信号: {len(sig):,}")

    # 参数空间
    exit_days_vals  = [3, 4, 5, 6, 7, 8, 10, 12]
    exit_profit_vals = [0.005, 0.01, 0.015, 0.02, 0.03, 0.05]
    force_days_vals  = [5, 7, 8, 9, 10, 12, 15, 20]

    # 生成所有有效组�?(exit_days < force_days)
    trials=[]
    for ed in exit_days_vals:
        for ep in exit_profit_vals:
            for fd in force_days_vals:
                if ed>=fd: continue  # 条件退出必须在强制退出之�?                name=f"D{ed}/>{ep*100:.1f}%/F{fd}"
                base={
                    'hard_stop':HARD_STOP,'tp1_pct':TP1_PCT,'tp1_sell_ratio':TP1_SELL_RATIO,
                    'tp2_pct':TP2_PCT,'trail_activate':TRAIL_ACTIVATE,'trail_dd':TRAIL_DD,
                    'exit_days':ed,'exit_profit':ep,'force_days':fd,
                    'same_stock_cooldown':SAME_STOCK_COOLDOWN,
                }
                trials.append((name,base))

    # 去重：同 exit_days �?exit_profit �?force_days 只留一�?    seen=set()
    unique_trials=[]
    for name,p in trials:
        key=(p['exit_days'],p['exit_profit'],p['force_days'])
        if key not in seen:
            seen.add(key)
            unique_trials.append((name,p))
    trials=unique_trials

    # 加上 baseline 对照：当前参�?(exit=5, profit=1%, force=10)
    baseline_params={
        'hard_stop':HARD_STOP,'tp1_pct':TP1_PCT,'tp1_sell_ratio':TP1_SELL_RATIO,
        'tp2_pct':TP2_PCT,'trail_activate':TRAIL_ACTIVATE,'trail_dd':TRAIL_DD,
        'exit_days':5,'exit_profit':0.01,'force_days':10,
        'same_stock_cooldown':SAME_STOCK_COOLDOWN,
    }
    trials.insert(0,("�?BASELINE D5/>1%/F10",baseline_params))

    # 也加上无时间条件的对�?    no_tc_params=dict(baseline_params)
    no_tc_params['exit_days']=999; no_tc_params['force_days']=999
    trials.insert(0,("�?无时间条�?,no_tc_params))

    print(f"\n[3/3] 运行 {len(trials)} 组参�?..")
    results=[]
    best_calmar=-999

    for idx,(name,p) in enumerate(trials):
        t1=time.time()
        r=run_one(p,bars,sig,td,closes,highs,sbd)
        elapsed=time.time()-t1

        if r is None: print(f"  [{idx+1:>3}/{len(trials)}] {name:<28} 无交�?); continue

        r['name']=name; r['exit_days']=p['exit_days']; r['exit_profit']=p['exit_profit']
        r['force_days']=p['force_days']; r['time']=round(elapsed,1)
        # 综合评分：Calmar为主
        r['score']=round(r['calmar']*r['wr']/100,2)
        results.append(r)

        marker=" �? if r['calmar']>best_calmar else ""
        if r['calmar']>best_calmar: best_calmar=r['calmar']

        tc_count=r['reasons'].get('TC',0); tf_count=r['reasons'].get('TF',0)
        print(f"  [{idx+1:>3}/{len(trials)}] {name:<28} "
              f"收益{r['ret']:>+7.2f}% DD{r['dd']:>+6.2f}% "
              f"胜率{r['wr']:>5.1f}% Calmar{r['calmar']:>6.2f} "
              f"TC{tc_count:>4} TF{tf_count:>3}{marker}")

    # ── 排名 ──
    results.sort(key=lambda x: x['calmar'], reverse=True)

    print(f"\n{'='*90}")
    print(f"  TOP 30 (按Calmar排名)")
    print(f"{'='*90}")
    print(f"  {'排名':<5} {'参数':<30} {'收益%':>8} {'DD%':>6} {'胜率%':>6} {'Calmar':>7} {'交易':>5} {'TC':>5} {'TF':>4}")
    print(f"  {'-'*80}")

    for i,r in enumerate(results[:30]):
        print(f"  {i+1:<5} {r['name']:<30} {r['ret']:>+8.2f} {r['dd']:>+6.2f} "
              f"{r['wr']:>5.1f} {r['calmar']:>7.2f} {r['n']:>5} "
              f"{r['reasons'].get('TC',0):>5} {r['reasons'].get('TF',0):>4}")

    # ── 固定维度分析 ──
    print(f"\n{'='*90}")
    print(f"  按「时间条件退出天数」分组最�?)
    print(f"{'='*90}")
    for ed in exit_days_vals:
        subset=[r for r in results if r['exit_days']==ed]
        if not subset: continue
        best=max(subset,key=lambda x:x['calmar'])
        print(f"  D{ed}: 最�?{best['name']:<25} 收益{best['ret']:>+7.2f}% DD{best['dd']:>+6.2f}% Calmar{best['calmar']:>6.2f}")

    print(f"\n{'='*90}")
    print(f"  按「时间条件盈利阈值」分组最�?)
    print(f"{'='*90}")
    for ep in exit_profit_vals:
        subset=[r for r in results if abs(r['exit_profit']-ep)<0.0001]
        if not subset: continue
        best=max(subset,key=lambda x:x['calmar'])
        print(f"  >{ep*100:.1f}%: 最�?{best['name']:<25} 收益{best['ret']:>+7.2f}% DD{best['dd']:>+6.2f}% Calmar{best['calmar']:>6.2f}")

    print(f"\n{'='*90}")
    print(f"  按「强制清仓天数」分组最�?)
    print(f"{'='*90}")
    for fd in force_days_vals:
        subset=[r for r in results if r['force_days']==fd]
        if not subset: continue
        best=max(subset,key=lambda x:x['calmar'])
        print(f"  F{fd}: 最�?{best['name']:<25} 收益{best['ret']:>+7.2f}% DD{best['dd']:>+6.2f}% Calmar{best['calmar']:>6.2f}")

    # ── 最优详�?──
    best=results[0]
    print(f"\n{'='*90}")
    print(f"  最优参数组�?)
    print(f"{'='*90}")
    print(f"  名称: {best['name']}")
    print(f"  时间条件: 持仓>{best['exit_days']}�?�?盈利>{best['exit_profit']*100:.1f}% �?清仓")
    print(f"  时间强制: 持仓>{best['force_days']}�?�?无条件清�?)
    print(f"  总收�? {best['ret']:+.2f}%  |  最大回�? {best['dd']:+.2f}%")
    print(f"  胜率: {best['wr']:.1f}%  |  Calmar: {best['calmar']:.2f}")
    print(f"  交易: {best['n']}�? |  盈利{best['nw']}/亏损{best['nl']}")
    print(f"  均盈: {best['aw']:+.2f}%  |  均亏: {best['al']:+.2f}%")
    print(f"  盈亏�? {best['pf']:.2f}")
    print(f"  退出分�? {best['reasons']}")

    # 与baseline对比
    bl=[r for r in results if 'BASELINE' in r['name']]
    no_tc=[r for r in results if '无时间条�? in r['name']]
    if bl:
        b=bl[0]
        print(f"\n  对比BASELINE(D5/>1%/F10):")
        print(f"    收益: {b['ret']:+.2f}% �?{best['ret']:+.2f}%  (Δ{best['ret']-b['ret']:+.2f}%)")
        print(f"    回撤: {b['dd']:+.2f}% �?{best['dd']:+.2f}%  (Δ{best['dd']-b['dd']:+.2f}%)")
        print(f"    Calmar: {b['calmar']:.2f} �?{best['calmar']:.2f}")
    if no_tc:
        n=no_tc[0]
        print(f"\n  对比无时间条�?")
        print(f"    收益: {n['ret']:+.2f}% �?{best['ret']:+.2f}%  (Δ{best['ret']-n['ret']:+.2f}%)")
        print(f"    回撤: {n['dd']:+.2f}% �?{best['dd']:+.2f}%  (Δ{best['dd']-n['dd']:+.2f}%)")

    print(f"\n  总耗时: {time.time()-t0:.0f}s")
