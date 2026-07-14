"""
QUANTQQ 参数网格搜索
基于缓存的全区间数据快速遍历12组参数
"""
import sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.backtest.tdx_runner import run_tdx_backtest

# 实验参数
EXPERIMENTS = [
    # (name, hard_stop, tp1_pct, tp1_ratio, time_exit_days, time_exit_profit, trail_activate, trail_dd)
    ('基准',        -0.06, 0.03, 0.30, 7,  0.03, 0.05, 0.02),
    ('止损-5%',     -0.05, 0.03, 0.30, 7,  0.03, 0.05, 0.02),
    ('止损-4%',     -0.04, 0.03, 0.30, 7,  0.03, 0.05, 0.02),
    ('TP1=2%',     -0.06, 0.02, 0.30, 7,  0.03, 0.05, 0.02),
    ('TP1=4%',     -0.06, 0.04, 0.30, 7,  0.03, 0.05, 0.02),
    ('时间止损5天', -0.06, 0.03, 0.30, 5,  0.03, 0.05, 0.02),
    ('时间止损10天',-0.06, 0.03, 0.30, 10, 0.03, 0.05, 0.02),
    ('TP1=5%激进', -0.06, 0.05, 0.40, 7,  0.03, 0.05, 0.02),
    ('止损-7%保守', -0.07, 0.03, 0.30, 7,  0.03, 0.05, 0.02),
    ('移动止盈3%', -0.06, 0.03, 0.30, 7,  0.03, 0.03, 0.02),
    ('移动止盈7%', -0.06, 0.03, 0.30, 7,  0.03, 0.07, 0.03),
    ('综合A',       -0.05, 0.04, 0.35, 5,  0.03, 0.04, 0.02),
]

def run_one(name, hard_stop, tp1_pct, tp1_ratio, time_exit_days, time_exit_profit, trail_activate, trail_dd):
    """运行单次实验"""
    from app.sim_trader.config import (
        INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
        TIME_EXIT_PROFIT as _DEFAULT_TEP,
        TIME_FORCE_DAYS, LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE, PAUSE_DAYS,
        SAME_STOCK_COOLDOWN, USE_ATR_TRAIL, ATR_TRAIL_MULTIPLIER,
        FIRST_DAY_EXIT_MIN_PROFIT, FIRST_DAY_EXIT_DAYS,
    )

    params = {
        'strategy_name': 'QUANTQQ',
        'strategy_type': 'tdx',
        'intraday_freq': 'daily',
        'start_date': '2024-01-01',
        'end_date': '2024-06-30',  # 半年区间
        'initial_capital': INITIAL_CAPITAL,
        'position_size': POSITION_SIZE,
        'min_buy_amt': MIN_BUY_AMT,
        'hard_stop': hard_stop,
        'take_profit_tiers': [{'profit_pct': tp1_pct, 'sell_ratio': tp1_ratio}],
        'trail_activate': trail_activate,
        'trail_dd': trail_dd,
        'time_exit_days': time_exit_days,
        'time_exit_profit': time_exit_profit,
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
        'name': name,
        'hard_stop': hard_stop,
        'tp1_pct': tp1_pct,
        'tp1_ratio': tp1_ratio,
        'time_exit_days': time_exit_days,
        'trail_activate': trail_activate,
        'total_return': s['total_return'],
        'ann_return': s['ann_return'],
        'max_drawdown': s['max_drawdown'],
        'sharpe': s['sharpe'],
        'calmar': s['calmar'],
        'win_rate': s['win_rate'],
        'profit_factor': s['profit_factor'],
        'trades': s['trades'],
        'avg_win': s['avg_win'],
        'avg_loss': s['avg_loss'],
        'final_equity': s['final_equity'],
    }, elapsed, None


def main():
    print(f'开始运行 {len(EXPERIMENTS)} 组参数实验...')
    print(f'基准: 2024-01-01 ~ 2024-06-30 (半年)')
    print()

    all_results = []
    t0_total = time.time()

    for i, (name, hs, tp1, tp1r, ted, tep, ta, td) in enumerate(EXPERIMENTS):
        print(f'[{i+1}/{len(EXPERIMENTS)}] {name}...', flush=True)
        result, elapsed, error = run_one(name, hs, tp1, tp1r, ted, tep, ta, td)

        if error:
            print(f'  ERROR: {error} ({elapsed:.1f}秒)')
        elif result:
            print(f'  耗时:{elapsed:.1f}秒 | '
                  f'收益:{result["total_return"]:+.2f}% | '
                  f'年化:{result["ann_return"]:+.2f}% | '
                  f'回撤:{result["max_drawdown"]:.2f}% | '
                  f'夏普:{result["sharpe"]} | '
                  f'胜率:{result["win_rate"]:.1f}% | '
                  f'交易:{result["trades"]}')
            all_results.append(result)
        print(flush=True)

    t1_total = time.time()

    # 输出汇总
    print()
    print('=' * 100)
    print('参数搜索汇总（按年化收益率排序）')
    print('=' * 100)
    print(f'{"名称":<12} {"止损":>6} {"TP1":>6} {"时间止损":>8} {"总收益":>10} {"年化":>8} {"最大回撤":>10} {"夏普":>6} {"胜率":>6} {"交易数":>8}')
    print('-' * 100)

    all_results.sort(key=lambda x: -x['ann_return'])
    for r in all_results:
        print(f'{r["name"]:<12} {r["hard_stop"]*100:>5.0f}% {r["tp1_pct"]*100:>5.0f}% {r["time_exit_days"]:>6}天 '
              f'{r["total_return"]:>+9.2f}% {r["ann_return"]:>+7.2f}% {r["max_drawdown"]:>9.2f}% '
              f'{r["sharpe"]:>5.2f} {r["win_rate"]:>5.1f}% {r["trades"]:>7}')

    print('-' * 100)
    print(f'总耗时: {t1_total-t0_total:.1f}秒')

    # 保存结果
    out_path = ROOT / 'output' / 'quantqq_param_search.json'
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({
            'baseline': all_results[0] if all_results else None,
            'best': all_results[0] if all_results else None,
            'all': all_results,
            'elapsed_total': t1_total - t0_total,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f'\n结果已保存: {out_path}')

    return all_results


if __name__ == '__main__':
    main()
