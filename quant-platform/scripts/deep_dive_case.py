"""深挖单笔案例 + 统计 hold 差异 + 拉我方 parquet OHLC 对照"""
import json, sys
from collections import Counter
from pathlib import Path
import pandas as pd

OURS = json.load(open('output/backtest_results/bt_20260709_000234_1783526554.json', encoding='utf-8'))

# 1. 找 688106 所有 trade
print('=== 我方 688106 所有交易 ===')
for t in OURS['trades']:
    if t['code']=='688106':
        print(json.dumps(t, ensure_ascii=False))

# 2. 拉我方 parquet 688106 日线
print('\n=== 我方 parquet 688106 日线 03-20~03-27 ===')
p = Path('data/parquet/daily/688106.parquet')
if p.exists():
    df = pd.read_parquet(p)
    print('cols:', list(df.columns))
    # 找 2026-03 附近
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'])
        sub = df[(df['date']>='2026-03-18')&(df['date']<='2026-03-28')]
        print(sub.to_string())
    else:
        print(df.head())
else:
    print('parquet 不存在:', p)

# 3. 重新解析 VERA 688106
print('\n=== VERA 688106 ===')
with open(r'C:\Users\liuziheng\Desktop\1TEST.txt', encoding='utf-8') as f:
    next(f)
    for line in f:
        parts = line.rstrip().split('\t')
        if len(parts)>=12 and parts[1].startswith('688106'):
            print(parts)

# 4. hold 差异统计（共同键配对）
import re
def parse_vera():
    rows=[]
    with open(r'C:\Users\liuziheng\Desktop\1TEST.txt', encoding='utf-8') as f:
        next(f)
        for line in f:
            parts=line.rstrip().split('\t')
            if len(parts)<12: continue
            seq,code,name,ed,ep,eq,xd,xp,xq,hold,pnl,reason=parts[:12]
            rows.append({'code':code.split('.')[0],'entry_date':ed,'entry_px':float(ep),
                         'exit_date':xd,'exit_px':float(xp),'hold':int(hold),'shares':int(re.match(r'\d+',eq).group()),
                         'reason':reason})
    return rows
vera=parse_vera()
ours=[{'code':t['code'],'entry_date':t['entry_date'],'entry_px':t['entry_px'],
       'exit_date':t['exit_date'],'exit_px':t['exit_px'],'hold':t['hold_days'],
       'shares':t['shares'],'reason':t['reason']} for t in OURS['trades']]
from collections import defaultdict
vg=defaultdict(list);og=defaultdict(list)
for r in vera: vg[(r['code'],r['entry_date'],round(r['entry_px'],3))].append(r)
for r in ours: og[(r['code'],r['entry_date'],round(r['entry_px'],3))].append(r)
common=set(vg)&set(og)
hold_diff=Counter(); exitdate_diff=0; pairs=0
for k in common:
    vs=sorted(vg[k],key=lambda r:r['shares']); os_=sorted(og[k],key=lambda r:r['shares'])
    n=min(len(vs),len(os_))
    for i in range(n):
        v,o=vs[i],os_[i]; pairs+=1
        d=o['hold']-v['hold']
        hold_diff[d]+=1
        if v['exit_date']!=o['exit_date']: exitdate_diff+=1
print(f'\n=== 共同键配对 {pairs} 对 ===')
print(f'hold 差(我方-VERA) 分布:')
for d,n in sorted(hold_diff.items()):
    if n>=3: print(f'  我方-VERA={d}: {n}')
print(f'exit_date 不同: {exitdate_diff}/{pairs}')
