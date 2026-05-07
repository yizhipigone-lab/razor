"""
对比测试：有/无热门概念过滤的回测效果（缩短范围快速验证）
"""
import sys, io
sys.path.insert(0, '.')
from datetime import date
from app.backtest.engine import backtest_engine

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 用最近4个月快速验证
START = date(2026, 1, 1)
END = date(2026, 5, 2)

def run_test(label, use_hot_concept, hot_concept_top_n=5):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    result = backtest_engine.run(
        strategy_name="ma5_angle",
        strategy_params={"version": "improved", "disable_quality_sort": False},
        start=START,
        end=END,
        use_portfolio=True,
        initial_capital=1000000,
        position_size=50000,
        use_hot_concept=use_hot_concept,
        hot_concept_top_n=hot_concept_top_n,
    )
    print(f"  信号数: {result.total_trades}")
    print(f"  胜率: {result.win_rate:.1f}%")
    print(f"  总收益: {result.total_pnl_pct:.2f}%")
    if hasattr(result, 'portfolio_total_return') and result.portfolio_total_return is not None:
        print(f"  组合总收益率: {result.portfolio_total_return:.2f}%")
    return result

r1 = run_test("BASELINE (无热门概念过滤)", use_hot_concept=False)
r2 = run_test("热门概念 Top 5", use_hot_concept=True, hot_concept_top_n=5)
r3 = run_test("热门概念 Top 10", use_hot_concept=True, hot_concept_top_n=10)

print(f"\n{'='*60}")
print(f"  对比总结 ({START} ~ {END})")
print(f"{'='*60}")
print(f"  {'指标':<20} {'Baseline':>10} {'Top5':>10} {'Top10':>10}")
print(f"  {'总交易':<20} {r1.total_trades:>10} {r2.total_trades:>10} {r3.total_trades:>10}")
print(f"  {'胜率%':<20} {r1.win_rate:>10.1f} {r2.win_rate:>10.1f} {r3.win_rate:>10.1f}")
print(f"  {'总收益%':<20} {r1.total_pnl_pct:>10.2f} {r2.total_pnl_pct:>10.2f} {r3.total_pnl_pct:>10.2f}")
