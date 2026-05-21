"""
回测 CLI — 参数来源与前端 /api/backtest/run-simple 完全一致

核心参数**直接读取前端保存的配置文件** output/backtest_config.json
该文件由前端每次回测时自动保存，是前端参数的唯一真实来源。
CLI 的 --strategy / --start / --end 可覆盖文件中的对应字段。

用法:
  python scripts/run_backtest.py                          # 前端最后一次的参数
  python scripts/run_backtest.py --strategy "MA5角度_TDXv2"  # 换策略
  python scripts/run_backtest.py --start 2023-01-01 --trades
"""
import sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import argparse, copy
from datetime import date
from app.backtest.simple_runner import run_backtest

# 前端配置文件路径（与 app/api/backtest.py 中 _BT_CONFIG_FILE 一致）
_FRONTEND_CONFIG = ROOT / "output" / "backtest_config.json"

# ── 策略名 → signal_params（与前端 _collectBtConfig 一致） ──────────
def _load_frontend_config() -> dict:
    """从 output/backtest_config.json 读取前端最后一次回测的参数"""
    if _FRONTEND_CONFIG.exists():
        with open(_FRONTEND_CONFIG, 'r', encoding='utf-8') as f:
            return json.load(f)
    # 兜底：如果配置文件不存在，用 config.py 的默认值
    from app.sim_trader.config import (
        INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
        HARD_STOP, TAKE_PROFIT_TIERS, TRAIL_ACTIVATE, TRAIL_DD,
        TIME_EXIT_DAYS, TIME_EXIT_PROFIT, TIME_FORCE_DAYS,
        LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE, SAME_STOCK_COOLDOWN,
        STRATEGY_NAME, USE_ATR_TRAIL, ATR_TRAIL_MULTIPLIER,
    )
    return {
        "strategy_name": STRATEGY_NAME,
        "start_date": "2026-01-01",
        "end_date": str(date.today()),
        "initial_capital": INITIAL_CAPITAL, "position_size": POSITION_SIZE,
        "min_buy_amt": MIN_BUY_AMT, "hard_stop": HARD_STOP,
        "trail_activate": TRAIL_ACTIVATE, "trail_dd": TRAIL_DD,
        "time_exit_days": TIME_EXIT_DAYS, "time_exit_profit": TIME_EXIT_PROFIT,
        "time_force_days": TIME_FORCE_DAYS, "same_stock_cooldown": SAME_STOCK_COOLDOWN,
        "loss_streak_halve": LOSS_STREAK_HALVE, "loss_streak_pause": LOSS_STREAK_PAUSE,
        "use_atr_trail": USE_ATR_TRAIL, "atr_trail_multiplier": ATR_TRAIL_MULTIPLIER,
        "take_profit_tiers": copy.deepcopy(TAKE_PROFIT_TIERS),
    }


def build_params(strategy_name: str = None, start_date: str = None,
                 end_date: str = None) -> dict:
    """构建与前端完全一致的 backtest params。
    核心参数从 output/backtest_config.json 读取（前端每次回测自动保存）。
    --strategy / --start / --end 可覆盖。"""
    cfg = _load_frontend_config()

    # 策略名：CLI > 配置文件 > config.py 默认
    if strategy_name is None:
        strategy_name = cfg.get("strategy_name")
    # 日期：CLI > 配置文件
    if start_date is None:
        start_date = cfg.get("start_date", "2026-01-01")
    if end_date is None:
        end_date = cfg.get("end_date", str(date.today()))

    # signal_params 由后端从策略文件 PARAMS 自动读取，前端/CLI 只传策略名

    return {
        "strategy_name": strategy_name,
        "start_date": start_date,
        "end_date": end_date,
        "initial_capital": cfg["initial_capital"],
        "position_size": cfg["position_size"],
        "min_buy_amt": cfg.get("min_buy_amt", 5000),
        "hard_stop": cfg["hard_stop"],
        "trail_activate": cfg["trail_activate"],
        "trail_dd": cfg["trail_dd"],
        "time_exit_days": cfg["time_exit_days"],
        "time_exit_profit": cfg.get("time_exit_profit", 0.03),
        "time_force_days": cfg["time_force_days"],
        "same_stock_cooldown": cfg.get("same_stock_cooldown", 20),
        "loss_streak_halve": cfg.get("loss_streak_halve", 3),
        "loss_streak_pause": cfg.get("loss_streak_pause", 5),
        "use_atr_trail": cfg.get("use_atr_trail", True),
        "atr_trail_multiplier": cfg.get("atr_trail_multiplier", 1.0),
        "take_profit_tiers": copy.deepcopy(cfg.get("take_profit_tiers", [])),
        "signal_params": cfg.get("signal_params", {}),
    }


def main():
    parser = argparse.ArgumentParser(description="回测 CLI（参数与前端一致）")
    parser.add_argument("--strategy", default=None,
                        help="策略名 (默认: 读取前端配置文件)")
    parser.add_argument("--start", default=None, help="起始日期 (默认: 读取前端配置文件)")
    parser.add_argument("--end", default=None, help="结束日期 (默认: 今天)")
    parser.add_argument("--trades", action="store_true", help="列出全部交易明细")
    parser.add_argument("--last", type=int, default=0, help="只列最近 N 笔交易")
    parser.add_argument("--codes", default=None, help="只显示指定股票 (逗号分隔)")
    args = parser.parse_args()

    params = build_params(
        strategy_name=args.strategy,
        start_date=args.start,
        end_date=args.end,
    )

    strategy = params["strategy_name"]
    print(f"策略: {strategy}")
    print(f"区间: {params['start_date']} ~ {params['end_date']}")
    print(f"signal_params: {params['signal_params']}")
    print(f"use_atr_trail: {params['use_atr_trail']}")
    print()

    result = run_backtest(params)
    s = result["summary"]

    print(f"{'='*55}")
    print(f"  收益率:    {s['total_return']:+.2f}%")
    print(f"  最大回撤:  {s['max_drawdown']:.2f}%")
    print(f"  胜率:      {s['win_rate']:.1f}%")
    print(f"  交易笔数:  {s['trades']}  (赢 {s['wins']} / 亏 {s['losses']})")
    print(f"  盈亏比:    {s['profit_factor']}")
    print(f"  夏普:      {s['sharpe']}  |  卡玛:  {s['calmar']}")
    print(f"  终值:      {s['final_equity']:.0f}")
    print(f"  退出原因:  {s['exit_reasons']}")
    print(f"{'='*55}")

    trades = result["trades"]

    if args.codes:
        codes = set(c.strip() for c in args.codes.split(","))
        trades = [t for t in trades if t["code"] in codes]

    if args.last > 0:
        trades = trades[-args.last:]

    if args.trades or args.last > 0 or args.codes:
        print(f"\n交易明细 ({len(trades)} 笔):")
        for i, t in enumerate(trades):
            print(f"  {i+1}. {t['code']} buy:{t['entry_date']}@{t['entry_px']:.2f} "
                  f"sell:{t['exit_date']}@{t['exit_px']:.2f} "
                  f"ret:{t['ret_pct']:+.2f}% {t['reason']} {t.get('hold_days','?')}d")


if __name__ == "__main__":
    main()
