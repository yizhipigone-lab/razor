"""基于缓存parquet做快速per-signal三维度分析(阶段性,不等后台回测)"""
import sys, json, time
sys.path.insert(0,'.')
import pandas as pd
from collections import defaultdict
from app.backtest.exit_rules import exit_rule_engine

t0=time.time()
df = pd.read_parquet('output/tdx_cache/be1b896ae2789862.parquet')
df['date']=df['date'].astype(str)
imv = json.load(open(r'C:\Users\liuziheng\AppData\Local\Temp\hs_industry_mv.json',encoding='utf-8'))
industry, floatvol = imv['industry'], imv['floatvol']
print(f'parquet {len(df)}行 {df["code"].nunique()}只, 耗时{time.time()-t0:.0f}s')

params={'hard_stop':-0.046,'trail_activate':0.039,'trail_dd':0.017,
    'take_profit_tiers':[{'profit_pct':0.03,'sell_ratio':0.2},{'profit_pct':0.13,'sell_ratio':0.6}],
    'realistic_stop_fill':'stop','use_atr_trail':True,'atr_trail_multiplier':1,
    'time_exit_days':7,'time_exit_profit':0.025,'time_force_days':12,
    'first_day_exit_min_profit':0,'first_day_exit_days':1,
    'priority_mode':'trailing_first','tp_stack_mode':True,'tp1_fill_pct':0.03}

class P: pass
def run_signal(bars, i):
    entry=bars[i]['close']
    pos=P(); pos.entry_price=entry; pos.peak_price=entry; pos.shares=100; pos.remaining=100; pos.tp_triggered=set()
    for j in range(i+1, min(i+25, len(bars))):
        b=bars[j]
        if not(b['close']>0 and b['high']>0 and b['low']>0 and b['open']>0): continue
        pos.peak_price=max(pos.peak_price, b['high'])
        hd=j-i
        ctx=exit_rule_engine.build_context(pos, {'open':b['open'],'high':b['high'],'low':b['low'],'close':b['close']}, hd, params, use_high_for_tp=True)
        sigs=exit_rule_engine.check_all(ctx)
        if sigs:
            # 取最后一个(全卖)或最关键的
            s=sigs[-1]
            return (s.sell_price/entry-1)*100, s.reason
    return None, None

results=[]
n_sig=0; t1=time.time()
for code, g in df.groupby('code'):
    g=g.sort_values('date')
    bars=g[['date','open','high','low','close','signal_value']].to_dict('records')
    fv=floatvol.get(code,0)
    ind=industry.get(code,'未分类')
    for i,b in enumerate(bars):
        if str(b.get('signal_value','0'))!='1': continue
        if not(b['close']>0 and b['high']>0): continue
        n_sig+=1
        ret,reason=run_signal(bars,i)
        if ret is not None:
            mv=b['close']*fv/1e8 if fv else None
            results.append({'code':code,'date':b['date'],'entry':b['close'],'ret':ret,'reason':reason,
                'industry':ind,'mv':mv,'year':b['date'][:4]})
    if n_sig%20000==0 and n_sig>0:
        print(f'  信号{n_sig} 已处理{len(results)} 耗时{time.time()-t1:.0f}s', flush=True)

print(f'\n总信号{n_sig}, 有效结果{len(results)}, per-signal耗时{time.time()-t1:.0f}s')
json.dump(results, open(r'C:\Users\liuziheng\AppData\Local\Temp\sig_results.json','w'))

# === 三维度分析 ===
def wr(items):
    if not items: return 0,0
    w=sum(1 for x in items if x['ret']>0)
    return w/len(items)*100, len(items)
def avgr(items):
    return sum(x['ret'] for x in items)/len(items) if items else 0

print('\n=== 1. 市值区间盈利率 ===')
mvb={'小<100亿':[0,100],'中100-500亿':[100,500],'大>500亿':[500,99999]}
for k,(lo,hi) in mvb.items():
    its=[x for x in results if x['mv'] and lo<=x['mv']<hi]
    w,n=wr(its)
    print(f'  {k}: {n}笔 胜率{w:.1f}% 平均ret{avgr(its):+.2f}%')

print('\n=== 2. 年份×行业(各年胜率最高行业,≥50笔) ===')
yr=defaultdict(list)
for x in results: yr[x['year']].append(x)
for y in sorted(yr):
    yi=defaultdict(list)
    for x in yr[y]: yi[x['industry']].append(x)
    rk=[(name,*wr(ts),avgr(ts)) for name,ts in yi.items() if len(ts)>=50]
    rk.sort(key=lambda z:-z[1])
    top=rk[:3]
    print(f'  {y}: '+' '.join(f'{n}({w:.0f}%/{c},{a:+.1f}%)' for n,w,c,a in top))

print('\n=== 3. 股价区间盈利率 ===')
pxb={'低价<10元':[0,10],'中价10-30':[10,30],'中高30-100':[30,100],'高价>100':[100,99999]}
for k,(lo,hi) in pxb.items():
    its=[x for x in results if lo<=x['entry']<hi]
    w,n=wr(its)
    print(f'  {k}: {n}笔 胜率{w:.1f}% 平均ret{avgr(its):+.2f}%')
print(f'\n总耗时{time.time()-t0:.0f}s')
