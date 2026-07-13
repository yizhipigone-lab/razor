"""对齐VERA三项(仓位2万/TP1 3%/纯TDX OHLC)重跑，对比 stop vs min 成交价，看哪个接近VERA +47.85%"""
import sys, json, copy, time
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from datetime import date
import pandas as pd
from app.backtest.tdx_runner import _run_daily_backtest

bt = json.load(open('output/backtest_results/bt_20260709_000234_1783526554.json', encoding='utf-8'))
params = bt['params']
start = date.fromisoformat(params['start_date'])
end = date.fromisoformat(params['end_date'])

# 对齐VERA三项
params['position_size'] = 20000           # 仓位 5万→2万
params['position_ratio'] = 20000 / params['initial_capital']  # 0.02
params['take_profit_tiers'] = [
    {'profit_pct': 0.03, 'sell_ratio': 0.2},   # TP1 2.7%→3% (对齐VERA取整)
    {'profit_pct': 0.13, 'sell_ratio': 0.6},
]

print('对齐VERA: 仓位2万/只, TP1=3%, 纯TDX OHLC(parquet)')
print(f'区间 {start}~{end} (parquet数据到7-06)')
print(f'VERA实际: +47.85% 1831笔 | bt原值: +116% 2001笔')
print()

# 构造 sig_result (parquet完整OHLC = 纯TDX)
df = pd.read_parquet('output/tdx_cache/c52126f5a4f50522.parquet')
sig_result = {'signals': {}, 'prices': {}}
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

def run(fill, label):
    p = copy.deepcopy(params)
    p['realistic_stop_fill'] = fill
    t0 = time.time()
    r = _run_daily_backtest(copy.deepcopy(sig_result), p, start, end, None, None, {})
    dt = time.time()-t0
    s = r['summary']
    trades = r['trades']
    hs = [round(t['ret_pct'],1) for t in trades if t['reason']=='HS']
    tr = [round(t['ret_pct'],1) for t in trades if t['reason']=='TR']
    hs46 = sum(1 for x in hs if abs(x+4.6)<0.15)
    print(f'[{label}] fill={fill!r} 耗时{dt:.1f}s')
    print(f'  收益={s.get("total_return")}% 回撤={s.get("max_drawdown")}% 胜率={s.get("win_rate")}% 交易={s.get("trades")}')
    print(f'  退出原因: {s.get("exit_reasons")}')
    print(f'  HS {len(hs)}笔(-4.6%占{hs46}), TR {len(tr)}笔, TP1 {sum(1 for t in trades if t["reason"]=="TP1")}笔')
    return s, trades

s_stop, t_stop = run('stop', 'stop(TR对齐VERA,HS正常对齐)')
s_min,  t_min  = run(True,  'min(HS对齐VERA,TR偏保守)')

print()
print('=== 对齐VERA后对比 ===')
print(f'{"模式":<24}{"收益%":<10}{"胜率%":<10}{"交易":<8}{"回撤%":<10}')
for lbl,s in [('stop(对齐VERA)',s_stop),('min(HS对齐)',s_min)]:
    print(f'{lbl:<24}{s.get("total_return"):<10}{s.get("win_rate"):<10}{s.get("trades"):<8}{s.get("max_drawdown"):<10}')
print(f'{"VERA实际":<24}{"47.85":<10}{"-":<10}{"1831":<8}{"-":<10}')
print(f'{"bt原值(未对齐)":<24}{"116.07":<10}{"70":<10}{"2001":<8}{"4.53":<10}')

# 距VERA差距
for lbl,s in [('stop',s_stop),('min',s_min)]:
    print(f'  {lbl} 距VERA(+47.85%): {s.get("total_return",0)-47.85:+.2f}个百分点')
