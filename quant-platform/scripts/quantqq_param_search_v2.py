"""
QUANTQQ 参数网格搜索 V2 - 大范围探索
边界扩大5倍：止损/TP1/时间止损/移动止盈全覆盖
"""
import sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.backtest.tdx_runner import run_tdx_backtest

# 大范围参数实验
# 关键洞察：2024H1是牛市，多数策略亏钱。
# 真正有价值的参数要找熊市/震荡市也能赚钱的。
EXPERIMENTS = [
    # A. 止损边界扩展
    ('止损-3%',    -0.03, 0.03, 0.30, 7,  0.05, 0.02),
    ('止损-4%',    -0.04, 0.03, 0.30, 7,  0.05, 0.02),
    ('止损-5%',    -0.05, 0.03, 0.30, 7,  0.05, 0.02),
    ('止损-6%',    -0.06, 0.03, 0.30, 7,  0.05, 0.02),
    ('止损-7%',    -0.07, 0.03, 0.30, 7,  0.05, 0.02),
    ('止损-8%',    -0.08, 0.03, 0.30, 7,  0.05, 0.02),
    ('止损-10%',   -0.10, 0.03, 0.30, 7,  0.05, 0.02),
    ('止损-12%',   -0.12, 0.03, 0.30, 7,  0.05, 0.02),
    # B. TP1止盈边界扩展
    ('TP1=1%',    -0.06, 0.01, 0.30, 7,  0.05, 0.02),
    ('TP1=2%',    -0.06, 0.02, 0.30, 7,  0.05, 0.02),
    ('TP1=3%',    -0.06, 0.03, 0.30, 7,  0.05, 0.02),
    ('TP1=4%',    -0.06, 0.04, 0.30, 7,  0.05, 0.02),
    ('TP1=5%',    -0.06, 0.05, 0.30, 7,  0.05, 0.02),
    ('TP1=6%',    -0.06, 0.06, 0.30, 7,  0.05, 0.02),
    ('TP1=8%',    -0.06, 0.08, 0.30, 7,  0.05, 0.02),
    ('TP1=10%',   -0.06, 0.10, 0.30, 7,  0.05, 0.02),
    # C. TP1卖出比例扩展
    ('TP1=3%卖50%', -0.06, 0.03, 0.50, 7, 0.05, 0.02),
    ('TP1=3%卖70%', -0.06, 0.03, 0.70, 7, 0.05, 0.02),
    ('TP1=3%卖20%', -0.06, 0.03, 0.20, 7, 0.05, 0.02),
    # D. 时间止损扩展
    ('时间止损3天',   -0.06, 0.03, 0.30, 3,  0.05, 0.02),
    ('时间止损5天',   -0.06, 0.03, 0.30, 5,  0.05, 0.02),
    ('时间止损7天',   -0.06, 0.03, 0.30, 7,  0.05, 0.02),
    ('时间止损10天',  -0.06, 0.03, 0.30, 10, 0.05, 0.02),
    ('时间止损15天',  -0.06, 0.03, 0.30, 15, 0.05, 0.02),
    ('时间止损20天',  -0.06, 0.03, 0.30, 20, 0.05, 0.02),
    # E. 移动止盈扩展
    ('移动止盈2%',  -0.06, 0.03, 0.30, 7,  0.02, 0.015),
    ('移动止盈3%',  -0.06, 0.03, 0.30, 7,  0.03, 0.02),
    ('移动止盈4%',  -0.06, 0.03, 0.30, 7,  0.04, 0.02),
    ('移动止盈5%',  -0.06, 0.03, 0.30, 7,  0.05, 0.02),
    ('移动止盈7%',  -0.06, 0.03, 0.30, 7,  0.07, 0.03),
    ('移动止盈10%', -0.06, 0.03, 0.30, 7,  0.10, 0.05),
    # F. 多层止盈组合
    ('双层TP1_2+4',  -0.06, 0.02, 0.30, 7,  0.04, 0.02),  # 特殊：TP1=2%用4%追踪
    ('双层TP1_3+5',  -0.06, 0.03, 0.30, 7,  0.05, 0.02),  # 特殊
    # G. 保守组合
    ('保守止损-8%TP1=2%',  -0.08, 0.02, 0.30, 7, 0.05, 0.02),
    ('保守止损-10%TP1=3%', -0.10, 0.03, 0.30, 7, 0.05, 0.02),
    # H. 激进组合
    ('激进止损-4%TP1=5%',  -0.04, 0.05, 0.40, 5, 0.05, 0.02),
    ('激进止损-5%TP1=4%',  -0.05, 0.04, 0.35, 5, 0.04, 0.02),
    # I. 时间止盈条件
    ('无时间止损',   -0.06, 0.03, 0.30, 99, 0.05, 0.02),
    ('无移动止盈',  -0.06, 0.03, 0.30, 7,  0.99, 0.99),  # 几乎不触发
]

print(f'总计 {len(EXPERIMENTS)} 组实验，边界大幅扩展')


def run_one(name, hard_stop, tp1_pct, tp1_ratio, time_exit_days, trail_activate, trail_dd):
    from app.sim_trader.config import (
        INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
        TIME_FORCE_DAYS, LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE, PAUSE_DAYS,
        SAME_STOCK_COOLDOWN, USE_ATR_TRAIL, ATR_TRAIL_MULTIPLIER,
        FIRST_DAY_EXIT_MIN_PROFIT, FIRST_DAY_EXIT_DAYS,
    )
    params = {
        'strategy_name': 'QUANTQQ',
        'strategy_type': 'tdx',
        'intraday_freq': 'daily',
        'start_date': '2024-01-01',
        'end_date': '2024-06-30',
        'initial_capital': INITIAL_CAPITAL,
        'position_size': POSITION_SIZE,
        'min_buy_amt': MIN_BUY_AMT,
        'hard_stop': hard_stop,
        'take_profit_tiers': [{'profit_pct': tp1_pct, 'sell_ratio': tp1_ratio}],
        'trail_activate': trail_activate,
        'trail_dd': trail_dd,
        'time_exit_days': time_exit_days,
        'time_exit_profit': 0.03,
        'time_force_days': TIME_FORCE_DAYS,
        'loss_streak_halve': LOSS_STREAK_HALVE,
        'loss_streak_pause': LOSS_STREAK_PAUSE,
        'pause_days': PAUSE_DAYS,
        'same_stock_cooldown': SAME_STOCK_COOLDOWN,
        'use_atr_trail': USE_ATR_TRAIL,
        'atr_trail_multiplier': ATR_TRAIL_MULTIPLIER,
        'first_day_exit_min_profit': FIRST_DAY_EXIT_MIN_PROFIT,
        'first_day_exit_days': FIRST_DAY_EXIT_DAYS,
        'signal_params': {},
    }
    t0 = time.time()
    result = run_tdx_backtest(params)
    elapsed = time.time() - t0
    if result.get('status') != 'ok':
        return None, elapsed, result.get('message', 'unknown error')
    s = result['summary']
    return {
        'name': name, 'hard_stop': hard_stop, 'tp1_pct': tp1_pct,
        'tp1_ratio': tp1_ratio, 'time_exit_days': time_exit_days,
        'trail_activate': trail_activate, 'trail_dd': trail_dd,
        'total_return': s['total_return'], 'ann_return': s['ann_return'],
        'max_drawdown': s['max_drawdown'], 'sharpe': s['sharpe'],
        'calmar': s['calmar'], 'win_rate': s['win_rate'],
        'profit_factor': s['profit_factor'], 'trades': s['trades'],
        'avg_win': s['avg_win'], 'avg_loss': s['avg_loss'],
        'final_equity': s['final_equity'],
    }, elapsed, None


def main():
    all_results = []
    t0_total = time.time()

    for i, exp in enumerate(EXPERIMENTS):
        name = exp[0]
        print(f'[{i+1}/{len(EXPERIMENTS)}] {name}...', end=' ', flush=True)
        result, elapsed, error = run_one(*exp)
        if error:
            print(f'ERROR: {error} ({elapsed:.1f}s)')
        elif result:
            print(f'{elapsed:.1f}s | '
                  f'收益:{result["total_return"]:+.2f}% | '
                  f'年化:{result["ann_return"]:+.2f}% | '
                  f'回撤:{result["max_drawdown"]:.2f}% | '
                  f'夏普:{result["sharpe"]:.2f} | '
                  f'胜率:{result["win_rate"]:.1f}% | '
                  f'交易:{result["trades"]}')
            all_results.append(result)
        else:
            print(f'FAILED ({elapsed:.1f}s)')

    t1_total = time.time()

    # 汇总
    print()
    print('=' * 110)
    print('参数搜索汇总 V2（按年化收益率排序）')
    print('=' * 110)
    hdr = f'{"名称":<16} {"止损":>6} {"TP1":>5} {"TP1R":>5} {"TExit":>6} {"TrAct":>6} {"总收益":>10} {"年化":>8} {"最大回撤":>10} {"夏普":>6} {"胜率":>6} {"交易数":>8}'
    print(hdr)
    print('-' * 110)

    all_results.sort(key=lambda x: -x['ann_return'])
    for r in all_results:
        print(f'{r["name"]:<16} {r["hard_stop"]*100:>5.0f}% {r["tp1_pct"]*100:>4.0f}% {r["tp1_ratio"]*100:>4.0f}% '
              f'{r["time_exit_days"]:>5}d {r["trail_activate"]*100:>5.0f}% '
              f'{r["total_return"]:>+9.2f}% {r["ann_return"]:>+7.2f}% {r["max_drawdown"]:>9.2f}% '
              f'{r["sharpe"]:>5.2f} {r["win_rate"]:>5.1f}% {r["trades"]:>7}')

    print('-' * 110)
    print(f'总耗时: {t1_total-t0_total:.1f}秒 ({len(all_results)}/{len(EXPERIMENTS)} 成功)')

    # TOP5 总结
    print()
    print('=== TOP5 最优参数 ===')
    for i, r in enumerate(all_results[:5]):
        print(f'{i+1}. {r["name"]}: 年化{r["ann_return"]:+.2f}% | 回撤{r["max_drawdown"]:.2f}% | 夏普{r["sharpe"]:.2f} | 胜率{r["win_rate"]:.1f}% | 交易{r["trades"]}')

    # 保存
    out_path = ROOT / 'output' / 'quantqq_param_search_v2.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'all': all_results, 'elapsed_total': t1_total - t0_total},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f'\n结果已保存: {out_path}')
    return all_results


if __name__ == '__main__':
    main()
