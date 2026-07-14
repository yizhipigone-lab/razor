"""三维度分析: 市值区间/年份×行业/股价区间 盈利率"""
import json, os
from collections import defaultdict

# 读回测结果(最新hmqb_hs_*.json) + 行业市值
files = sorted([f for f in os.listdir('output/backtest_results') if f.startswith('hmqb_hs_') and f.endswith('.json')])
bt = json.load(open(f'output/backtest_results/{files[-1]}', encoding='utf-8'))
imv = json.load(open(r'C:\Users\liuziheng\AppData\Local\Temp\hs_industry_mv.json', encoding='utf-8'))
# trades code无后缀(600004), 行业/股本key有后缀(600004.SH), 统一去后缀
industry = {k.split('.')[0]: v for k, v in imv['industry'].items()}
floatvol = {k.split('.')[0]: v for k, v in imv['floatvol'].items()}
trades = bt['trades']
print(f'交易{len(trades)}笔, 行业覆盖{len(industry)}只')

# 算每笔市值(亿) = entry_px × FloatVolume / 1e8
def mv_yi(t):
    fv = floatvol.get(t['code'], 0)
    return t['entry_px'] * fv / 1e8 if fv else None

def wr(items):
    if not items: return 0,0
    wins = sum(1 for t in items if t['ret_pct']>0)
    return wins/len(items)*100, len(items)
def avg_ret(items):
    if not items: return 0
    return sum(t['ret_pct'] for t in items)/len(items)

# === 1. 市值区间 ===
mv_buckets = {'小(<100亿)':[0,100],'中(100-500亿)':[100,500],'大(>500亿)':[500,99999]}
mv_stat = {k:[] for k in mv_buckets}
no_mv = []
for t in trades:
    m = mv_yi(t)
    if m is None: no_mv.append(t); continue
    for k,(lo,hi) in mv_buckets.items():
        if lo<=m<hi: mv_stat[k].append(t); break
print('\n=== 1. 市值区间盈利率 ===')
for k in mv_buckets:
    w,n = wr(mv_stat[k]); a = avg_ret(mv_stat[k])
    print(f'  {k}: {n}笔 胜率{w:.1f}% 平均ret{a:+.2f}%')

# === 2. 年份×行业 ===
yr_ind = defaultdict(lambda: defaultdict(list))
for t in trades:
    y = t['entry_date'][:4]
    ind = industry.get(t['code'],'未分类')
    yr_ind[y][ind].append(t)
print('\n=== 2. 各年盈利率最高行业 ===')
for y in sorted(yr_ind):
    inds = yr_ind[y]
    # 只看≥30笔的行业
    ranked = [(name, wr(ts)[0], len(ts), avg_ret(ts)) for name,ts in inds.items() if len(ts)>=30]
    ranked.sort(key=lambda x:-x[1])
    top3 = ranked[:3]
    line = f'  {y}: '
    for name,w,n,a in top3:
        line += f'{name}({w:.0f}%/{n}笔,{a:+.1f}%) '
    print(line)

# === 3. 股价区间 ===
px_buckets = {'低价(<10元)':[0,10],'中价(10-30)':[10,30],'中高(30-100)':[30,100],'高价(>100)':[100,99999]}
px_stat = {k:[] for k in px_buckets}
for t in trades:
    p = t['entry_px']
    for k,(lo,hi) in px_buckets.items():
        if lo<=p<hi: px_stat[k].append(t); break
print('\n=== 3. 股价区间盈利率 ===')
for k in px_buckets:
    w,n = wr(px_stat[k]); a = avg_ret(px_stat[k])
    print(f'  {k}: {n}笔 胜率{w:.1f}% 平均ret{a:+.2f}%')

print(f'\n无市值数据: {len(no_mv)}笔')
