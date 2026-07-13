"""全量对比我们 vs VERA 的 TDX 回测交易（按 code+入场日 分组配对）
我们: output/backtest_results/bt_20260707_062557_1783376757.json
VERA: 桌面 TEST.txt
"""
import json
from collections import defaultdict

ROOT = 'e:/1target/p9_project/quant-platform'
with open(f'{ROOT}/output/backtest_results/bt_20260707_062557_1783376757.json','r',encoding='utf-8') as f:
    d = json.load(f)
ours = d.get('trades',[])

vera = []
with open(r'C:/Users/liuziheng/Desktop/TEST.txt','r',encoding='utf-8') as f:
    next(f)
    for line in f:
        parts = line.strip().split('\t')
        if len(parts) < 11: continue
        code = parts[1].split('.')[0]
        ed = parts[3]; ep = float(parts[4])
        xd = parts[6]; xp = float(parts[7])
        qty = int(parts[5].replace(' 股','').replace('股','').strip())
        ret = float(parts[10].replace('%',''))
        reason = parts[11]
        vera.append({'code':code,'entry_date':ed,'exit_date':xd,'entry_px':ep,'exit_px':xp,'shares':qty,'ret_pct':ret,'reason':reason})

def group(trades):
    g = defaultdict(list)
    for t in trades:
        g[(t['code'],t['entry_date'])].append(t)
    return g

og = group(ours); vg = group(vera)
all_keys = set(og) | set(vg)

def agg(lst):
    cost = sum(t['entry_px']*t['shares'] for t in lst)
    rev = sum(t['exit_px']*t['shares'] for t in lst)
    return len(lst), sum(t['shares'] for t in lst), cost, rev, (rev/cost-1)*100 if cost else 0

matched = []
for k in all_keys:
    o = og.get(k, []); v = vg.get(k, [])
    if o and v:
        ob, osh, oc, orr, oret = agg(o)
        vb, vsh, vc, vrr, vret = agg(v)
        ods = sorted(set(t['exit_date'] for t in o))
        vds = sorted(set(t['exit_date'] for t in v))
        matched.append({
            'code':k[0],'entry_date':k[1],
            'o_batches':ob,'o_shares':osh,'o_ret':oret,
            'v_batches':vb,'v_shares':vsh,'v_ret':vret,
            'ret_diff': oret - vret,
            'o_exitdays':ods,'v_exitdays':vds,
            'o_trades':o,'v_trades':v,
        })

only_o = [k for k in og if k not in vg]
only_v = [k for k in vg if k not in og]

print('='*60)
print('全量配对统计')
print('='*60)
print(f'我们总笔数: {len(ours)}, VERA总笔数: {len(vera)}')
print(f'匹配组合数(同code同入场日): {len(matched)}')
print(f'仅我们有: {len(only_o)} 组, 仅VERA有: {len(only_v)} 组')

we_win = sum(1 for r in matched if r['ret_diff']>0.1)
vera_win = sum(1 for r in matched if r['ret_diff']<-0.1)
tie = len(matched) - we_win - vera_win
print(f'\n按组合收益率差(我们-VERA):')
print(f'  我们更高: {we_win} 组')
print(f'  VERA更高: {vera_win} 组')
print(f'  持平(<0.1%): {tie} 组')

more_b_o = sum(1 for r in matched if r['o_batches']>r['v_batches'])
more_b_v = sum(1 for r in matched if r['o_batches']<r['v_batches'])
same_b = sum(1 for r in matched if r['o_batches']==r['v_batches'])
print(f'\n批次构成(分批卖出笔数):')
print(f'  我们批次数更多(分更细/慢卖): {more_b_o} 组')
print(f'  VERA批次数更多: {more_b_v} 组')
print(f'  批次数相同: {same_b} 组')

diff_exit = sum(1 for r in matched if r['o_exitdays']!=r['v_exitdays'])
print(f'\n退出日集合不同: {diff_exit} 组 / {len(matched)} 组')

# 收益率差加权（按成本权重算总差异贡献）
tot_ocost = sum(r['o_trades'][0]['entry_px']*sum(t['shares'] for t in r['o_trades']) for r in matched)
# 简单：按各组收益率差*组成本占比加权
total_o_cost = sum(agg(r['o_trades'])[2] for r in matched)
total_v_cost = sum(agg(r['v_trades'])[2] for r in matched)
print(f'\n匹配组总成本: 我们 {total_o_cost:.0f}, VERA {total_v_cost:.0f}')

# 我们赢的组总多赚 vs VERA赢的组总少赚
we_win_groups = [r for r in matched if r['ret_diff']>0.1]
vera_win_groups = [r for r in matched if r['ret_diff']<-0.1]
we_avg = sum(r['ret_diff'] for r in we_win_groups)/len(we_win_groups) if we_win_groups else 0
vera_avg = abs(sum(r['ret_diff'] for r in vera_win_groups)/len(vera_win_groups)) if vera_win_groups else 0
print(f'\n我们赢的 {len(we_win_groups)} 组, 平均多赚 {we_avg:.2f}%/组')
print(f'VERA赢的 {len(vera_win_groups)} 组, 平均多赚 {vera_avg:.2f}%/组')

print('\n'+'='*60)
print('我们收益率比VERA高最多的 25 组')
print('='*60)
for r in sorted(matched, key=lambda x:-x['ret_diff'])[:25]:
    print(f"  {r['code']} {r['entry_date']}: 我们{r['o_ret']:+.1f}%({r['o_batches']}批/{r['o_shares']}股 退出{r['o_exitdays']}) VERA{r['v_ret']:+.1f}%({r['v_batches']}批/{r['v_shares']}股 退出{r['v_exitdays']}) 差={r['ret_diff']:+.1f}%")

print('\n'+'='*60)
print('VERA收益率比我们高最多的 25 组')
print('='*60)
for r in sorted(matched, key=lambda x:x['ret_diff'])[:25]:
    print(f"  {r['code']} {r['entry_date']}: 我们{r['o_ret']:+.1f}%({r['o_batches']}批 退出{r['o_exitdays']}) VERA{r['v_ret']:+.1f}%({r['v_batches']}批 退出{r['v_exitdays']}) 差={r['ret_diff']:+.1f}%")

# 总利润
our_profit = sum(t.get('profit',0) for t in ours)
vera_profit = sum((t['exit_px']-t['entry_px'])*t['shares'] for t in vera)
print('\n'+'='*60)
print('总利润对比 (本金均100万)')
print('='*60)
print(f'我们总利润(含佣金印花滑点): {our_profit:.0f}  收益率 {our_profit/1000000*100:.2f}%')
print(f'VERA总利润(不含费): {vera_profit:.0f}  收益率 {vera_profit/1000000*100:.2f}%')
