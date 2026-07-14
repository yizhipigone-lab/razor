"""用当前代码重跑 bt_20260709，对比 max(旧) / stop(对齐VERA) / min 三种成交价模式
验证根因：修复后(stop)成本止损应变-4.6%、收益应下降接近VERA"""
import sys, json, copy, time
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from datetime import date, timedelta
from app.backtest.tdx_runner import _run_daily_backtest
from app.tqsdk.bridge import TdxBridge

bt = json.load(open('output/backtest_results/bt_20260709_000234_1783526554.json', encoding='utf-8'))
params = bt['params']
start = date.fromisoformat(params['start_date'])
end = date.fromisoformat(params['end_date'])
formula_start = (start - timedelta(days=365)).strftime('%Y%m%d')
end_time = end.strftime('%Y%m%d')
natural_days = (end - start).days
kline_count = max(100, int(natural_days * 0.7) + 80)
formula = params.get('strategy_name')
print(f'策略={formula} 区间={start}~{end} kline_count={kline_count}')
print(f'原bt(旧代码max): 收益={bt["summary"]["total_return"]}% 胜率={bt["summary"]["win_rate"]}% 交易={bt["summary"]["trades"]}')
print()

print('调TDX获取信号(可能数分钟)...')
t0 = time.time()
bridge = TdxBridge()
sig = bridge.execute_screen_range(end_time=end_time, kline_count=kline_count,
                                   start_time=formula_start, formula_name=formula)
print(f'信号获取耗时 {time.time()-t0:.0f}s, status={sig.get("status")}')
if sig.get('status') != 'ok':
    print('信号获取失败:', sig.get('message'))
    sys.exit(1)
print()

def run(fill, label):
    p = copy.deepcopy(params)
    p['realistic_stop_fill'] = fill
    t0 = time.time()
    r = _run_daily_backtest(copy.deepcopy(sig), p, start, end, None, None, {})
    dt = time.time()-t0
    s = r['summary']
    trades = r['trades']
    # 统计 HS ret
    hs = [round(t['ret_pct'],1) for t in trades if t['reason']=='HS']
    hs_46 = sum(1 for x in hs if abs(x+4.6)<0.15)
    print(f'[{label}] realistic={fill!r} 耗时{dt:.1f}s')
    print(f'  收益={s.get("total_return")}% 回撤={s.get("max_drawdown")}% 胜率={s.get("win_rate")}% 交易={s.get("trades")} 最终={s.get("final_equity")}')
    print(f'  退出原因: {s.get("exit_reasons")}')
    print(f'  HS共{len(hs)}笔, 其中-4.6%: {hs_46}笔, 其他: {len(hs)-hs_46}笔')
    return s, trades

# VERA对标: 成本止损全-4.6%, 收益应在VERA附近
s_max,  t_max  = run(False,  '旧max(乐观,bt同款)')
s_stop, t_stop = run('stop', 'stop(对齐VERA,当前默认)')
s_min,  t_min  = run(True,  'min(跳空保护)')

print()
print('=== 对比汇总 ===')
print(f'{"模式":<16}{"收益%":<10}{"胜率%":<10}{"交易":<8}{"HS中-4.6%占比":<16}')
for lbl,s,trades in [('旧max(bt)',s_max,t_max),('stop(对齐VERA)',s_stop,t_stop),('min',s_min,t_min)]:
    hs=[t['ret_pct'] for t in trades if t['reason']=='HS']
    r46 = f'{sum(1 for x in hs if abs(x+4.6)<0.15)}/{len(hs)}' if hs else '0/0'
    print(f'{lbl:<16}{s.get("total_return"):<10}{s.get("win_rate"):<10}{s.get("trades"):<8}{r46:<16}')
print(f'原bt_20260709:   {bt["summary"]["total_return"]:<10}{bt["summary"]["win_rate"]:<10}{bt["summary"]["trades"]:<8}')
print(f'VERA(1TEST.txt): 收益更低(成本止损全-4.6%), 交易1831')
