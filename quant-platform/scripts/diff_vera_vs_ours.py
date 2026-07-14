"""全量逐笔对比：我方回测 vs VERA 1TEST.txt
遍历不抽检。按 (code6, entry_date, entry_px) 对齐，输出差异分类。
"""
import json, re, sys
from collections import defaultdict, Counter

VERA_PATH = r'C:\Users\liuziheng\Desktop\1TEST.txt'
OURS_PATH = 'output/backtest_results/bt_20260709_000234_1783526554.json'

# ---- 解析 VERA ----
def parse_vera():
    rows = []
    with open(VERA_PATH, encoding='utf-8') as f:
        next(f)  # skip header
        for line in f:
            line = line.rstrip('\n')
            if not line.strip():
                continue
            parts = line.split('\t')
            if len(parts) < 12:
                continue
            seq, code, name, entry_date, entry_px, entry_qty, exit_date, exit_px, exit_qty, hold, pnl, reason = parts[:12]
            code6 = code.split('.')[0]
            m = re.match(r'(\d+)', entry_qty)
            shares = int(m.group(1)) if m else 0
            rows.append({
                'code': code6, 'name': name, 'entry_date': entry_date,
                'entry_px': float(entry_px), 'exit_date': exit_date,
                'exit_px': float(exit_px), 'shares': shares,
                'hold': int(hold), 'pnl_pct': float(pnl.replace('%','')),
                'reason': reason,
            })
    return rows

# ---- 解析我方 ----
def parse_ours():
    d = json.load(open(OURS_PATH, encoding='utf-8'))
    out = []
    for t in d['trades']:
        out.append({
            'code': t['code'], 'name': t.get('name',''), 'entry_date': t['entry_date'],
            'entry_px': t['entry_px'], 'exit_date': t['exit_date'],
            'exit_px': t['exit_px'], 'shares': t['shares'],
            'hold': t['hold_days'], 'pnl_pct': t['ret_pct'],
            'reason': t['reason'], 'profit': t.get('profit',0),
        })
    return out

REASON_MAP = {'HS':'成本止损','TP1':'阶梯止盈','TP2':'阶梯止盈','TR':'移动止盈',
              'FE':'首日弱势','TF':'强制时间','TC':'时间条件'}

vera = parse_vera()
ours = parse_ours()

print(f'VERA: {len(vera)} 笔  日期 {min(r["entry_date"] for r in vera)} ~ {max(r["entry_date"] for r in vera)}')
print(f'我方: {len(ours)} 笔  日期 {min(r["entry_date"] for r in ours)} ~ {max(r["entry_date"] for r in ours)}')
print()
print('=== VERA 退出原因分布 ===')
for k,v in Counter(r['reason'] for r in vera).most_common():
    print(f'  {k}: {v}')
print('=== 我方退出原因分布 ===')
for k,v in Counter(r['reason'] for r in ours).most_common():
    print(f'  {k}: {v}  -> VERA语义:{REASON_MAP.get(k,k)}')
print()

# ---- 按 (code, entry_date, entry_px) 分组 ----
def key(r):
    return (r['code'], r['entry_date'], round(r['entry_px'],3))

vg = defaultdict(list); og = defaultdict(list)
for r in vera: vg[key(r)].append(r)
for r in ours: og[key(r)].append(r)

vkeys = set(vg); okeys = set(og)
print(f'=== 唯一入场键 (code,entry_date,entry_px) ===')
print(f'  VERA: {len(vkeys)}  我方: {len(okeys)}  交集: {len(vkeys&okeys)}  VERA独有: {len(vkeys-okeys)}  我方独有: {len(okeys-vkeys)}')
print()

# ---- 交集键内逐笔对齐 ----
common = vkeys & okeys
# 对每个共同键，按 shares 升序配对（VERA 与我方同一键下可能笔数不同）
mismatch_reason = Counter()
mismatch_px = []
mismatch_hold = []
match_exact = 0
match_reason_only = 0
pair_count = 0
reason_when_px_same = Counter()
for k in common:
    vs = sorted(vg[k], key=lambda r: r['shares'])
    os_ = sorted(og[k], key=lambda r: r['shares'])
    n = min(len(vs), len(os_))
    for i in range(n):
        v, o = vs[i], os_[i]
        pair_count += 1
        vreason = v['reason']
        oreason_mapped = REASON_MAP.get(o['reason'], o['reason'])
        px_close = abs(v['exit_px'] - o['exit_px']) < 0.005
        reason_same = (vreason == oreason_mapped)
        if px_close and reason_same:
            match_exact += 1
        elif px_close and not reason_same:
            match_reason_only += 1
            reason_when_px_same[(oreason_mapped, vreason)] += 1
        else:
            mismatch_px.append((k, v, o, reason_same))
            if not reason_same:
                mismatch_reason[(oreason_mapped, vreason)] += 1
            if v['hold'] != o['hold']:
                mismatch_hold.append((k, v['hold'], o['hold']))

print(f'=== 共同键内配对 (按shares升序) 共 {pair_count} 对 ===')
print(f'  完全一致(exit_px+reason): {match_exact}')
print(f'  仅reason不同(px相同): {match_reason_only}')
print(f'  exit_px不同: {len(mismatch_px)}')
print()
if reason_when_px_same:
    print('=== px相同但reason不同 的分布 (我方, VERA) ===')
    for (a,b),n in reason_when_px_same.most_common():
        print(f'  我方{a} vs VERA{b}: {n}')
print()
if mismatch_reason:
    print('=== exit_px不同时 reason分布 (我方, VERA) ===')
    for (a,b),n in mismatch_reason.most_common(20):
        print(f'  我方{a} vs VERA{b}: {n}')

# exit_px 差异统计
if mismatch_px:
    diffs = []
    for k,v,o,_ in mismatch_px:
        d = o['exit_px'] - v['exit_px']
        diffs.append(d)
    import statistics
    print(f'\n=== exit_px 差异 (我方-VERA) 共{len(diffs)} ===')
    print(f'  均值: {statistics.mean(diffs):.4f}  中位: {statistics.median(diffs):.4f}')
    print(f'  我方更高: {sum(1 for d in diffs if d>0.005)}  我方更低: {sum(1 for d in diffs if d<-0.005)}')
    # 按 reason 拆分
    by_reason = defaultdict(list)
    for k,v,o,_ in mismatch_px:
        by_reason[(REASON_MAP.get(o['reason'],o['reason']), v['reason'])].append(o['exit_px']-v['exit_px'])
    print('  按reason组合:')
    for (or_,vr),ds in sorted(by_reason.items(), key=lambda x:-len(x[1])):
        print(f'    我方{or_} vs VERA{vr}: n={len(ds)} 均值diff={statistics.mean(ds):.4f}')

# 共同键但 VERA 笔数 > 我方（VERA多买了）
extra_vera = []
for k in common:
    if len(vg[k]) > len(og[k]):
        extra_vera.append((k, len(vg[k]), len(og[k])))
print(f'\n=== 共同入场键中 VERA笔数>我方 的键: {len(extra_vera)} ===')
for (k,nv,no) in extra_vera[:15]:
    print(f'  {k}: VERA {nv}笔 vs 我方 {no}笔')

# 股数对比：同一键同一shares档位
print(f'\n=== 持仓股数差异 (共同键内配对) ===')
sh_diff = 0
for k in common:
    vs = sorted(vg[k], key=lambda r: r['shares'])
    os_ = sorted(og[k], key=lambda r: r['shares'])
    n = min(len(vs), len(os_))
    for i in range(n):
        if vs[i]['shares'] != os_[i]['shares']:
            sh_diff += 1
print(f'  配对中股数不同: {sh_diff}/{pair_count}')

# 写出 mismatch_px 详细到文件供分析
with open('output/diff_mismatch_px.csv','w',encoding='utf-8') as f:
    f.write('code,entry_date,entry_px,vera_shares,vera_exit_date,vera_exit_px,vera_pnl,vera_reason,vera_hold,our_shares,our_exit_date,our_exit_px,our_pnl,our_reason,our_hold\n')
    for k,v,o,_ in mismatch_px:
        f.write(f"{k[0]},{k[1]},{k[2]},{v['shares']},{v['exit_date']},{v['exit_px']},{v['pnl_pct']},{v['reason']},{v['hold']},{o['shares']},{o['exit_date']},{o['exit_px']},{o['pnl_pct']},{o['reason']},{o['hold']}\n")
print(f'\n详细 exit_px 差异已写入 output/diff_mismatch_px.csv')
