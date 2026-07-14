"""全量逐笔对比: 我们min(真实成交价) vs VERA + 验证VERA费用拆解"""
import sys, json, copy
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
import pandas as pd
from datetime import date
from collections import defaultdict
from app.tqsdk import result_cache
from app.backtest.tdx_runner import _run_daily_backtest

# 1. 我们 min trades
df = pd.read_parquet('output/tdx_cache/c52126f5a4f50522.parquet')
sig, pri = result_cache.df_to_signals_prices(df)
sig_result = {'status':'ok','signals':sig,'prices':pri}
with open('output/backtest_results/bt_20260707_062557_1783376757.json','r',encoding='utf-8') as f:
    bt = json.load(f)
params = bt['params']
params['realistic_stop_fill'] = True
start = date.fromisoformat(params['start_date']); end = date.fromisoformat(params['end_date'])
r = _run_daily_backtest(copy.deepcopy(sig_result), params, start, end, None, None, {})
ours = r['trades']
print(f'我们min: 收益={r["summary"]["total_return"]}% 回撤={r["summary"]["max_drawdown"]}% 胜率={r["summary"]["win_rate"]}% 交易={len(ours)}笔')

# 2. VERA trades
vera = []
with open(r'C:/Users/liuziheng/Desktop/TEST.txt','r',encoding='utf-8') as f:
    next(f)
    for line in f:
        parts = line.strip().split('\t')
        if len(parts)<11: continue
        code = parts[1].split('.')[0]
        ed=parts[3]; ep=float(parts[4]); xd=parts[6]; xp=float(parts[7])
        qty=int(parts[5].replace(' 股','').strip())
        ret=float(parts[10].replace('%',''))
        reason=parts[11]
        vera.append({'code':code,'entry_date':ed,'exit_date':xd,'entry_px':ep,'exit_px':xp,'shares':qty,'ret_pct':ret,'reason':reason})
print(f'VERA: 交易={len(vera)}笔')

# 3. 按 (code, entry_date) 分组
def group(ts):
    g=defaultdict(list)
    for t in ts: g[(t['code'],t['entry_date'])].append(t)
    return g
og=group(ours); vg=group(vera)

def agg(lst):
    cost=sum(t['entry_px']*t['shares'] for t in lst)
    rev=sum(t['exit_px']*t['shares'] for t in lst)
    return (rev/cost-1)*100 if cost else 0, len(lst), sum(t['shares'] for t in lst), cost, rev

matched=[]
for k in set(og)&set(vg):
    oret,ob,osh,oc,orr=agg(og[k]); vret,vb,vsh,vc,vrr=agg(vg[k])
    ods=sorted(set(t['exit_date'] for t in og[k]))
    vds=sorted(set(t['exit_date'] for t in vg[k]))
    matched.append({'code':k[0],'entry_date':k[1],'oret':oret,'vret':vret,'diff':oret-vret,
                    'ob':ob,'vb':vb,'o_shares':osh,'v_shares':vsh,
                    'o_cost':oc,'v_cost':vc,'o_exitdays':ods,'v_exitdays':vds,
                    'o_trades':og[k],'v_trades':vg[k]})

print(f'\n匹配组: {len(matched)} (同code同入场日)')
print(f'仅我们: {len(set(og)-set(vg))}, 仅VERA: {len(set(vg)-set(og))}')
we_win=sum(1 for m in matched if m['diff']>0.1)
vera_win=sum(1 for m in matched if m['diff']<-0.1)
tie=len(matched)-we_win-vera_win
print(f'我们min收益率更高: {we_win}组, VERA更高: {vera_win}组, 持平: {tie}组')

# 加权总收益差（按我们成本占比）
tot_o_cost=sum(m['o_cost'] for m in matched)
tot_v_cost=sum(m['v_cost'] for m in matched)
o_profit=sum(m['o_cost']*m['oret']/100 for m in matched)
v_profit=sum(m['v_cost']*m['vret']/100 for m in matched)
print(f'\n匹配组总成本: 我们{tot_o_cost:.0f} VERA{tot_v_cost:.0f}')
print(f'匹配组总利润: 我们{o_profit:.0f}({o_profit/tot_o_cost*100:.2f}%) VERA{v_profit:.0f}({v_profit/tot_v_cost*100:.2f}%)')

print('\n=== 我们min比VERA高最多的18组 ===')
for m in sorted(matched,key=lambda x:-x['diff'])[:18]:
    print(f"  {m['code']} {m['entry_date']}: 我们{m['oret']:+.1f}%({m['ob']}批/{m['o_shares']}股 退出{m['o_exitdays']}) VERA{m['vret']:+.1f}%({m['vb']}批/{m['v_shares']}股 退出{m['v_exitdays']}) 差={m['diff']:+.1f}%")

print('\n=== VERA比我们min高最多的18组 ===')
for m in sorted(matched,key=lambda x:x['diff'])[:18]:
    print(f"  {m['code']} {m['entry_date']}: 我们{m['oret']:+.1f}%({m['ob']}批/{m['o_shares']}股 退出{m['o_exitdays']}) VERA{m['vret']:+.1f}%({m['vb']}批/{m['v_shares']}股 退出{m['v_exitdays']}) 差={m['diff']:+.1f}%")

# 4. VERA 费用拆解验证
print('\n'+'='*50)
print('VERA 费用拆解验证')
print('='*50)
entry_amt=sum(t['entry_px']*t['shares'] for t in vera)
exit_amt=sum(t['exit_px']*t['shares'] for t in vera)
gross=exit_amt-entry_amt
buy_fee=entry_amt*0.0013
sell_fee=exit_amt*0.0018
net=gross-buy_fee-sell_fee
print(f'entry_amount总和: {entry_amt:.0f} (报告称27,130,452)')
print(f'exit_amount总和: {exit_amt:.0f}')
print(f'毛收益(exit-entry): {gross:.2f} (报告称102,164.23)')
print(f'买入费(0.13%=佣金0.03%+滑点0.10%): {buy_fee:.2f} (报告称35,277.73)')
print(f'卖出费(0.18%=印花0.05%+佣金0.03%+滑点0.10%): {sell_fee:.2f} (报告称48,995.03)')
print(f'真实净收益: {net:.2f} (报告称17,891.47)')
print(f'净收益率: {net/1000000*100:.4f}% (报告称1.7981%)')
print(f'对账: 毛收益-买入费-卖出费={gross-buy_fee-sell_fee:.2f} vs 净收益{net:.2f}')
