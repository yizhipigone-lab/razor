"""跑 limit-up 修复基线（simple_runner 日线回测）

用法:
    python scripts/run_limit_up_baseline.py --output output/baseline_before.json

策略名说明（2026-07-18 适配）:
    计划原文默认 "QUANTQQ"，但 QUANTQQ 是通达信公式名（走 tdx_runner），
    simple_runner 的策略体系（app/screener/strategies/）中并无 QUANTQQ 模块。
    app/screener/strategies/ma5_angle_cross.py 文件头注释明确写明
    "MA5金叉策略 — Python 复刻通达信 QUANTQQ 公式"，
    且 strategy_files 映射表以 'MA5金叉' 为键指向该模块，
    故本脚本默认策略改为 'MA5金叉'（即 QUANTQQ 的 Python 复刻版）。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.backtest.simple_runner import run_backtest


def main():
    parser = argparse.ArgumentParser(description="跑 limit-up 修复基线")
    parser.add_argument("--strategy", default="MA5金叉",
                        help="策略名（QUANTQQ 的 Python 复刻为 'MA5金叉' / ma5_angle_cross）")
    parser.add_argument("--start", default="20230101", help="起始日期")
    parser.add_argument("--end", default="20240630", help="结束日期")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    args = parser.parse_args()

    params = {
        "strategy_name": args.strategy,
        "start_date": args.start,
        "end_date": args.end,
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "min_buy_amt": 5_000,
        "hard_stop": 0.05,
        "trail_activate": 0.10,
        "trail_dd": 0.05,
        "time_exit_days": 20,
        "time_exit_profit": 0.03,
        "time_force_days": 5,
        "same_stock_cooldown": 20,
        "loss_streak_halve": 3,
        "loss_streak_pause": 5,
        "use_atr_trail": True,
        "atr_trail_multiplier": 1.0,
        "take_profit_tiers": [],
        "first_day_exit_min_profit": 0.03,
        "first_day_exit_days": 1,
        "signal_params": {},
    }

    result = run_backtest(params)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"基线已保存: {output_path}")


if __name__ == "__main__":
    main()
