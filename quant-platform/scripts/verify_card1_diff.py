#!/usr/bin/env python3
"""CARD1 改动端到端验证:对比改前/改后 simple_runner 回测的 summary 差异。

用法:
  # 1) 暂存当前改动
  git stash
  # 2) 跑基线
  python scripts/verify_card1_diff.py --mode before --output summary_before.json
  # 3) 恢复改动
  git stash pop
  # 4) 跑改后
  python scripts/verify_card1_diff.py --mode after --output summary_after.json
  # 5) 对比
  python scripts/verify_card1_diff.py --mode diff summary_before.json summary_after.json

预期(plan 自审已写明):
- total_return / sharpe / max_dd 偏差 < ~15%(否则排查 loss_streak 反馈)
- win_rate / avg_win / worst_trade 必降(净口径)
- trades / losses 可能变(loss_streak pause 提前)
- TDX 5m → VERA 对齐不受影响(等价改写)
"""
import argparse
import json
import sys
from pathlib import Path

# 确保项目根在 import 路径
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def run_simple_backtest():
    """跑一段简单回测(短周期,够看 loss_streak 触发 + 看 summary 即可)。"""
    from app.backtest.simple_runner import run_backtest

    params = {
        "start_date": "2024-01-01",
        "end_date": "2024-06-30",
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "strategy_name": "ma5_angle",
        "loss_streak_halve": 3,
        "loss_streak_pause": 5,
        "pause_days": 3,
        "tp1_pct": 3.0,
        "tp2_pct": 6.0,
        "tp1_sell_ratio": 0.5,
    }
    result = run_backtest(params)
    summary = result.get("summary", {})  # run_backtest 把 summary 嵌在 'summary' 键
    if not summary:
        # 容错:顶层跑出数字的话就用顶层
        summary = result
    return {
        "total_return": summary.get("total_return"),
        "sharpe": summary.get("sharpe"),
        "max_drawdown": summary.get("max_drawdown"),
        "win_rate": summary.get("win_rate"),
        "avg_win": summary.get("avg_win"),
        "avg_loss": summary.get("avg_loss"),
        "profit_factor": summary.get("profit_factor"),
        "best_trade": summary.get("best_trade"),
        "worst_trade": summary.get("worst_trade"),
        "trades": summary.get("trades", 0),
        "losses": summary.get("losses", 0),
    }


def cmd_run(mode, output):
    print(f"[VERIFY] 跑回测 mode={mode} ...")
    summary = run_simple_backtest()
    Path(output).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[VERIFY] 写入 {output}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_diff(before_path, after_path, threshold_pct):
    before = json.loads(Path(before_path).read_text(encoding="utf-8"))
    after = json.loads(Path(after_path).read_text(encoding="utf-8"))
    print(f"字段                                  before         after          偏差%      预期")
    print("-" * 88)
    must_drop = ("win_rate", "avg_win", "worst_trade")
    must_stable = ("total_return", "sharpe", "max_drawdown")
    fails = []

    for k in before:
        b = before[k]
        a = after[k]
        if isinstance(b, (int, float)) and isinstance(a, (int, float)) and b != 0:
            delta_pct = (a - b) / abs(b) * 100
            ok = "✓"
            if k in must_drop and a > b:
                ok = "❌ 净口径必降,却升了"
                fails.append(k)
            if k in must_stable and abs(delta_pct) > threshold_pct:
                ok = f"❌ 偏差 {abs(delta_pct):.1f}% > {threshold_pct}%"
                fails.append(k)
            print(f"{k:38s} {b:>13.4f} {a:>13.4f}  {delta_pct:+8.2f}%  {ok}")
        else:
            print(f"{k:38s} {str(b):>13s} {str(a):>13s}")

    print()
    if fails:
        print(f"[VERIFY FAIL] 字段违反预期: {fails}")
        sys.exit(1)
    print("[VERIFY PASS] 全部字段符合 CARD1 预期(净口径必降稳/收益曲线稳健)")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--mode", required=True, choices=["before", "after"])
    p_run.add_argument("--output", required=True)

    p_diff = sub.add_parser("diff")
    p_diff.add_argument("--threshold-pct", type=float, default=15.0)
    p_diff.add_argument("before_json")
    p_diff.add_argument("after_json")

    args = parser.parse_args()
    if args.cmd == "run":
        cmd_run(args.mode, args.output)
    elif args.cmd == "diff":
        cmd_diff(args.before_json, args.after_json, args.threshold_pct)


if __name__ == "__main__":
    main()
