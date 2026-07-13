"""按reason语义重新配对, 验证468对交叉是否配对错位"""
import json, re
from collections import defaultdict, Counter
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
REMAP={'HS':'成本止损','TP1':'阶梯止盈','TP2':'阶梯止盈','TR':'移动止盈','FE':'时间','TF':'时间','TC':'时间'}
# 反向: VERA中文 -> 我方类别
def vera_cat(r):
    if r=='成本止损': return 'HS'
    if r=='阶梯止盈': return 'TP'
    if r=='移动止盈': return 'TR'
    return 'TIME'

vg=defaultdict(list); og=defaultdict(list)
for v in vera: vg[key(v)].append(v)
for o in ours: og[key(o)].append(o)

common=set(vg)&set(og)
# 语义配对: 同类别按shares配对
pair_ok=0; pair_reason_mismatch=0; exact_after=0; ret_diff_ok=[]
mismatch_cases=[]
for k in common:
    # 按类别分组
    o_by_cat=defaultdict(list); v_by_cat=defaultdict(list)
    for o in og[k]:
        cat = 'TP' if o['reason'] in ('TP1','TP2') else o['reason']
        o_by_cat[cat].append(o)
    for v in vg[k]:
        v_by_cat[vera_cat(v['reason'])].append(v)
    # 每个类别配对
    for cat in set(list(o_by_cat.keys())+list(v_by_cat.keys())):
        os_ = sorted(o_by_cat.get(cat,[]), key=lambda r:r['shares'])
        vs = sorted(v_by_cat.get(cat,[]), key=lambda r:r['shares'])
        for i in range(min(len(os_),len(vs))):
            o,v=os_[i],vs[i]; pair_ok+=1
            px_close=abs(v['exit_px']-o['exit_px'])<0.01
            if px_close: exact_after+=1
            else: ret_diff_ok.append(o['ret_pct']-v['pnl'])
        # 多出的笔
        if len(os_)>len(vs):
            for o in os_[len(vs):]: mismatch_cases.append((k,'我方多',o['reason'],o['shares'],o['exit_px']))
        if len(vs)>len(os_):
            for v in vs[len(os_):]: mismatch_cases.append((k,'VERA多',v['reason'],v['shares'],v['exit_px']))

print(f'语义配对总数: {pair_ok}')
print(f'exit_px一致(<0.01): {exact_after} ({exact_after/pair_ok*100:.1f}%)')
if ret_diff_ok:
    import statistics
    print(f'exit_px不同的ret差: 均值{statistics.mean(ret_diff_ok):+.3f}% 中位{statistics.median(ret_diff_ok):+.3f}% n={len(ret_diff_ok)}')
print(f'\n多出笔数(零头/未配对): {len(mismatch_cases)}')
mc=Counter((m[1],m[2]) for m in mismatch_cases)
for (side,r),n in mc.most_common(8):
    print(f'  {side} {r}: {n}笔')
# 多出笔的shares分布(看是否零头)
print(f'\n多出笔shares分布:')
sh=Counter()
for m in mismatch_cases:
    sh[m[3]]+=1
for s,n in sorted(sh.items())[:15]:
    print(f'  shares={s}: {n}笔')
