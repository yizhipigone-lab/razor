#!/usr/bin/env python
"""
日线收盘�?vs 5分钟线OHLC 对比
直接调用已验证的两套回测引擎
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta, datetime
from collections import defaultdict, Counter
from pathlib import Path
import time, warnings
warnings.filterwarnings('ignore')

from app.screener.strategies.ma5_angle import generate_signals

START = date(2024, 1, 1)
END   = date.today()
ROOT  = Path(__file__).parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
MIN5_DIR  = ROOT / "data" / "parquet" / "min5"

# ── 当前config参数 ──
INITIAL_CAPITAL = 1_000_000; POSITION_SIZE = 50_000; MIN_BUY_AMT = 5_000
HARD_STOP = -0.055; TP1_PCT, TP1_SELL_RATIO = 0.04, 0.20; TP2_PCT = 0.14
TRAIL_ACTIVATE = 0.03; TRAIL_DD = 0.01
TIME_EXIT_DAYS = 5; TIME_FORCE_DAYS = 10
LOSS_STREAK_HALVE = 3; LOSS_STREAK_PAUSE = 5; PAUSE_DAYS = 3
SAME_STOCK_COOLDOWN = 20; STRATEGY_NAME = "ma5_angle"
SIGNAL_PARAMS = {
    "version": "improved", "filter_st": True, "filter_bj": True,
    "disable_quality_sort": False, "filter_consecutive_up": False, "filter_gap_quality": False,
}

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


def load_daily():
    files = [f for f in DAILY_DIR.glob("*.parquet")
             if not f.stem.startswith('index_') and len(f.stem)==6 and f.stem.isdigit()]
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
        df = df.loc[:,~df.columns.duplicated()].copy()
        keep=[c for c in ['date','open','high','low','close','volume'] if c in df.columns]
        if 'date' not in keep or 'close' not in keep: continue
        df=df[keep].copy(); df['code']=f.stem; df['date']=pd.to_datetime(df['date']).dt.date
        dfs.append(df)
    bars=pd.concat(dfs, ignore_index=True)
    for c in ['open','high','low','close','volume']:
        if c in bars.columns: bars[c]=pd.to_numeric(bars[c], errors='coerce')
    bars=bars.dropna(subset=['close'])
    bars=bars[(bars['date']>=date(2023,6,1))&(bars['date']<=END)]
    return bars.sort_values(['code','date']).reset_index(drop=True)


# ══�?日线引擎 ══�?
class EngineDaily:
    def __init__(self, td_list):
        self.cash=INITIAL_CAPITAL; self.positions={}; self.trades=[]; self.equity=[]
        self.cl=0; self.pause=None; self.td_list=td_list
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
            if cur<=HARD_STOP: sells.append((p,cp,"HS",None)); continue
            if hd>TIME_FORCE_DAYS: sells.append((p,cp,"TF",None)); continue
            if not p.tp2 and cur>=TP2_PCT: sells.append((p,cp,"TP2",None)); continue
            if not p.tp1 and cur>=TP1_PCT:
                ss=int(p.remaining*TP1_SELL_RATIO/100)*100
                if ss>=100: sells.append((p,cp,"TP1",ss)); continue
            if pp>=TRAIL_ACTIVATE:
                dd=cp/p.peak_price-1
                if dd<=-TRAIL_DD: sells.append((p,p.peak_price*(1-TRAIL_DD),"TR",None)); continue
            if hd>TIME_EXIT_DAYS and cur>0.01: sells.append((p,cp,"TC",None)); continue
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
                if t.ret<=0:
                    self.cl+=1
                    if self.cl>=LOSS_STREAK_PAUSE: self.pause=d+timedelta(days=PAUSE_DAYS)
                else: self.cl=0; self.pause=None
        self.positions={k:v for k,v in self.positions.items() if v.active}
    def record(self, d, prices):
        eq=self.eq(prices); self.equity.append({'date':d,'equity':eq,'cash':self.cash,'pos':self.pos_n()})


# ══�?5分钟线引擎（使用已验证的backtest_2025_intraday.py的load_min5逻辑）═══

_min5_cache={}

def load_min5(code):
    if code not in _min5_cache:
        suffix="SH" if code.startswith(('6','5','9')) else "SZ"
        df_all=[]
        for stem in [f"{code}{suffix}",code]:
            fp=MIN5_DIR / f"{stem}.parquet"
            if fp.exists():
                try:
                    df=pd.read_parquet(str(fp))
                    if 'datetime' in df.columns and not df.empty:
                        df['datetime']=pd.to_datetime(df['datetime'])
                        for c in ['open','high','low','close']:
                            if c in df.columns: df[c]=pd.to_numeric(df[c],errors='coerce')
                        df['date']=df['datetime'].dt.date
                        df_clean=df.dropna(subset=['open','high','low','close']).sort_values('datetime')
                        if not df_clean.empty: df_all.append(df_clean)
                except: pass
        if not df_all:
            _min5_cache[code]={}
        else:
            merged=pd.concat(df_all,ignore_index=True)
            merged=merged.drop_duplicates('datetime').sort_values('datetime')
            _min5_cache[code]={dt:g for dt,g in merged.groupby('date') if not g.empty}
    return _min5_cache[code]


class EngineIntraday:
    def __init__(self, td_list):
        self.cash=INITIAL_CAPITAL; self.positions={}; self.trades=[]; self.equity=[]
        self.cl=0; self.pause=None; self.td_list=td_list; self.intra=0; self.eod=0; self.miss=0
    def max_pos(self): return POSITION_SIZE/2 if self.cl>=LOSS_STREAK_HALVE else POSITION_SIZE
    def pos_n(self): return sum(1 for p in self.positions.values() if p.active)
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

    def check_bar(self, p, bar):
        h=bar['high']; l=bar['low']; c=bar['close']
        if h>p.peak_price: p.peak_price=h
        pp=p.peak_price/p.entry_price-1; cur=c/p.entry_price-1
        if l<=p.entry_price*(1+HARD_STOP): return (p.entry_price*(1+HARD_STOP),"HS")
        if not p.tp2 and cur>=TP2_PCT: return (c,"TP2")
        if not p.tp1 and cur>=TP1_PCT:
            ss=int(p.remaining*TP1_SELL_RATIO/100)*100
            if ss>=100: return (c,"TP1",ss)
        if pp>=TRAIL_ACTIVATE:
            if l/p.peak_price-1<=-TRAIL_DD: return (min(p.peak_price*(1-TRAIL_DD),c),"TR")
        return None

    def check_eod(self, p, d, close):
        cur=close/p.entry_price-1; hd=self._td(p.entry_date,d)
        if cur<=HARD_STOP: return (close,"HS")
        if hd>TIME_FORCE_DAYS: return (close,"TF")
        if hd>TIME_EXIT_DAYS and cur>0.01: return (close,"TC")
        return None

    def sell(self, p, px, reason, partial=None, xd=None, timing="close"):
        ss=partial if partial else p.remaining; ss=int(ss//100*100)
        if ss<=0: return None
        ret=(px/p.entry_price-1)*100; profit=ss*(px-p.entry_price)
        p.remaining-=ss
        if "TP2" in reason: p.tp2=True
        if "TP1" in reason: p.tp1=True
        if p.remaining<=0: p.active=False; p.remaining=0
        self.cash+=ss*px
        if timing=="intraday": self.intra+=1
        else: self.eod+=1
        return Trade(p.code,p.entry_date,xd or date.today(),p.entry_price,px,ss,ret,profit,reason,0,p.strategy,timing)

    def sell_phase_intraday(self, d, closes_of_day):
        for code,p in list(self.positions.items()):
            if not p.active or p.remaining<=0: continue
            cached=load_min5(code)
            bars_5m=cached.get(d) if cached else None
            if bars_5m is None:
                self.miss+=1
                cp=closes_of_day.get(code,p.entry_price)
                result=self.check_eod(p,d,cp)
                if result:
                    ep,reason,*rest=result; partial=rest[0] if rest else None
                    t=self.sell(p,ep,reason,partial,d,"close")
                    if t: t.hold=self._td(p.entry_date,d); self.trades.append(t)
                continue
            exited=False
            for _,bar in bars_5m.iterrows():
                if exited: break
                result=self.check_bar(p,{'open':float(bar['open']),'high':float(bar['high']),'low':float(bar['low']),'close':float(bar['close'])})
                if result:
                    ep,reason,*rest=result; partial=rest[0] if rest else None
                    t=self.sell(p,ep,reason,partial,d,"intraday")
                    if t: t.hold=self._td(p.entry_date,d); self.trades.append(t)
                    exited=True
            if not exited:
                last_close=float(bars_5m['close'].iloc[-1])
                result=self.check_eod(p,d,last_close)
                if result:
                    ep,reason,*rest=result; partial=rest[0] if rest else None
                    t=self.sell(p,ep,reason,partial,d,"close")
                    if t: t.hold=self._td(p.entry_date,d); self.trades.append(t)
        self.positions={k:v for k,v in self.positions.items() if v.active}
        new_l=[t for t in self.trades if t.exit_date==d and t.ret<=0]
        new_w=[t for t in self.trades if t.exit_date==d and t.ret>0]
        self.cl+=len(new_l)
        if new_w: self.cl=0; self.pause=None
        if self.cl>=LOSS_STREAK_PAUSE: self.pause=d+timedelta(days=PAUSE_DAYS)

    def record(self, d, closes_of_day):
        pv=sum(p.remaining*closes_of_day.get(p.code,p.entry_price) for p in self.positions.values() if p.active)
        self.equity.append({'date':d,'equity':self.cash+pv,'cash':self.cash,'pos':self.pos_n()})


# ══�?Main ══�?
if __name__=="__main__":
    t0=time.time()
    print("="*80)
    print("  日线收盘�?vs 5分钟线OHLC 止盈止损对比")
    print(f"  TA={TRAIL_ACTIVATE} DD={TRAIL_DD} HS={HARD_STOP} TP1={TP1_PCT} TP2={TP2_PCT}")
    print("="*80)

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

    results={}

    for mode,EngineClass in [("日线收盘�?,EngineDaily),("5分钟线OHLC",EngineIntraday)]:
        print(f"\n[3] {mode}回测...")
        t1=time.time()
        eng=EngineClass(td)

        for i,d in enumerate(td):
            if mode=="5分钟线OHLC":
                eng.sell_phase_intraday(d,closes.get(d,{}))
            else:
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

            if mode=="5分钟线OHLC":
                eng.record(d,closes.get(d,{}))
            else:
                snap_r={code:closes[d].get(code,0) for code in eng.positions}
                eng.record(d,snap_r)

            if (i+1)%100==0:
                print(f"  {d} | {i+1}/{len(td)}",end="")
                if mode=="5分钟线OHLC": print(f" | 日内{eng.intra}/尾盘{eng.eod} | 缺数据{eng.miss}",end="")
                print()

        elapsed=time.time()-t1

        trades=eng.trades
        eq=pd.DataFrame(eng.equity)
        fe=eq['equity'].iloc[-1]; tr=(fe/INITIAL_CAPITAL-1)*100
        eq['cmax']=eq['equity'].cummax(); eq['dd']=(eq['equity']-eq['cmax'])/eq['cmax']*100; md=eq['dd'].min()
        n=len(trades); wins=[t for t in trades if t.ret>0]; loses=[t for t in trades if t.ret<=0]
        nw,nl=len(wins),len(loses); wr=nw/n*100 if n>0 else 0
        aw=np.mean([t.ret for t in wins]) if wins else 0; al=np.mean([t.ret for t in loses]) if loses else 0
        ds=(td[-1]-td[0]).days; ar=(1+tr/100)**(365/max(ds,1))-1
        calmar=ar/abs(md/100) if md!=0 else 0
        rc=Counter(t.reason for t in trades)

        results[mode]={
            'ret':tr,'dd':md,'trades':n,'wins':nw,'losses':nl,
            'wr':wr,'aw':aw,'al':al,'calmar':calmar,
            'equity':fe,'reasons':dict(rc.most_common()),
            'time':elapsed
        }

        print(f"\n  {mode}: 收益{tr:+.2f}% DD{md:+.2f}% 胜率{wr:.1f}% Calmar{calmar:.2f} "
              f"交易{n}�?耗时{elapsed:.0f}s")
        if mode=="5分钟线OHLC":
            print(f"  盘中退出{eng.intra}/尾盘退出{eng.eod}/缺数据{eng.miss}")

    # ── 对比�?──
    print(f"\n{'='*80}")
    print(f"  对比总结")
    print(f"{'='*80}")
    print(f"  {'指标':<20} {'日线收盘�?:>15} {'5分钟线OHLC':>15}")
    print(f"  {'-'*50}")
    for key,label in [('ret','总收�?'),('dd','最大回�?'),('wr','胜率%'),('calmar','Calmar'),('trades','交易笔数'),('aw','均盈%'),('al','均亏%'),('equity','期末净�?)]:
        dv=results['日线收盘�?][key]; iv=results['5分钟线OHLC'][key]
        print(f"  {label:<20} {dv:>15.2f} {iv:>15.2f}")
    print(f"\n  退出原因分�?")
    for reason in ['HS','TF','TP2','TP1','TR','TC']:
        dc=results['日线收盘�?]['reasons'].get(reason,0)
        ic=results['5分钟线OHLC']['reasons'].get(reason,0)
        print(f"  {reason:<8} {dc:>15} {ic:>15}")

    print(f"\n  5分钟线耗时: {results['5分钟线OHLC']['time']:.0f}s")
    print(f"  总耗时: {time.time()-t0:.0f}s")
