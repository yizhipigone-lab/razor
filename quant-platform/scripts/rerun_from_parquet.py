"""从 parquet 缓存构造 sig_result，用当前代码重跑 bt_20260709，对比 max/stop/min 三种成交价模式"""
import sys, json, copy, time
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from datetime import date
import pandas as pd
from app.backtest.tdx_runner import _run_daily_backtest

bt = json.load(open('output/backtest_results/bt_20260709_000234_1783526554.json', encoding='utf-8'))
params = bt['params']
start = date.fromisoformat(params['start_date'])
end = date.fromisoformat(params['end_date'])

print('从 parquet 构造 sig_result...')
t0 = time.time()
df = pd.read_parquet('output/tdx_cache/c52126f5a4f50522.parquet')
print(f'  parquet {len(df)}行, 耗时{time.time()-t0:.0f}s')

sig_result = {'signals': {}, 'prices': {}}
t0 = time.time()
for code, g in df.groupby('code'):
    g = g.sort_values('date')
    dt_list = g['date'].astype(str).tolist()
    sig_result['prices'][code] = {
        'Date': dt_list,
        'Close': [float(x) if pd.notna(x) else 0.0 for x in g['close']],
        'High':  [float(x) if pd.notna(x) else 0.0 for x in g['high']],
        'Low':   [float(x) if pd.notna(x) else 0.0 for x in g['low']],
        'Open':  [float(x) if pd.notna(x) else 0.0 for x in g['open']],
    }
    sig_result['signals'][code] = {
        'Date': dt_list,
        'ZP': [str(int(v)) if pd.notna(v) else '0' for v in g['signal_value']],
    }
print(f'  构造完成 {len(sig_result["prices"])}只code, 耗时{time.time()-t0:.0f}s')
print(f'原bt(旧代码max): 收益={bt["summary"]["total_return"]}% 胜率={bt["summary"]["win_rate"]}% 交易={bt["summary"]["trades"]}')
print()

def run(fill, label):
    p = copy.deepcopy(params)
    p['realistic_stop_fill'] = fill
    t0 = time.time()
    r = _run_daily_backtest(copy.deepcopy(sig_result), p, start, end, None, None, {})
    dt = time.time()-t0
    s = r['summary']
    trades = r['trades']
    hs = [round(t['ret_pct'],1) for t in trades if t['reason']=='HS']
    hs_46 = sum(1 for x in hs if abs(x+4.6)<0.15)
    print(f'[{label}] realistic={fill!r} 耗时{dt:.1f}s')
    print(f'  收益={s.get("total_return")}% 回撤={s.get("max_drawdown")}% 胜率={s.get("win_rate")}% 交易={s.get("trades")} 最终={s.get("final_equity")}')
    print(f'  退出原因: {s.get("exit_reasons")}')
    print(f'  HS共{len(hs)}笔, 其中-4.6%: {hs_46}笔, 其他(>-4.5%): {len(hs)-hs_46}笔')
    return s, trades

s_max,  t_max  = run(False,  '旧max(乐观,bt同款)')
s_stop, t_stop = run('stop', 'stop(对齐VERA,当前默认)')
s_min,  t_min  = run(True,  'min(跳空保护,VERA同款)')

print()
print('=== 对比汇总 ===')
print(f'{"模式":<18}{"收益%":<10}{"胜率%":<10}{"交易":<8}{"HS中-4.6%":<14}')
for lbl,s,trades in [('旧max(bt同款)',s_max,t_max),('stop对齐VERA',s_stop,t_stop),('min(VERA同款)',s_min,t_min)]:
    hs=[t['ret_pct'] for t in trades if t['reason']=='HS']
    r46 = f'{sum(1 for x in hs if abs(x+4.6)<0.15)}/{len(hs)}' if hs else '0/0'
    print(f'{lbl:<18}{s.get("total_return"):<10}{s.get("win_rate"):<10}{s.get("trades"):<8}{r46:<14}')
print(f'原bt_20260709:    {bt["summary"]["total_return"]:<10}{bt["summary"]["win_rate"]:<10}{bt["summary"]["trades"]:<8}(HS -4.6%仅80/670)')
print(f'VERA(1TEST.txt):  成本止损全-4.6%, 交易1831, 收益更低')
