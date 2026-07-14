"""验证费用假设: 我方stop模式 毛收益 vs 净收益(扣费), 对比VERA毛收益47.85%"""
import sys, json, copy
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from datetime import date
import pandas as pd
from app.backtest.tdx_runner import _run_daily_backtest

bt = json.load(open('output/backtest_results/bt_20260709_000234_1783526554.json', encoding='utf-8'))
params = bt['params']
start = date.fromisoformat(params['start_date']); end = date.fromisoformat(params['end_date'])
params['position_size'] = 20000; params['position_ratio'] = 0.02
params['take_profit_tiers'] = [{'profit_pct':0.03,'sell_ratio':0.2},{'profit_pct':0.13,'sell_ratio':0.6}]
params['realistic_stop_fill'] = 'stop'

# 用新TDX缓存(parquet)
df = pd.read_parquet('output/tdx_cache/3a1b031deae13d5b.parquet')
sig = {'signals':{}, 'prices':{}}
for code, g in df.groupby('code'):
    g = g.sort_values('date'); dt = g['date'].astype(str).tolist()
    sig['prices'][code] = {'Date':dt,'Close':[float(x) if pd.notna(x) else 0 for x in g['close']],
        'High':[float(x) if pd.notna(x) else 0 for x in g['high']],'Low':[float(x) if pd.notna(x) else 0 for x in g['low']],
        'Open':[float(x) if pd.notna(x) else 0 for x in g['open']]}
    sig['signals'][code] = {'Date':dt,'ZP':[str(int(v)) if pd.notna(v) else '0' for v in g['signal_value']]}

r = _run_daily_backtest(copy.deepcopy(sig), params, start, end, None, None, {})
trades = r['trades']
s = r['summary']

# 净收益(扣费后)
net_ret = s['total_return']
# 毛收益 = sum(ret_pct * entry_total) / 本金
gross_pnl = sum(t['ret_pct']/100 * t['entry_total'] for t in trades)
gross_ret = gross_pnl / params['initial_capital'] * 100
fee = gross_ret - net_ret

print(f'=== 我方stop模式(2万+3%+纯TDX) 费用分析 ===')
print(f'交易数: {len(trades)}')
print(f'净收益率(扣费后, equity口径): {net_ret:.2f}%')
print(f'毛收益率(sum ret_pct*entry/本金): {gross_ret:.2f}%')
print(f'费用占比: {fee:.2f}%  (毛-净)')
print(f'平均每笔费用率: {fee/len(trades)*100:.3f}%/笔 (相对本金)')
print()
# 平均每笔相对仓位费用
avg_fee_per_trade = fee*params['initial_capital']/100/len(trades)
print(f'平均每笔费用金额: {avg_fee_per_trade:.1f}元 (仓位2万, 费率约{avg_fee_per_trade/20000*100:.3f}%)')
print()
print(f'=== 对比 ===')
print(f'VERA(毛, 不扣费):       47.85%')
print(f'我方毛(不扣费):         {gross_ret:.2f}%  ← 应和VERA可比')
print(f'我方净(扣费):           {net_ret:.2f}%  ← 之前报告的值')
print(f'毛收益差距(我方-VERA):  {gross_ret-47.85:+.2f}pct')
