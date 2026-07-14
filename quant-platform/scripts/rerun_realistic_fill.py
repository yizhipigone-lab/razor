"""用缓存的 TDX signals 重跑回测，对比 max(旧/乐观) vs min(新/真实) 成交价"""
import sys, json, time, copy
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from datetime import date
from app.backtest.tdx_runner import _run_daily_backtest

with open('output/backtest_results/bt_20260707_062557_1783376757.json','r',encoding='utf-8') as f:
    bt = json.load(f)
params_orig = bt['params']

with open('output/tdx_cache/20269910a777074cbec64ff68565141c.json','r',encoding='utf-8') as f:
    sig_result = json.load(f)

start = date.fromisoformat(params_orig['start_date'])
end = date.fromisoformat(params_orig['end_date'])

def run(realistic):
    p = copy.deepcopy(params_orig)
    p['realistic_stop_fill'] = realistic
    t0 = time.time()
    r = _run_daily_backtest(copy.deepcopy(sig_result), p, start, end, None, None, {})
    s = r.get('summary', {})
    return time.time()-t0, s, r.get('trades',[])

# 旧逻辑 max（乐观，应复现 20.12%）
dt, s1, t1 = run(False)
print(f'旧逻辑 max(stop,open) [乐观]: 耗时{dt:.0f}s')
print(f'  收益={s1.get("total_return")}% 回撤={s1.get("max_drawdown")}% 胜率={s1.get("win_rate")}% 交易={s1.get("trades")} 最终={s1.get("final_equity")}')
print(f'  原 bt 记录: 收益=20.12% 交易=946 (对照)')

# 新逻辑 min（真实）
dt, s2, t2 = run(True)
print(f'\n新逻辑 min(stop,open) [真实]: 耗时{dt:.0f}s')
print(f'  收益={s2.get("total_return")}% 回撤={s2.get("max_drawdown")}% 胜率={s2.get("win_rate")}% 交易={s2.get("trades")} 最终={s2.get("final_equity")}')

# 退出原因对比
print(f'\n旧退出原因: {s1.get("exit_reasons")}')
print(f'新退出原因: {s2.get("exit_reasons")}')

# 差异
print(f'\n=== 差异 ===')
print(f'收益: {s1.get("total_return")}% -> {s2.get("total_return")}%  (差 {s2.get("total_return",0)-s1.get("total_return",0):+.2f}%)')
print(f'胜率: {s1.get("win_rate")}% -> {s2.get("win_rate")}%')
