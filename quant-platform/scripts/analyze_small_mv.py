"""40亿以下重点分析(真实回测trades)"""
import json, os
from collections import defaultdict, Counter

files = sorted([f for f in os.listdir('output/backtest_results') if f.startswith('hmqb_hs_') and f.endswith('.json')])
bt = json.load(open(f'output/backtest_results/{files[-1]}', encoding='utf-8'))
imv = json.load(open(r'C:\Users\liuziheng\AppData\Local\Temp\hs_industry_mv.json', encoding='utf-8'))
industry = {k.split('.')[0]: v for k, v in imv['industry'].items()}
floatvol = {k.split('.')[0]: v for k, v in imv['floatvol'].items()}

results = []
for t in bt['trades']:
    code = t['code']
    fv = floatvol.get(code, 0)
    mv = t['entry_px'] * fv / 1e8 if fv else None
    results.append({'code': code, 'entry': t['entry_px'], 'ret': t['ret_pct'],
        'reason': t['reason'], 'industry': industry.get(code, '未分类'),
        'mv': mv, 'year': t['entry_date'][:4]})

small = [x for x in results if x['mv'] and x['mv'] < 40]
all40 = [x for x in results if x['mv'] and x['mv'] >= 40]
print(f'总{len(results)}笔 | 40亿以下{len(small)}笔({len(small)/len(results)*100:.0f}%) | 40亿以上{len(all40)}笔')

def wr(items):
    return (sum(1 for x in items if x['ret']>0)/len(items)*100, len(items)) if items else (0,0)
def avgr(items):
    return sum(x['ret'] for x in items)/len(items) if items else 0

print('\n=== 1. 细市值(40亿以下) ===')
for k,(lo,hi) in {'<10亿':[0,10],'10-20亿':[10,20],'20-30亿':[20,30],'30-40亿':[30,40]}.items():
    its=[x for x in small if lo<=x['mv']<hi]
    w,n=wr(its)
    print(f'  {k}: {n}笔 胜率{w:.1f}% 平均ret{avgr(its):+.2f}%')

print('\n=== 2. 40亿以下 行业TOP10(≥50笔) ===')
ind=defaultdict(list)
for x in small: ind[x['industry']].append(x)
rk=[(name,*wr(ts),avgr(ts)) for name,ts in ind.items() if len(ts)>=50]
rk.sort(key=lambda z:-z[1])
for name,w,n,a in rk[:10]: print(f'  {name}: {n}笔 胜率{w:.1f}% 平均ret{a:+.2f}%')
print('  --- BOTTOM5 ---')
for name,w,n,a in sorted(rk,key=lambda z:z[1])[:5]: print(f'  {name}: {n}笔 胜率{w:.1f}% 平均ret{a:+.2f}%')

print('\n=== 3. 40亿以下 股价区间 ===')
for k,(lo,hi) in {'<5元':[0,5],'5-10':[5,10],'10-20':[10,20],'20-40':[20,40],'>40':[40,99999]}.items():
    its=[x for x in small if lo<=x['entry']<hi]
    w,n=wr(its)
    print(f'  {k}: {n}笔 胜率{w:.1f}% 平均ret{avgr(its):+.2f}%')

print('\n=== 4. 40亿以下 各年 ===')
yr=defaultdict(list)
for x in small: yr[x['year']].append(x)
for y in sorted(yr):
    w,n=wr(yr[y])
    print(f'  {y}: {n}笔 胜率{w:.1f}% 平均ret{avgr(yr[y]):+.2f}%')

print('\n=== 5. 40亿以下 退出原因 ===')
REASON_CN={'HS':'成本止损','TP1':'阶梯3%','TP2':'阶梯13%','TR':'移动止盈','TC':'时间条件','TF':'强制时间','FE':'期末'}
for r,n in Counter(x['reason'] for x in small).most_common():
    its=[x for x in small if x['reason']==r]
    print(f'  {REASON_CN.get(r,r)}: {n}笔({n/len(small)*100:.0f}%) 胜率{wr(its)[0]:.0f}% 平均ret{avgr(its):+.2f}%')

# 存40亿以下结果供MD用
json.dump({'small':small,'all':results,'small40':small},
    open(r'C:\Users\liuziheng\AppData\Local\Temp\small40_real.json','w'))
