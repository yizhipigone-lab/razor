"""对齐三项后跑stop模式, 逐笔匹配VERA, 定位单笔盈亏差异来源"""
import sys, json, copy, re
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from datetime import date
from collections import defaultdict, Counter
import pandas as pd
from app.backtest.tdx_runner import _run_daily_backtest

bt = json.load(open('output/backtest_results/bt_20260709_000234_1783526554.json', encoding='utf-8'))
params = bt['params']
start = date.fromisoformat(params['start_date']); end = date.fromisoformat(params['end_date'])
params['position_size'] = 20000; params['position_ratio'] = 0.02
params['take_profit_tiers'] = [{'profit_pct':0.03,'sell_ratio':0.2},{'profit_pct':0.13,'sell_ratio':0.6}]
params['realistic_stop_fill'] = 'stop'

df = pd.read_parquet('output/tdx_cache/c52126f5a4f50522.parquet')
sig = {'signals':{}, 'prices':{}}
for code, g in df.groupby('code'):
    g = g.sort_values('date'); dt = g['date'].astype(str).tolist()
    sig['prices'][code] = {'Date':dt,'Close':[float(x) if pd.notna(x) else 0 for x in g['close']],
        'High':[float(x) if pd.notna(x) else 0 for x in g['high']],'Low':[float(x) if pd.notna(x) else 0 for x in g['low']],
        'Open':[float(x) if pd.notna(x) else 0 for x in g['open']]}
    sig['signals'][code] = {'Date':dt,'ZP':[str(int(v)) if pd.notna(v) else '0' for v in g['signal_value']]}

r = _run_daily_backtest(copy.deepcopy(sig), params, start, end, None, None, {})
ours = r['trades']
print(f'我方stop对齐: {len(ours)}笔 收益={r["summary"]["total_return"]}%')

# 各reason平均ret
print('\n=== 我方各退出原因平均ret ===')
by_r = defaultdict(list)
for t in ours: by_r[t['reason']].append(t['ret_pct'])
for k in ['TP1','TP2','TR','HS','TF','FE','TC']:
    if by_r[k]: print(f'  {k}: {len(by_r[k])}笔 平均={sum(by_r[k])/len(by_r[k]):.2f}%')

# VERA各reason平均ret
vera=[]
with open(r'C:\Users\liuziheng\Desktop\1TEST.txt', encoding='utf-8') as f:
    next(f)
    for line in f:
        p=line.rstrip().split('\t')
        if len(p)<12: continue
        vera.append({'code':p[1].split('.')[0],'entry_date':p[3],'entry_px':float(p[4]),
            'exit_date':p[6],'exit_px':float(p[7]),'shares':int(re.match(r'\d+',p[5]).group()),
            'hold':int(p[9]),'pnl':float(p[10].replace('%','')),'reason':p[11]})
print('\n=== VERA各退出原因平均ret ===')
vb = defaultdict(list)
for v in vera: vb[v['reason']].append(v['pnl'])
for k in ['阶梯止盈','移动止盈','成本止损','时间止盈','时间止损']:
    if vb[k]: print(f'  {k}: {len(vb[k])}笔 平均={sum(vb[k])/len(vb[k]):.2f}%')

# 逐笔匹配 (code, entry_date, entry_px)
REASON_MAP={'HS':'成本止损','TP1':'阶梯止盈','TP2':'阶梯止盈','TR':'移动止盈','FE':'时间止盈','TF':'时间止损','TC':'时间止盈'}
def key(x): return (x['code'], x['entry_date'], round(x['entry_px'],3))
vg=defaultdict(list); og=defaultdict(list)
for v in vera: vg[key(v)].append(v)
for o in ours: og[key(o)].append(o)
common=set(vg)&set(og)
print(f'\n=== 逐笔匹配 ===')
print(f'VERA键{len(vg)} 我方键{len(og)} 交集{len(common)}')

pair=0; reason_same=0; px_diff=[]; reason_cross=Counter(); ret_diff_by_reason=Counter()
for k in common:
    vs=sorted(vg[k],key=lambda r:r['shares']); os_=sorted(og[k],key=lambda r:r['shares'])
    for i in range(min(len(vs),len(os_))):
        v,o=vs[i],os_[i]; pair+=1
        vr=v['reason']; oremap=REASON_MAP.get(o['reason'],o['reason'])
        if vr==oremap: reason_same+=1
        else: reason_cross[(oremap,vr)]+=1
        px_diff.append(o['exit_px']-v['exit_px'])
        ret_diff_by_reason[(oremap,vr)]+=o['ret_pct']-v['pnl']
print(f'配对{pair}对, reason一致{reason_same}, 不同{pair-reason_same}')
print(f'exit_px差(我方-VERA): 均值{sum(px_diff)/len(px_diff):.4f} 中位{sorted(px_diff)[len(px_diff)//2]:.4f}')
print(f'\n=== reason交叉分布 (我方, VERA) ===')
for (a,b),n in reason_cross.most_common(10):
    avg_rd=ret_diff_by_reason[(a,b)]/n
    print(f'  我方{a} vs VERA{b}: {n}对 平均ret差(我方-VERA)={avg_rd:+.2f}%')

# reason一致的对里, ret差
same_ret=[]
for k in common:
    vs=sorted(vg[k],key=lambda r:r['shares']); os_=sorted(og[k],key=lambda r:r['shares'])
    for i in range(min(len(vs),len(os_))):
        v,o=vs[i],os_[i]
        if v['reason']==REASON_MAP.get(o['reason'],o['reason']):
            same_ret.append(o['ret_pct']-v['pnl'])
print(f'\n=== reason一致的对 ({len(same_ret)}对) ret差(我方-VERA) ===')
if same_ret:
    print(f'  均值={sum(same_ret)/len(same_ret):+.3f}% 中位={sorted(same_ret)[len(same_ret)//2]:+.3f}%')
    print(f'  我方更高: {sum(1 for x in same_ret if x>0.1)} 我方更低: {sum(1 for x in same_ret if x<-0.1)}')
