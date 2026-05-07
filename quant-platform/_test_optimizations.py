"""
综合对比测试：P0/P1/P2 各优化方向效果
P0: 策略层优化 (删除日涨幅>2%, 放宽MA60, range_mid, quality权重)
P1: 波动率自适应止盈止损 (vol_adaptive)
P2: 市场状态评分 + Kelly仓位 (regime_filter + kelly_sizing)
"""
import sys, io, time
sys.path.insert(0, '.')
from datetime import date
from app.backtest.engine import backtest_engine

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

START = date(2026, 1, 1)
END = date(2026, 5, 2)

def run(label, **kwargs):
    t0 = time.time()
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    result = backtest_engine.run(
        strategy_name="ma5_angle",
        strategy_params={"version": "improved", "disable_quality_sort": False},
        start=START, end=END,
        use_portfolio=True,
        initial_capital=1_000_000,
        position_size=50_000,
        **kwargs
    )
    elapsed = time.time() - t0
    print(f"  ⏱ {elapsed:.0f}s")
    print(f"  成交笔数: {result.total_trades}")
    print(f"  胜率: {result.win_rate:.1f}%")
    print(f"  单笔平均收益: {result.total_pnl_pct:.2f}%")
    pr = getattr(result, 'portfolio_total_return', None)
    if pr is not None:
        print(f"  组合总收益率: {pr:.2f}%")
    skipped = getattr(result, 'portfolio_skipped', 0)
    print(f"  资金不足跳过: {skipped}")
    return result

# Test 1: P0 only (新策略，无优化引擎参数)
r1 = run("P0: 策略优化 (删除冗余+放宽MA60+range_mid+quality权重)")

# Test 2: P0 + P1 (波动率自适应)
r2 = run("P0+P1: 策略优化 + 波动率自适应止盈止损",
        use_vol_adaptive=True)

# Test 3: P0 + P2 (市场状态 + Kelly)
r3 = run("P0+P2: 策略优化 + 市场状态+Kelly仓位",
        use_regime_filter=True, use_kelly_sizing=True)

# Test 4: P0 + P1 + P2 (全开)
r4 = run("P0+P1+P2: 全优化组合",
        use_vol_adaptive=True,
        use_regime_filter=True, use_kelly_sizing=True)

# Summary
print(f"\n{'='*60}")
print(f"  最终对比总结 ({START} ~ {END})")
print(f"{'='*60}")

# 旧baseline (之前回测结果)
old_baseline = {"trades": 284, "wr": 51.8, "avg_pnl": 0.99, "portfolio": 14.07, "label": "旧Baseline"}

tests = [
    ("旧Baseline", old_baseline["trades"], old_baseline["wr"], old_baseline["avg_pnl"], old_baseline["portfolio"]),
    ("P0策略优化", r1.total_trades, r1.win_rate, r1.total_pnl_pct, getattr(r1, 'portfolio_total_return', 0)),
    ("P0+P1波动率", r2.total_trades, r2.win_rate, r2.total_pnl_pct, getattr(r2, 'portfolio_total_return', 0)),
    ("P0+P2择时仓位", r3.total_trades, r3.win_rate, r3.total_pnl_pct, getattr(r3, 'portfolio_total_return', 0)),
    ("全优化组合", r4.total_trades, r4.win_rate, r4.total_pnl_pct, getattr(r4, 'portfolio_total_return', 0)),
]

print(f"  {'方案':<16} {'成交':>6} {'胜率%':>7} {'单笔%':>7} {'组合%':>8}")
print(f"  {'-'*50}")
for name, trades, wr, avg_pnl, portfolio in tests:
    print(f"  {name:<16} {trades:>6} {wr:>7.1f} {avg_pnl:>7.2f} {portfolio:>8.2f}")
