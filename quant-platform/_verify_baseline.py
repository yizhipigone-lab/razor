"""基线验证：无RPS+红盘日(策略内置)+17.26%退出参数"""
import sys, io, time
from pathlib import Path
from datetime import date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from core.settings import settings
from app.backtest.engine import BacktestEngine

START = date(2025, 4, 25)
END = date(2026, 5, 2)

print(f"{'='*80}")
print(f"基线验证回测")
print(f"{'='*80}")
print(f"策略: ma5_angle | 红盘日(策略内置) | 无RPS")
risk = settings.get("risk") or {}
print(f"退出: 2档止盈 {risk['staged_take_profit'][0]['profit_pct']}%/{int(risk['staged_take_profit'][0]['sell_ratio']*100)}% | {risk['staged_take_profit'][1]['profit_pct']}%/{int(risk['staged_take_profit'][1]['sell_ratio']*100)}%")
print(f"      硬止损{risk['hard_stop_loss_pct']}% | 移动{risk['trailing_stop_activate_pct']}%/{risk['trailing_stop_drawdown_pct']}%")
print(f"      利润保卫{risk['breakeven_threshold_pct']}%→{risk['breakeven_stop_pnl_pct']}% | 时间{risk['time_exit_days']}天")
bt = settings.get("backtest") or {}
print(f"资金: {bt['initial_capital']/10000:.0f}万 | 单票{bt['position_size']/10000:.0f}万 | 连败{bt['streak_pause']}/{bt['pause_days']}天")
print(f"区间: {START} ~ {END}")

engine = BacktestEngine()
t0 = time.time()

result = engine.run(
    strategy_name="ma5_angle",
    strategy_params={"rps_threshold": 0, "breadth_threshold": 0},
    start=START, end=END,
    exchanges=["SH", "SZ"], sectors=[], index_filter=[],
    bj_filter=True,
    sh_red_filter=False,  # 策略内部已处理红盘日
)

elapsed = time.time() - t0
trades = result.trades
n = len(trades)

if n == 0:
    print("\n无交易记录")
    import sys; sys.exit()

wins = [t for t in trades if t["pnl_pct"] > 0]
losses = [t for t in trades if t["pnl_pct"] < 0]
avg_pnl = sum(t["pnl_pct"] for t in trades) / n
wr = len(wins) / (len(wins) + len(losses)) * 100 if (len(wins) + len(losses)) > 0 else 0
avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
pf = abs(avg_win * len(wins) / (avg_loss * len(losses))) if avg_loss != 0 and len(losses) > 0 else 0

trades_sorted = sorted(trades, key=lambda t: t.get("buy_date", ""))
port = 1.0; peak = 1.0; max_dd = 0.0
for t in trades_sorted:
    port *= (1 + t["pnl_pct"] / 100)
    peak = max(peak, port)
    dd = (peak - port) / peak * 100
    max_dd = max(max_dd, dd)

print(f"\n{'='*80}")
print(f"回测结果")
print(f"{'='*80}")
print(f"交易笔数: {n}")
print(f"平均盈亏: {avg_pnl:+.3f}%")
print(f"胜率: {wr:.1f}%")
print(f"盈亏比(PF): {pf:.2f}")
print(f"最大回撤(Nav): {max_dd:.2f}%")
print(f"平均盈利: {avg_win:+.3f}% | 平均亏损: {avg_loss:+.3f}%")
print(f"平均持仓: {sum(t['hold_days'] for t in trades)/n:.1f}天")

pf_init = getattr(result, "portfolio_initial_capital", None)
if pf_init:
    print(f"\n── 投资组合 ──")
    print(f"初始资金: {pf_init:,.0f}元")
    print(f"最终资金: {result.portfolio_final_value:,.0f}元")
    print(f"总收益率: {result.portfolio_total_return:+.2f}%")
    print(f"实际成交: {len(result.portfolio_trades)}笔")
    print(f"跳过信号: {result.portfolio_skipped}个")

# 离场原因
reasons = {}
for t in trades:
    r = t.get("exit_reason", "?")
    reasons[r] = reasons.get(r, 0) + 1
print(f"\n── 离场原因 ──")
for r, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
    print(f"  {r}: {cnt}笔 ({cnt/n*100:.1f}%)")

# 月度
monthly = getattr(result, "portfolio_monthly", None)
if monthly:
    print(f"\n── 月度净值 ──")
    for m in monthly:
        print(f"  {m['month']} | 净值{m['nav']:>10,} | 开{m['entries']:>3} 平{m['closes']:>3} | 盈亏{m['pnl_yuan']:>+10,} | 回撤{m['max_dd_pct']:>5.1f}%")

print(f"\n耗时: {elapsed:.0f}s")
