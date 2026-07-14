"""毛收益口径(关闭费用)跑stop+2万+3%+纯TDX, 逐笔比对VERA 1TEST"""
import sys, json, copy, re
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from datetime import date
from collections import defaultdict, Counter
import pandas as pd

# 关闭费用: monkeypatch get_cost_cfg 返回0费率
import app.backtest.execution as exe
exe.get_cost_cfg = lambda: {'commission_rate':0.0,'min_commission':0.0,'stamp_tax_rate':0.0,'slippage_rate':0.0}

from app.backtest.tdx_runner import _run_daily_backtest

bt = json.load(open('output/backtest_results/bt_20260709_000234_1783526554.json', encoding='utf-8'))
params = bt['params']
start = date.fromisoformat(params['start_date']); end = date.fromisoformat(params['end_date'])
params['position_size'] = 20000; params['position_ratio'] = 0.02
params['take_profit_tiers'] = [{'profit_pct':0.03,'sell_ratio':0.2},{'profit_pct':0.13,'sell_ratio':0.6}]
params['realistic_stop_fill'] = 'stop'

# 新TDX缓存
df = pd.read_parquet('output/tdx_cache/3a1b031deae13d5b.parquet')
sig = {'signals':{}, 'prices':{}}
for code, g in df.groupby('code'):
    g = g.sort_values('date'); dt = g['date'].astype(str).tolist()
    sig['prices'][code] = {'Date':dt,'Close':[float(x) if pd.notna(x) else 0 for x in g['close']],
        'High':[float(x) if pd.notna(x) else 0 for x in g['high']],'Low':[float(x) if pd.notna(x) else 0 for x in g['low']],
        'Open':[float(x) if pd.notna(x) else 0 for x in g['open']]}
    sig['signals'][code] = {'Date':dt,'ZP':[str(int(v)) if pd.notna(v) else '0' for v in g['signal_value']]}

print('跑stop+2万+3%+纯TDX+无费用...')
r = _run_daily_backtest(copy.deepcopy(sig), params, start, end, None, None, {})
ours = r['trades']
s = r['summary']
gross = sum(t['ret_pct']/100*t['entry_total'] for t in ours)/1e6*100
print(f'我方: {len(ours)}笔 净收益={s["total_return"]}% 毛收益={gross:.2f}%')

# 解析VERA
vera=[]
with open(r'C:\Users\liuziheng\Desktop\1TEST.txt', encoding='utf-8') as f:
    next(f)
    for line in f:
        p=line.rstrip().split('\t')
        if len(p)<12: continue
        vera.append({'code':p[1].split('.')[0],'entry_date':p[3],'entry_px':float(p[4]),
            'exit_date':p[6],'exit_px':float(p[7]),'shares':int(re.match(r'\d+',p[5]).group()),
            'hold':int(p[9]),'pnl':float(p[10].replace('%','')),'reason':p[11]})
print(f'VERA: {len(vera)}笔')

# 逐笔匹配 (code, entry_date, entry_px)
def key(x): return (x['code'], x['entry_date'], round(x['entry_px'],3))
vg=defaultdict(list); og=defaultdict(list)
for v in vera: vg[key(v)].append(v)
for o in ours: og[key(o)].append(o)
vkeys=set(vg); okeys=set(og)
print(f'\n=== 入场键匹配 ===')
print(f'VERA键{len(vkeys)} 我方键{len(okeys)} 交集{len(vkeys&okeys)} VERA独有{len(vkeys-okeys)} 我方独有{len(okeys-vkeys)}')

# 交集内配对
common=vkeys&okeys
pair=0; exact=0; reason_same=0; px_diff=[]; reason_cross=Counter(); ret_diff_same=[]
for k in common:
    vs=sorted(vg[k],key=lambda r:r['shares']); os_=sorted(og[k],key=lambda r:r['shares'])
    for i in range(min(len(vs),len(os_))):
        v,o=vs[i],os_[i]; pair+=1
        REMAP={'HS':'成本止损','TP1':'阶梯止盈','TP2':'阶梯止盈','TR':'移动止盈','FE':'时间止盈','TF':'时间止损','TC':'时间止盈'}
        oremap=REMAP.get(o['reason'],o['reason'])
        px_close=abs(v['exit_px']-o['exit_px'])<0.005
        rs=(v['reason']==oremap)
        if px_close and rs: exact+=1
        if rs: reason_same+=1; ret_diff_same.append(o['ret_pct']-v['pnl'])
        else: reason_cross[(oremap,v['reason'])]+=1
        if not px_close: px_diff.append(o['exit_px']-v['exit_px'])

print(f'\n=== 配对结果 (共{pair}对) ===')
print(f'完全一致(exit_px+reason): {exact}')
print(f'reason一致: {reason_same}/{pair}')
print(f'exit_px不同: {len(px_diff)}')
if px_diff:
    import statistics
    print(f'  exit_px差(我方-VERA): 均值{statistics.mean(px_diff):.4f} 中位{statistics.median(px_diff):.4f}')
    print(f'  我方更高{sum(1 for d in px_diff if d>0.005)} 更低{sum(1 for d in px_diff if d<-0.005)}')
if reason_cross:
    print(f'\n=== reason交叉(我方,VERA) top10 ===')
    for (a,b),n in reason_cross.most_common(10):
        print(f'  我方{a} vs VERA{b}: {n}对')
if ret_diff_same:
    import statistics
    print(f'\n=== reason一致对的ret差(我方毛-VERA毛) ===')
    print(f'  均值={statistics.mean(ret_diff_same):+.3f}% 中位={statistics.median(ret_diff_same):+.3f}% n={len(ret_diff_same)}')
    print(f'  我方更高{sum(1 for d in ret_diff_same if d>0.1)} 更低{sum(1 for d in ret_diff_same if d<-0.1)}')

# 保存我方trades供后续
with open('output/ours_gross_stop.json','w',encoding='utf-8') as f:
    json.dump([{'code':t['code'],'entry_date':t['entry_date'],'entry_px':t['entry_px'],
        'exit_date':t['exit_date'],'exit_px':t['exit_px'],'shares':t['shares'],
        'ret_pct':t['ret_pct'],'reason':t['reason'],'hold':t['hold_days']} for t in ours], f, ensure_ascii=False)
print(f'\n我方trades已存 output/ours_gross_stop.json')
