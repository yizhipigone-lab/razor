"""调通达信重跑 bt_20260707_062557 原始参数，对比 max(旧) vs min(新) 成交价
第一次调通达信(~5min)写parquet缓存，第二次命中缓存秒级跑"""
import sys, json, time, copy
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from app.backtest.tdx_runner import run_tdx_backtest

with open('output/backtest_results/bt_20260707_062557_1783376757.json','r',encoding='utf-8') as f:
    bt = json.load(f)
params_orig = bt['params']
print(f'策略={params_orig["strategy_name"]} 区间={params_orig["start_date"]}~{params_orig["end_date"]}')
print(f'hard_stop={params_orig["hard_stop"]} trail={params_orig["trail_activate"]}/{params_orig["trail_dd"]} tiers={params_orig["take_profit_tiers"]}')
print()

def run(realistic, label):
    p = copy.deepcopy(params_orig)
    p['realistic_stop_fill'] = realistic
    t0 = time.time()
    r = run_tdx_backtest(p)
    dt = time.time()-t0
    s = r.get('summary', {})
    print(f'[{label}] realistic={realistic} 耗时{dt:.0f}s')
    print(f'  status={r.get("status")} 收益={s.get("total_return")}% 回撤={s.get("max_drawdown")}% 胜率={s.get("win_rate")}% 交易={s.get("trades")} 最终={s.get("final_equity")}')
    print(f'  退出原因: {s.get("exit_reasons")}')
    return s

# 第一次：旧逻辑 max（调通达信，写缓存）
s1 = run(False, '旧max(乐观)')

print()

# 第二次：新逻辑 min（命中缓存）
s2 = run(True, '新min(真实)')

print()
print('=== 对比 ===')
print(f'收益: {s1.get("total_return")}% -> {s2.get("total_return")}%  (差 {s2.get("total_return",0)-s1.get("total_return",0):+.2f}%)')
print(f'回撤: {s1.get("max_drawdown")}% -> {s2.get("max_drawdown")}%')
print(f'胜率: {s1.get("win_rate")}% -> {s2.get("win_rate")}%')
print(f'交易: {s1.get("trades")} -> {s2.get("trades")}')
print(f'原bt记录: 收益=20.12% 交易=946 胜率=67%')
