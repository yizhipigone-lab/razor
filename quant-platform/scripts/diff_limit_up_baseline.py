"""对比 limit-up 修复前后基线

对计划原文的结构适配（2026-07-18，依据 simple_runner.run_backtest 真实返回结构）:
- result 顶层无 strategy_name/start_date/end_date：
  strategy_name 取自 result['params']，区间取自 result['summary']['start_date'/'end_date']。
- summary 无 'buys' 键：买入笔数改从 result['daily_trades'][*]['bought'] 列表统计
  （含期末仍持仓的买入，比 trades 数更准）。
- summary['total_return']/['max_drawdown']/['win_rate'] 均为百分数数值（如 47.0 表示 47%），
  与计划脚本假设一致，无需换算。
- 'intraday_window_fallback_count' 是 tdx_runner 的键，simple_runner 基线恒为 0（预期）。
"""
import argparse
import json
from pathlib import Path


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def count_buys(result):
    """从 daily_trades 统计真实买入笔数（含期末仍持仓）"""
    return sum(len(day.get('bought', []))
               for day in result.get('daily_trades', {}).values())


def main():
    parser = argparse.ArgumentParser(description="对比 limit-up 修复前后基线")
    parser.add_argument("before", help="改前基线 JSON")
    parser.add_argument("after", help="改后基线 JSON")
    parser.add_argument("--output", default="docs/reports/2026-07-18-limit-up-baseline-diff.md")
    args = parser.parse_args()

    before = load(args.before)
    after = load(args.after)

    sb = before["summary"]
    sa = after["summary"]

    buys_b = count_buys(before)
    buys_a = count_buys(after)
    strategy = before.get('params', {}).get('strategy_name', '?')

    lines = [
        "# 5m/涨停修复基线 Diff 报告",
        "",
        f"- 策略: {strategy}（simple_runner 日线；QUANTQQ 的 Python 复刻）",
        f"- 区间: {sb.get('start_date', '?')} ~ {sb.get('end_date', '?')}",
        "",
        "| 指标 | 改前 | 改后 | 变化 |",
        "|---|---|---|---|",
        f"| 收益率 | {sb['total_return']:+.2f}% | {sa['total_return']:+.2f}% | {sa['total_return'] - sb['total_return']:+.2f}% |",
        f"| 最大回撤 | {sb['max_drawdown']:.2f}% | {sa['max_drawdown']:.2f}% | {sa['max_drawdown'] - sb['max_drawdown']:+.2f}% |",
        f"| 交易笔数 | {sb['trades']} | {sa['trades']} | {sa['trades'] - sb['trades']:+d} |",
        f"| 买入笔数 | {buys_b} | {buys_a} | {buys_a - buys_b:+d} |",
        f"| 5m 降级买入笔数 | - | {sa.get('intraday_window_fallback_count', 0)} | - |",
        f"| 胜率 | {sb['win_rate']:.1f}% | {sa['win_rate']:.1f}% | {sa['win_rate'] - sb['win_rate']:+.1f}% |",
        f"| 夏普 | {sb['sharpe']} | {sa['sharpe']} | {sa['sharpe'] - sb['sharpe']:+.2f} |",
        "",
        "## 关键观察",
        "",
        "- 本基线为 simple_runner 日线回归检查：commits a2415ec..d5a3ab3 对该路径的设计意图是**行为保持**"
        "（prev_close 有效时 strict=True 与旧 can_buy 判定等价；缺失时 strict=False 与旧 (px,px) 放行等价），"
        "因此 before/after 预期**零差异或近零差异**。",
        "- 5m 降级与涨停 fail-closed 的主修复面在 tdx_runner / risk_gate，不在本基线覆盖内"
        "（'5m 降级买入笔数' 恒为 0 属预期）。",
        "- 若收益/回撤/笔数出现任何非零差异，需回查具体交易明细定位回归。",
        "",
        "## 退出原因分布",
        "",
        f"- 改前: {sb['exit_reasons']}",
        f"- 改后: {sa['exit_reasons']}",
    ]

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Diff 报告已保存: {output_path}")


if __name__ == "__main__":
    main()
