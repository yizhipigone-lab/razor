"""挑TP/TR分叉案例, 带行情分析分叉点"""
import sys, json, re
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
import pandas as pd

ours = json.load(open('output/ours_gross_stop.json', encoding='utf-8'))
vera=[]
with open(r'C:\Users\liuziheng\Desktop\1TEST.txt', encoding='utf-8') as f:
    next(f)
    for line in f:
        p=line.rstrip().split('\t')
        if len(p)<12: continue
        vera.append({'code':p[1].split('.')[0],'name':p[2],'entry_date':p[3],'entry_px':float(p[4]),
            'exit_date':p[6],'exit_px':float(p[7]),'shares':int(re.match(r'\d+',p[5]).group()),
            'hold':int(p[9]),'pnl':float(p[10].replace('%','')),'reason':p[11]})

def key(x): return (x['code'], x['entry_date'], round(x['entry_px'],3))
from collections import defaultdict
vg=defaultdict(list); og=defaultdict(list)
for v in vera: vg[key(v)].append(v)
for o in ours: og[key(o)].append(o)

REMAP={'HS':'成本止损','TP1':'阶梯止盈','TP2':'阶梯止盈','TR':'移动止盈','FE':'时间止盈','TF':'时间止损','TC':'时间止盈'}

# 找分叉案例: 我方TR vs VERA阶梯, 我方阶梯 vs VERA TR
cases_tr_tp = []  # 我方TR, VERA阶梯
cases_tp_tr = []  # 我方阶梯, VERA TR
for k in set(vg)&set(og):
    vs=sorted(vg[k],key=lambda r:r['shares']); os_=sorted(og[k],key=lambda r:r['shares'])
    for i in range(min(len(vs),len(os_))):
        v,o=vs[i],os_[i]
        oremap=REMAP.get(o['reason'],o['reason'])
        if o['reason']=='TR' and v['reason']=='阶梯止盈':
            cases_tr_tp.append((k,v,o))
        elif oremap=='阶梯止盈' and v['reason']=='移动止盈':
            cases_tp_tr.append((k,v,o))

# 加载parquet行情
df = pd.read_parquet('output/tdx_cache/3a1b031deae13d5b.parquet')
df['date']=df['date'].astype(str)

def show_case(k, v, o, label):
    code, ed, ep = k
    # 取该票entry_date后10天OHLC (parquet code是 000001.SZ 格式)
    tdx_code = None
    for c in df['code'].unique():
        if c.split('.')[0]==code: tdx_code=c; break
    if tdx_code is None:
        print(f'{code} parquet无数据'); return
    sub = df[(df['code']==tdx_code)&(df['date']>=ed.replace('-',''))].head(12)
    print(f'\n=== {label}: {code} {v.get("name","")} ===')
    print(f'入场: {ed} @ {ep}')
    print(f'我方: {o["exit_date"]} @ {o["exit_px"]} {o["reason"]} ret={o["ret_pct"]}% hold={o["hold"]}')
    print(f'VERA: {v["exit_date"]} @ {v["exit_px"]} {v["reason"]} ret={v["pnl"]}% hold={v["hold"]}')
    print(f'行情(entry后):')
    for _,r in sub.iterrows():
        d=str(r['date'])
        dd=f'{d[:4]}-{d[4:6]}-{d[6:8]}'
        chg=(r['close']/ep-1)*100 if r['close']>0 else 0
        hi=(r['high']/ep-1)*100 if r['high']>0 else 0
        lo=(r['low']/ep-1)*100 if r['low']>0 else 0
        print(f'  {dd}: O={r["open"]:.2f} H={r["high"]:.2f}(+{hi:.1f}%) L={r["low"]:.2f}({lo:.1f}%) C={r["close"]:.2f}(+{chg:.1f}%)')

print(f'案例池: 我方TR/VERA阶梯={len(cases_tr_tp)}  我方阶梯/VERA TR={len(cases_tp_tr)}')
# 各挑3对
for k,v,o in cases_tr_tp[:3]: show_case(k,v,o,'我方=移动止盈, VERA=阶梯止盈')
for k,v,o in cases_tp_tr[:3]: show_case(k,v,o,'我方=阶梯止盈, VERA=移动止盈')
