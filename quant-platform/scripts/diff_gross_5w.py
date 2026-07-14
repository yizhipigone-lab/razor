"""对齐5万仓位(VERA实际) + stop + 无费用, 逐笔语义配对VERA"""
import sys, json, copy, re
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from datetime import date
from collections import defaultdict, Counter
import pandas as pd
import app.backtest.execution as exe
exe.get_cost_cfg = lambda: {'commission_rate':0.0,'min_commission':0.0,'stamp_tax_rate':0.0,'slippage_rate':0.0}
from app.backtest.tdx_runner import _run_daily_backtest

bt = json.load(open('output/backtest_results/bt_20260709_000234_1783526554.json', encoding='utf-8'))
params = bt['params']
start = date.fromisoformat(params['start_date']); end = date.fromisoformat(params['end_date'])
params['position_size'] = 50000; params['position_ratio'] = 0.05   # 5万对齐VERA
params['take_profit_tiers'] = [{'profit_pct':0.03,'sell_ratio':0.2},{'profit_pct':0.13,'sell_ratio':0.6}]
params['realistic_stop_fill'] = 'stop'

df = pd.read_parquet('output/tdx_cache/3a1b031deae13d5b.parquet')
sig = {'signals':{}, 'prices':{}}
for code, g in df.groupby('code'):
    g = g.sort_values('date'); dt = g['date'].astype(str).tolist()
    sig['prices'][code] = {'Date':dt,'Close':[float(x) if pd.notna(x) else 0 for x in g['close']],
        'High':[float(x) if pd.notna(x) else 0 for x in g['high']],'Low':[float(x) if pd.notna(x) else 0 for x in g['low']],
        'Open':[float(x) if pd.notna(x) else 0 for x in g['open']]}
    sig['signals'][code] = {'Date':dt,'ZP':[str(int(v)) if pd.notna(v) else '0' for v in g['signal_value']]}

r = _run_daily_backtest(copy.deepcopy(sig), params, start, end, None, None, {})
ours = r['trades']
gross = sum(t['ret_pct']/100*t['entry_total'] for t in ours)/1e6*100
print(f'我方(5万+stop+无费): {len(ours)}笔 毛收益={gross:.2f}%')

vera=[]
with open(r'C:\Users\liuziheng\Desktop\1TEST.txt', encoding='utf-8') as f:
    next(f)
    for line in f:
        p=line.rstrip().split('\t')
        if len(p)<12: continue
        vera.append({'code':p[1].split('.')[0],'entry_date':p[3],'entry_px':float(p[4]),
            'exit_date':p[6],'exit_px':float(p[7]),'shares':int(re.match(r'\d+',p[5]).group()),
            'pnl':float(p[10].replace('%','')),'reason':p[11]})
print(f'VERA: {len(vera)}笔 毛收益=47.85%')

def key(x): return (x['code'], x['entry_date'], round(x['entry_px'],3))
def vera_cat(r):
    if r=='成本止损': return 'HS'
    if r=='阶梯止盈': return 'TP'
    if r=='移动止盈': return 'TR'
    return 'TIME'
vg=defaultdict(list); og=defaultdict(list)
for v in vera: vg[key(v)].append(v)
for o in ours: og[key(o)].append(o)
common=set(vg)&set(og)
print(f'\n入场键: VERA{len(vg)} 我方{len(og)} 交集{len(common)}')

# 语义配对
pair=0; exact=0; ret_diff=[]; extra=0
for k in common:
    o_by=defaultdict(list); v_by=defaultdict(list)
    for o in og[k]:
        cat='TP' if o['reason'] in('TP1','TP2') else o['reason']
        o_by[cat].append(o)
    for v in vg[k]: v_by[vera_cat(v['reason'])].append(v)
    for cat in set(list(o_by.keys())+list(v_by.keys())):
        os_=sorted(o_by.get(cat,[]),key=lambda r:r['shares']); vs=sorted(v_by.get(cat,[]),key=lambda r:r['shares'])
        for i in range(min(len(os_),len(vs))):
            o,v=os_[i],vs[i]; pair+=1
            if abs(v['exit_px']-o['exit_px'])<0.01: exact+=1
            else: ret_diff.append((o,v,o['ret_pct']-v['pnl']))
        extra += abs(len(os_)-len(vs))
print(f'\n语义配对: {pair}对')
print(f'  exit_px一致(<0.01): {exact} ({exact/pair*100:.1f}%)')
print(f'  exit不同: {len(ret_diff)}')
if ret_diff:
    import statistics
    print(f'  ret差: 均值{statistics.mean(d[2] for d in ret_diff):+.3f}% 中位{statistics.median(d[2] for d in ret_diff):+.3f}%')
print(f'  未配对零头: {extra}笔')
print(f'\n=== 对比 ===')
print(f'  我方毛: {gross:.2f}%  VERA毛: 47.85%  差: {gross-47.85:+.2f}pct')
