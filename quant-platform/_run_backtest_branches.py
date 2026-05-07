"""
三分支回测脚本
分支1: 全市场，去*ST 去北交所
分支2: 全市场，去*ST 去北交所，仅上证红盘日

全部启用: 1分钟线仿真 + 交易成本(滑点0.1%+佣金+印花税) + RPS>80
硬止损: -9%
"""
import sys, os, json, time, io
from pathlib import Path
from datetime import date, datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from core.logger import get_logger
from app.backtest.engine import BacktestEngine
from database.duckdb_manager import db

log = get_logger("Branches")

STRATEGY = "ma5_angle"
START = date(2025, 4, 25)
END = date(2026, 4, 30)

BRANCHES = [
    {
        "id": 1,
        "label": "全市场(去ST/北交所)",
        "index_filter": [],
        "bj_filter": True,
        "exchanges": ["SH", "SZ"],
        "sh_red_filter": False,
        "rps_threshold": 80,
    },
    {
        "id": 2,
        "label": "全市场(去ST/北交所/上证红盘)",
        "index_filter": [],
        "bj_filter": True,
        "exchanges": ["SH", "SZ"],
        "sh_red_filter": True,
        "rps_threshold": 80,
    },
]


def analyze_trades(trades, label):
    """分析交易结果"""
    n = len(trades)
    if n == 0:
        return {"n": 0, "avg_pnl": 0, "win_rate": 0, "pf": 0, "max_dd": 0, "avg_hold": 0, "reasons": {}}

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] < 0]

    avg_pnl = sum(t["pnl_pct"] for t in trades) / n
    win_rate = len(wins) / (len(wins) + len(losses)) * 100 if (len(wins) + len(losses)) > 0 else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

    # 按买入日期排序，保证净值曲线时序正确
    trades_sorted = sorted(trades, key=lambda t: t.get("buy_date", ""))

    # 真实净值回撤率：模拟复利净值曲线
    portfolio = 1.0; nav_peak = 1.0; nav_max_dd = 0.0
    for t in trades_sorted:
        portfolio *= (1 + t["pnl_pct"] / 100)
        nav_peak = max(nav_peak, portfolio)
        dd = (nav_peak - portfolio) / nav_peak * 100
        nav_max_dd = max(nav_max_dd, dd)

    pf = abs(avg_win * len(wins) / (avg_loss * len(losses))) if avg_loss != 0 and len(losses) > 0 else 0
    avg_hold = sum(t["hold_days"] for t in trades) / n

    reasons = {}
    for t in trades:
        r = t.get("exit_reason", "未知")
        reasons[r] = reasons.get(r, 0) + 1

    return {
        "n": n, "avg_pnl": avg_pnl, "win_rate": win_rate, "pf": pf,
        "max_dd": nav_max_dd, "avg_win": avg_win, "avg_loss": avg_loss,
        "avg_hold": avg_hold, "reasons": reasons,
    }


def print_report(branch, stats):
    """打印单分支报告"""
    print(f"\n{'─'*60}")
    print(f"分支 {branch['id']}: {branch['label']}")
    print(f"{'─'*60}")
    if stats["n"] == 0:
        print("  ⚠️ 无交易记录")
        return

    print(f"  交易笔数:     {stats['n']:>8}")
    print(f"  平均盈亏:     {stats['avg_pnl']:>+8.3f}%")
    print(f"  胜率:         {stats['win_rate']:>8.1f}%")
    print(f"  盈亏比(PF):   {stats['pf']:>8.2f}")
    print(f"  最大回撤:     {stats['max_dd']:>8.2f}%")
    print(f"  平均盈利:     {stats['avg_win']:>+8.3f}%")
    print(f"  平均亏损:     {stats['avg_loss']:>+8.3f}%")
    print(f"  平均持仓:     {stats['avg_hold']:>8.1f}天")

    # 投资组合级别
    pf_return = stats.get("pf_return", 0)
    pf_skipped = stats.get("pf_skipped", 0)
    if pf_return or pf_skipped:
        print(f"\n  ── 投资组合(100万初始) ──")
        print(f"  最终资金:     {stats.get('pf_final', 0):>12,.0f}元")
        print(f"  总收益率:     {pf_return:>+10.2f}%")
        print(f"  因资金不足跳过: {pf_skipped:>8} 个信号")

    print(f"\n  离场原因分布:")
    for r, cnt in sorted(stats["reasons"].items(), key=lambda x: -x[1]):
        print(f"    {r}: {cnt} 笔 ({cnt/stats['n']*100:.1f}%)")


def _print_monthly_table(monthly: list):
    """打印月度净值变化表"""
    print(f"\n  ── 月度净值变化 ──")
    print(f"  {'月份':<8} {'月末净值':>12} {'新开':>5} {'平仓':>5} {'月度盈亏':>12} {'月内回撤':>8}")
    print(f"  {'─'*8} {'─'*12} {'─'*5} {'─'*5} {'─'*12} {'─'*8}")
    for m in monthly:
        nav_str = f"{m['nav']:>12,}"
        pnl_str = f"{m['pnl_yuan']:>+12,}" if m['pnl_yuan'] != 0 else f"{0:>12}"
        print(f"  {m['month']:<8} {nav_str} {m['entries']:>5} {m['closes']:>5} {pnl_str} {m['max_dd_pct']:>7.1f}%")


def main():
    print(f"\n{'='*70}")
    print(f"双分支回测 — 实盘仿真模式")
    print(f"策略: {STRATEGY} | RPS>80 | 硬止损-9%")
    print(f"成本: 滑点0.1% + 佣金0.025% + 印花税0.05%")
    print(f"区间: {START} ~ {END} ({(END-START).days} 天)")
    print(f"数据: 1分钟线 OHLC 精确仿真")
    print(f"{'='*70}")

    engine = BacktestEngine()
    all_stats = []

    for branch in BRANCHES:
        bid = branch["id"]
        label = branch["label"]

        def progress_cb(step, total, msg):
            ts = time.strftime("%H:%M:%S")
            print(f"  [{ts}] [B{bid} {step}/{total}] {msg}")

        t0 = time.time()

        try:
            result = engine.run(
                strategy_name=STRATEGY,
                strategy_params={"rps_threshold": branch["rps_threshold"], "breadth_threshold": 0},
                start=START,
                end=END,
                exchanges=branch["exchanges"],
                sectors=[],
                index_filter=branch["index_filter"],
                min_mv=None,
                max_mv=None,
                progress_callback=progress_cb,
                intraday_freq="min1",
                time_exit_min_pnl=1.0,
                apply_costs=True,
                bj_filter=branch["bj_filter"],
                sh_red_filter=branch.get("sh_red_filter", False),
                use_portfolio=True,
                initial_capital=1_000_000,
                position_size=50_000,
                streak_pause=3,
                pause_days=2,
            )
        except Exception as e:
            import traceback
            print(f"  ❌ 分支{branch['id']} 崩溃: {e}")
            traceback.print_exc()
            continue

        elapsed = time.time() - t0
        trades = result.trades
        stats = analyze_trades(trades, label)
        stats["elapsed"] = elapsed
        stats["label"] = label

        # 投资组合级别指标
        pf_initial = getattr(result, "portfolio_initial_capital", None)
        if pf_initial:
            stats["pf_initial"] = pf_initial
            stats["pf_final"] = result.portfolio_final_value
            stats["pf_return"] = result.portfolio_total_return
            stats["pf_skipped"] = result.portfolio_skipped
        else:
            stats["pf_initial"] = 0
            stats["pf_final"] = 0
            stats["pf_return"] = 0
            stats["pf_skipped"] = 0

        # 月度统计
        monthly = getattr(result, "portfolio_monthly", None)
        stats["monthly"] = monthly

        all_stats.append(stats)

        print_report(branch, stats)
        if monthly:
            _print_monthly_table(monthly)
        print(f"  耗时: {elapsed:.0f}s")

    # ── 汇总对比表 ──────────────────────────────────
    print(f"\n{'='*90}")
    print(f"双分支对比汇总")
    print(f"{'='*90}")
    print(f"{'指标':<20} {'分支1(全市场)':>16} {'分支2(红盘)':>16}")
    print(f"{'-'*20} {'-'*16} {'-'*16} {'-'*16}")
    metrics = [
        ("交易笔数", "n", ">8"),
        ("平均盈亏(%)", "avg_pnl", "+8.3f"),
        ("胜率(%)", "win_rate", "8.1f"),
        ("盈亏比(PF)", "pf", "8.2f"),
        ("最大回撤(%)", "max_dd", "8.2f"),
        ("平均盈利(%)", "avg_win", "+8.3f"),
        ("平均亏损(%)", "avg_loss", "+8.3f"),
        ("平均持仓(天)", "avg_hold", "8.1f"),
        ("组合总收益(%)", "pf_return", "+8.2f"),
        ("跳过信号", "pf_skipped", ">8"),
    ]
    for name, key, fmt in metrics:
        vals = " ".join(f"{s[key]:{fmt}}" if s["n"] > 0 else f"{'N/A':>16}" for s in all_stats)
        print(f"{name:<20} {vals}")

    # Save to DB
    for s in all_stats:
        if s["n"] > 0:
            try:
                hist_id = db.save_ai_backtest_history(
                    strategy_name=f"{STRATEGY}_B{s.get('label','?')}",
                    start_date=str(START), end_date=str(END),
                    exchanges=["SH", "SZ"], sectors=[],
                    index_filter=[], min_mv=None, max_mv=None,
                    use_llm=False, n_exploration=0, n_bayesian=0,
                    best_avg_pnl=s["avg_pnl"], best_win_rate=s["win_rate"],
                    best_params=json.dumps({"hard_stop_loss_pct": -9.0, "costs": True}, ensure_ascii=False),
                    top10_json=json.dumps([{
                        "rank": 1, "avg_pnl": s["avg_pnl"], "win_rate": s["win_rate"],
                        "max_dd": s["max_dd"], "n_trades": s["n"], "pf": s["pf"]
                    }], ensure_ascii=False),
                    wfo_json="[]",
                    llm_report=f"三分支回测|{s['label']}|成本模型|RPS80|硬止损-9%",
                    regime_summary=json.dumps(s["reasons"], ensure_ascii=False),
                )
                print(f"  💾 {s['label']} 已保存 (id={hist_id})")
            except Exception as e:
                print(f"  ⚠️ {s['label']} 保存失败: {e}")


if __name__ == "__main__":
    main()
