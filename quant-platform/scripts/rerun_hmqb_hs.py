"""黑马起步 沪深A股 日线回测 2019-至今 对齐VERA套 扣费"""
import sys, json, copy, time
sys.path.insert(0, 'e:/1target/p9_project/quant-platform')
from datetime import date, timedelta
from app.backtest.tdx_runner import _run_daily_backtest
from app.tqsdk.bridge import TdxBridge

hs = json.load(open(r'C:\Users\liuziheng\AppData\Local\Temp\hs_a.json'))
print(f'沪深A股: {len(hs)}只', flush=True)

start = date(2019, 6, 1)
end = date(2026, 7, 10)
formula_start = (start - timedelta(days=365)).strftime('%Y%m%d')
end_time = end.strftime('%Y%m%d')
kline_count = max(100, int((end - start).days * 0.7) + 80)
print(f'区间 {start}~{end} kline_count={kline_count}', flush=True)

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

print(f'{time.strftime("%H:%M:%S")} 开始TDX选股(5204只×黑马起步×2019至今)...', flush=True)
t0 = time.time()
bridge = TdxBridge()
sig = bridge.execute_screen_range(end_time=end_time, kline_count=kline_count,
                                   start_time=formula_start, formula_name='黑马起步',
                                   stock_list_override=hs)
print(f'{time.strftime("%H:%M:%S")} TDX取数耗时{time.time()-t0:.0f}s status={sig.get("status")}', flush=True)
if sig.get('status') != 'ok':
    print('失败:', sig.get('message'), flush=True)
    sys.exit(1)
print(f'  prices={len(sig.get("prices",{}))}只', flush=True)

print(f'{time.strftime("%H:%M:%S")} 开始日线回测...', flush=True)
t0 = time.time()
r = _run_daily_backtest(copy.deepcopy(sig), params, start, end, None, None, {})
print(f'{time.strftime("%H:%M:%S")} 回测耗时{time.time()-t0:.0f}s', flush=True)
s = r['summary']
print(f'\n=== 黑马起步 沪深A股 日线 2019-至今 ===')
print(f'总收益={s["total_return"]}% 回撤={s["max_drawdown"]}% 胜率={s["win_rate"]}%')
print(f'交易={s["trades"]} 信号={s["signals"]} 净值={s["final_equity"]} 天数={s["trading_days"]}')
print(f'退出原因: {s.get("exit_reasons")}', flush=True)

out = f'output/backtest_results/hmqb_hs_{date.today().strftime("%Y%m%d")}.json'
json.dump(r, open(out, 'w', encoding='utf-8'), ensure_ascii=False, default=str)
print(f'已存: {out}', flush=True)
