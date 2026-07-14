"""调TDX取纯TDX数据 + 对齐VERA(2万/3%) + 跑stop/min, 对比VERA +47.85%"""
import sys, json, copy, time
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from datetime import date, timedelta
from app.backtest.tdx_runner import _run_daily_backtest
from app.tqsdk.bridge import TdxBridge

bt = json.load(open('output/backtest_results/bt_20260709_000234_1783526554.json', encoding='utf-8'))
params = bt['params']
start = date.fromisoformat(params['start_date']); end = date.fromisoformat(params['end_date'])
formula_start = (start - timedelta(days=365)).strftime('%Y%m%d')
end_time = end.strftime('%Y%m%d')
natural_days = (end - start).days
kline_count = max(100, int(natural_days * 0.7) + 80)

# 对齐VERA
params['position_size'] = 20000; params['position_ratio'] = 0.02
params['take_profit_tiers'] = [{'profit_pct':0.03,'sell_ratio':0.2},{'profit_pct':0.13,'sell_ratio':0.6}]

print(f'调TDX取纯TDX数据(公式QUANTQQ, 区间{start}~{end})...')
t0 = time.time()
bridge = TdxBridge()
sig = bridge.execute_screen_range(end_time=end_time, kline_count=kline_count,
                                   start_time=formula_start, formula_name=params['strategy_name'])
print(f'TDX取数耗时{time.time()-t0:.0f}s status={sig.get("status")}')
if sig.get('status') != 'ok':
    print('失败:', sig.get('message')); sys.exit(1)
print(f'  cache_hit={sig.get("cache_hit")} prices={len(sig.get("prices",{}))}只')
print(f'VERA实际: +47.85% 1831笔 | bt原值: +116% 2001笔')
print()

def run(fill, label):
    p = copy.deepcopy(params); p['realistic_stop_fill'] = fill
    t0 = time.time()
    r = _run_daily_backtest(copy.deepcopy(sig), p, start, end, None, None, {})
    dt = time.time()-t0; s = r['summary']; trades = r['trades']
    hs = [round(t['ret_pct'],1) for t in trades if t['reason']=='HS']
    hs46 = sum(1 for x in hs if abs(x+4.6)<0.15)
    print(f'[{label}] fill={fill!r} 耗时{dt:.1f}s')
    print(f'  收益={s.get("total_return")}% 回撤={s.get("max_drawdown")}% 胜率={s.get("win_rate")}% 交易={s.get("trades")}')
    print(f'  退出原因: {s.get("exit_reasons")}')
    print(f'  HS {len(hs)}笔(-4.6%占{hs46})')
    return s, trades

s_stop, _ = run('stop', 'stop(对齐VERA)')
s_min, _  = run(True,  'min(VERA同款)')

print()
print('=== 纯TDX+对齐VERA 对比 ===')
print(f'{"模式":<20}{"收益%":<10}{"胜率%":<10}{"交易":<8}{"回撤%":<10}{"距VERA":<10}')
for lbl,s in [('stop(对齐)',s_stop),('min(VERA同款)',s_min)]:
    gap=s.get("total_return",0)-47.85
    print(f'{lbl:<20}{s.get("total_return"):<10}{s.get("win_rate"):<10}{s.get("trades"):<8}{s.get("max_drawdown"):<10}{gap:+.2f}pct')
print(f'{"VERA实际":<20}{"47.85":<10}{"-":<10}{"1831":<8}{"-":<10}{"0":<10}')
