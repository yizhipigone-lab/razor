"""
模拟盘交易 — 报告生成器
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pandas as pd
import numpy as np
from datetime import date
from typing import Dict, List
from pathlib import Path
from collections import Counter

from app.sim_trader.engine import SimTraderEngine, Trade, Position
from app.sim_trader.config import INITIAL_CAPITAL, SIM_START, SIM_END

OUT_DIR = Path(__file__).parent.parent.parent / "output" / "sim_trader"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def daily_report(today: date, engine: SimTraderEngine, snapshot: dict):
    """生成每日持仓和交易快照"""
    pass  # 逐日运行时按需调用


def final_report(engine: SimTraderEngine, trading_dates: List[date]):
    """回测结束后的完整统计报告"""
    # L4 修复: 从 store 重新加载 trades (回测模式 store=None 时此调用为 no-op;
    # 有 store 时则把内存漏掉的部分从 DB 补齐, 避免仅依赖运行期 in-memory 累加)
    engine.refresh_trades_from_store()
    eq_df = pd.DataFrame(engine.equity_curve)
    if eq_df.empty:
        print("无交易记录")
        return

    fe = eq_df['equity'].iloc[-1]
    tr = (fe / INITIAL_CAPITAL - 1) * 100
    eq_df['cummax'] = eq_df['equity'].cummax()
    eq_df['dd'] = (eq_df['equity'] - eq_df['cummax']) / eq_df['cummax'] * 100
    md = eq_df['dd'].min()
    eq_df['daily_ret'] = eq_df['equity'].pct_change()
    sharpe = (eq_df['daily_ret'].mean() / eq_df['daily_ret'].std() * np.sqrt(252)
              if eq_df['daily_ret'].std() > 0 else 0)
    days = (SIM_END - SIM_START).days
    ann = ((fe / INITIAL_CAPITAL) ** (365.25 / days) - 1) * 100

    trades = engine.trades
    if not trades:
        print("无交易")
        return

    wins = [t for t in trades if t.return_pct > 0]
    loses = [t for t in trades if t.return_pct <= 0]
    n = len(trades)
    nw = len(wins)
    nl = len(loses)
    wr = nw / n * 100
    aw = np.mean([t.return_pct for t in wins]) if wins else 0
    al = np.mean([t.return_pct for t in loses]) if loses else 0
    at_ = np.mean([t.return_pct for t in trades])
    med = np.median([t.return_pct for t in trades])
    tg_ = sum(t.return_pct for t in wins)
    tl_ = abs(sum(t.return_pct for t in loses))
    pf = tg_ / tl_ if tl_ > 0 else float('inf')
    tp = sum(t.profit_amount for t in trades)
    ah = np.mean([t.hold_days for t in trades])
    ed = Counter(t.exit_reason.split('(')[0] for t in trades)

    # 年度
    eq_df['year'] = pd.to_datetime(eq_df['date']).dt.year
    y_agg = eq_df.groupby('year').agg(start=('equity','first'), end=('equity','last'), dd=('dd','min'))
    y_agg['ret'] = (y_agg['end'] / y_agg['start'] - 1) * 100
    for yr in sorted(y_agg.index):
        yr_t = [t for t in trades if t.entry_date.year == yr]
        yr_w = [t for t in yr_t if t.return_pct > 0]
        y_agg.loc[yr, 'trades'] = len(yr_t)
        y_agg.loc[yr, 'wr'] = len(yr_w) / len(yr_t) * 100 if yr_t else 0

    # 月度
    eq_df['month'] = pd.to_datetime(eq_df['date']).dt.to_period('M')
    m_agg = eq_df.groupby('month').agg(start=('equity','first'), end=('equity','last'))
    m_agg['ret'] = (m_agg['end'] / m_agg['start'] - 1) * 100

    # ── 打印 ──────────────────────────────────
    print("\n" + "=" * 64)
    print("  模拟盘交易 — 最终报告")
    print("=" * 64)
    print(f"\n  区间: {SIM_START} ~ {SIM_END}  |  交易日: {len(trading_dates)}")
    print(f"  初始资金: {INITIAL_CAPITAL:>13,}")
    print(f"  最终净值: {fe:>13,.0f}  |  收益: {tr:+.2f}%  |  年化: {ann:+.2f}%")
    print(f"  最大回撤: {md:+.2f}%  |  夏普: {sharpe:.2f}  |  PF: {pf:.2f}")
    print(f"  交易: {n}笔  |  盈: {nw} / 亏: {nl}  |  胜率: {wr:.1f}%")
    print(f"  均盈: {aw:+.2f}%  |  均亏: {al:+.2f}%  |  均笔: {at_:+.2f}%  |  中位: {med:+.2f}%")
    print(f"  均持: {ah:.1f}天  |  总盈利: {tp:+,.0f}")

    print(f"\n  [退出分布]")
    for reason, count in ed.most_common():
        print(f"    {reason:<30} {count:>6} ({count/n*100:5.1f}%)")

    print(f"\n  [年度]")
    print(f"  {'年份':<8} {'收益':>10} {'回撤':>10} {'交易':>8} {'胜率':>8}")
    for yr, row in y_agg.iterrows():
        print(f"  {int(yr):<8} {row['ret']:>+9.2f}% {row['dd']:>9.2f}% "
              f"{int(row.get('trades',0)):>8} {row.get('wr',0):>7.1f}%")

    # ── 保存 ──────────────────────────────────
    eq_df.to_parquet(str(OUT_DIR / "equity.parquet"), index=False)
    if trades:
        pd.DataFrame([{
            'code': t.code, 'entry': str(t.entry_date), 'exit': str(t.exit_date),
            'entry_px': t.entry_price, 'exit_px': t.exit_price, 'shares': t.shares,
            'ret_pct': t.return_pct, 'profit': t.profit_amount,
            'reason': t.exit_reason, 'hold_days': t.hold_days,
        } for t in trades]).to_parquet(str(OUT_DIR / "trades.parquet"), index=False)
        # 也保存 CSV 方便查看
        pd.DataFrame([{
            'code': t.code, 'entry': str(t.entry_date), 'exit': str(t.exit_date),
            'entry_px': t.entry_price, 'exit_px': t.exit_price, 'shares': t.shares,
            'ret_pct': round(t.return_pct, 2), 'profit': round(t.profit_amount, 0),
            'reason': t.exit_reason, 'hold_days': t.hold_days,
        } for t in trades]).to_csv(str(OUT_DIR / "trades.csv"), index=False)
        eq_df.to_csv(str(OUT_DIR / "equity.csv"), index=False)

    print(f"\n  结果已保存至: {OUT_DIR}")
