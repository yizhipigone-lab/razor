"""获取沪深A股 申万一级行业映射 + 流通股本"""
import sys, json, time
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from xtquant import xtdata
xtdata.enable_hello=False

hs = json.load(open(r'C:\Users\liuziheng\AppData\Local\Temp\hs_a.json'))
hs_set = set(hs)

# 1. 申万一级行业映射
sl = xtdata.get_sector_list()
sw1 = [s for s in sl if str(s).startswith('SW1') and '加权' not in str(s) and 'HK' not in str(s)]
print(f'申万一级行业板块: {len(sw1)}个')
industry = {}  # code -> 行业
for sec in sw1:
    name = sec.replace('SW1','')
    try:
        codes = xtdata.get_stock_list_in_sector(sec)
    except Exception:
        continue
    for c in codes:
        if c in hs_set and c not in industry:
            industry[c] = name
print(f'行业映射覆盖: {len(industry)}/{len(hs)}只')
no_ind = [c for c in hs if c not in industry]
print(f'无行业: {len(no_ind)}只 样例:{no_ind[:5]}')

# 2. 流通股本
print('获取流通股本...')
t0=time.time()
floatvol = {}
for i,c in enumerate(hs):
    try:
        d = xtdata.get_instrument_detail(c)
        fv = d.get('FloatVolume',0)
        if fv: floatvol[c]=float(fv)
    except Exception:
        pass
    if (i+1)%1000==0:
        print(f'  {i+1}/{len(hs)} ({time.time()-t0:.0f}s)', flush=True)
print(f'流通股本获取: {len(floatvol)}/{len(hs)}只 耗时{time.time()-t0:.0f}s')

json.dump({'industry':industry,'floatvol':floatvol},
          open(r'C:\Users\liuziheng\AppData\Local\Temp\hs_industry_mv.json','w'))
print('已存 hs_industry_mv.json')
