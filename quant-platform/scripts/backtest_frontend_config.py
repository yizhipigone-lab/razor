"""
用前端真实配置跑全市场回测，复现 +226%
"""
import sys, os, warnings
warnings.filterwarnings('ignore')
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ROOT = Path(__file__).resolve().parent.parent

from app.backtest.simple_runner import load_daily_bars, run_backtest

# 前端真实配置
params = {
    'initial_capital': 1_000_000,
    'position_size': 50000,
    'min_buy_amt': 5000,
    # 止损止盈
    'hard_stop': -0.06,
    'tp1_pct': 0.04,
    'tp1_sell_ratio': 0.15,
    'tp2_pct': 0.07,              # ← 之前我用了0.12！
    # 移动止盈
    'trail_activate': 0.03,        # ← 之前我用了0.05
    'trail_dd': 0.01,              # ← 之前我用了0.025
    # 时间止盈
    'time_exit_days': 3,           # ← 之前我用了5
    'time_exit_profit': 0.03,
    'time_force_days': 9,          # ← 之前我用了20
    # 资金管理
    'loss_streak_halve': 3,
    'loss_streak_pause': 5,
    'pause_days': 3,
    'same_stock_cooldown': 20,
    # 信号参数
    'signal_params': {
        "version": "improved",
        "filter_st": True,
        "filter_bj": True,
        "vol_threshold": 1.5,
        "close_position_threshold": 0.8,
        "disable_quality_sort": False,
        "filter_consecutive_up": False,
        "filter_gap_quality": False,
    },
    'start_date': date(2022, 1, 1),
    'end_date': date(2026, 5, 9),
}

print("=" * 70)
print("全市场回测 — 前端真实配置")
print("=" * 70)

result = run_backtest(params)

if result['status'] == 'ok':
    s = result['summary']
    print(f"\n  回测区间: {s['start_date']} ~ {s['end_date']}")
    print(f"  总收益率: {s['total_return']:+.2f}%")
    print(f"  最大回撤: {s['max_drawdown']:.2f}%")
    print(f"  胜率:     {s['win_rate']:.1f}%")
    print(f"  初始资金: {s['initial_capital']:,.0f}")
    print(f"  最终资金: {s['final_equity']:,.0f}")
    print(f"  交易日:   {s['trading_days']}天 / {s['total_calendar_days']}天")
    print(f"  信号/交易: {s['signals']} / {s['trades']}")
    print(f"  买入/卖出: {s.get('buy_signals','?')} / {s.get('sell_signals','?')}")
    print(f"  夏普/卡玛: {s['sharpe']:.2f} / {s['calmar']:.2f}")
    print(f"  索提诺/盈亏比: {s['sortino']:.2f} / {s['profit_ratio']:.2f}")
    print(f"  利润因子/年化: {s['profit_factor']:.2f} / {s['ann_return']:.2f}%")
    print(f"  最佳/最差: {s['best_trade']:+.2f}% / {s['worst_trade']:+.2f}%")
    print(f"  均盈/均亏: {s['avg_win']:+.2f}% / {s['avg_loss']:+.2f}%")
    print(f"  均盈持仓/均亏持仓: {s['avg_hold_win']:.1f}天 / {s['avg_hold_loss']:.1f}天")
    print(f"  盈利月: {s['positive_months']}")
    print(f"  退出原因: {s['exit_reasons']}")
    print(f"  胜/负: {s['wins']}/{s['losses']}")
else:
    print(f"  Error: {result}")
    print(f"  Status: {result.get('status')}")

print("=" * 70)
