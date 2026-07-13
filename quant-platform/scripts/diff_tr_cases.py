"""挑211对exit不同的案例, 分析peak/trail_line差异"""
import json, re, sys
sys.path.insert(0,'.')
import pandas as pd
from collections import defaultdict
ours = json.load(open('output/ours_gross_stop.json', encoding='utf-8'))
vera=[]
with open(r'C:\Users\liuziheng\Desktop\1TEST.txt', encoding='utf-8') as f:
    next(f)
    for line in f:
        p=line.rstrip().split('\t')
        if len(p)<12: continue
        vera.append({'code':p[1].split('.')[0],'entry_date':p[3],'entry_px':float(p[4]),
            'exit_date':p[6],'exit_px':float(p[7]),'shares':int(re.match(r'\d+',p[5]).group()),
            'pnl':float(p[10].replace('%','')),'reason':p[11]})
def key(x): return (x['code'], x['entry_date'], round(x['entry_px'],3))
def vera_cat(r):
    if r=='成本止损': return 'HS'
    if r=='阶梯止盈': return 'TP'
    if r=='移动止盈': return 'TR'
    return 'TIME'
vg=defaultdict(list); og=defaultdict(list)
for v in vera: vg[key(v)].append(v)
for o in ours: og[key(o)].append(o)

df=pd.read_parquet('output/tdx_cache/3a1b031deae13d5b.parquet')
df['date']=df['date'].astype(str)
diff_cases=[]
for k in set(vg)&set(og):
    o_by=defaultdict(list); v_by=defaultdict(list)
    for o in og[k]:
        cat='TP' if o['reason'] in('TP1','TP2') else o['reason']
        o_by[cat].append(o)
    for v in vg[k]:
        v_by[vera_cat(v['reason'])].append(v)
    for cat in o_by:
        os_=sorted(o_by[cat],key=lambda r:r['shares']); vs=sorted(v_by.get(cat,[]),key=lambda r:r['shares'])
        for i in range(min(len(os_),len(vs))):
            o,v=os_[i],vs[i]
            if abs(v['exit_px']-o['exit_px'])>=0.01 and cat=='TR':
                diff_cases.append((k,o,v))
print(f'TR同reason但exit不同: {len(diff_cases)}对')
# 挑5对, 看行情和peak
for k,o,v in diff_cases[:5]:
    code,ed,ep=k
    tdx=None
    for c in df['code'].unique():
        if c.split('.')[0]==code: tdx=c;break
    print(f'\n=== {code} 入场{ed}@{ep} ===')
    print(f'  我方TR: {o["exit_date"]}@{o["exit_px"]} ret={o["ret_pct"]}%')
    print(f'  VERA移动: {v["exit_date"]}@{v["exit_px"]} ret={v["pnl"]}%')
    if tdx:
        sub=df[(df['code']==tdx)&(df['date']>=ed.replace('-',''))].head(8)
        peak=ep
        for _,r in sub.iterrows():
            d=str(r['date']); dd=f'{d[:4]}-{d[4:6]}-{d[6:8]}'
            if r['high']>peak: peak=r['high']
            trail=peak*0.983
            print(f'  {dd}: H={r["high"]:.2f} L={r["low"]:.2f} C={r["close"]:.2f} | peak={peak:.2f} trail_line={trail:.4f}')
            if d>=o['exit_date'].replace('-','') and d>=v['exit_date'].replace('-',''): break
