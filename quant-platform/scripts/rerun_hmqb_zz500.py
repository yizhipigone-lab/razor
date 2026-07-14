"""黑马起步 中证500 日线回测 2019-至今 对齐VERA套 扣费"""
import sys, json, copy, time
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from datetime import date, timedelta
from app.backtest.tdx_runner import _run_daily_backtest
from app.tqsdk.bridge import TdxBridge

zz500 = json.load(open(r'C:\Users\liuziheng\AppData\Local\Temp\zz500.json'))
print(f'中证500成分股: {len(zz500)}只 样例:{zz500[:3]}')

start = date(2019, 6, 1)
end = date(2026, 7, 10)
formula_start = (start - timedelta(days=365)).strftime('%Y%m%d')
end_time = end.strftime('%Y%m%d')
natural_days = (end - start).days
kline_count = max(100, int(natural_days * 0.7) + 80)
print(f'区间 {start}~{end} formula_start={formula_start} kline_count={kline_count}')

params = {
    'initial_capital': 1000000, 'position_size': 50000,
    'min_buy_amt': 5000, 'same_stock_cooldown': 20,
    'hard_stop': -0.046, 'trail_activate': 0.039, 'trail_dd': 0.017,
    'time_exit_days': 7, 'time_exit_profit': 0.025, 'time_force_days': 12,
    'loss_streak_halve': 3, 'loss_streak_pause': 5, 'pause_days': 3,
    'first_day_exit_min_profit': 0, 'first_day_exit_days': 1,
    'take_profit_tiers': [{'profit_pct': 0.03, 'sell_ratio': 0.2}, {'profit_pct': 0.13, 'sell_ratio': 0.6}],
    'realistic_stop_fill': 'stop', 'use_atr_trail': True, 'atr_trail_multiplier': 1,
    'priority_mode': 'trailing_first', 'tp_stack_mode': True, 'tp1_fill_pct': 0.03,
    'start_date': str(start), 'end_date': str(end),
    'strategy_type': 'tdx', 'strategy_name': '黑马起步', 'intraday_freq': 'daily',
    'position_ratio': 0.05,
}

print('\n调TDX跑黑马起步选股(500只×2019至今, 可能数分钟)...')
t0 = time.time()
bridge = TdxBridge()
sig = bridge.execute_screen_range(end_time=end_time, kline_count=kline_count,
                                   start_time=formula_start, formula_name='黑马起步',
                                   stock_list_override=zz500)
print(f'TDX取数耗时{time.time()-t0:.0f}s status={sig.get("status")}')
if sig.get('status') != 'ok':
    print('失败:', sig.get('message'))
    sys.exit(1)
prices = sig.get('prices', {})
print(f'  prices={len(prices)}只 cache_hit={sig.get("cache_hit")}')

print('\n跑日线回测(对齐VERA套+扣费)...')
t0 = time.time()
r = _run_daily_backtest(copy.deepcopy(sig), params, start, end, None, None, {})
print(f'回测耗时{time.time()-t0:.0f}s')
s = r['summary']
print('\n=== 黑马起步 中证500 日线 2019-06至今(对齐VERA套+扣费) ===')
print(f'总收益: {s["total_return"]}%')
print(f'最大回撤: {s["max_drawdown"]}%')
print(f'胜率: {s["win_rate"]}%')
print(f'交易笔数: {s["trades"]}  买入信号: {s["signals"]}')
print(f'最终净值: {s["final_equity"]}  交易天数: {s["trading_days"]}')
print(f'夏普/卡玛/索提诺: {s.get("sharpe")}/{s.get("calmar")}/{s.get("sortino")}')
print(f'盈亏比/利润因子: {s.get("profit_ratio")}/{s.get("profit_factor")}')
print(f'退出原因: {s.get("exit_reasons")}')
print(f'胜/负: {s["wins"]}/{s["losses"]}  最佳/最差: {s.get("best_trade")}/{s.get("worst_trade")}')

# 存结果
out = f'output/backtest_results/hmqb_zz500_{date.today().strftime("%Y%m%d")}.json'
json.dump(r, open(out, 'w', encoding='utf-8'), ensure_ascii=False, default=str)
print(f'\n完整结果已存: {out}')
