"""
一次性脚本：灌入 TDX 公式回测结果到模拟盘 JSON Store

用法:
  python scripts/populate_sim_trader.py --strategy QUANTQQ
  python scripts/populate_sim_trader.py --strategy gs_1_GUPIAO_011 --period 5m
  python scripts/populate_sim_trader.py --help

依赖: 通达信客户端已启动
"""
import argparse
import sys, json
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# P0-2 护栏: 运行态账本路径(禁止灌数脚本覆盖)
LIVE_STATE_PATH = (ROOT / "output" / "sim_trader" / "state.json").resolve()


def parse_args():
    p = argparse.ArgumentParser(description="灌入 TDX 公式回测结果到模拟盘")
    p.add_argument("--strategy", default="QUANTQQ",
                   help="TDX 公式名（默认: QUANTQQ）")
    p.add_argument("--period", default="5m", choices=["5m", "1m", "daily"],
                   help="回测精度（默认: 5m）")
    p.add_argument("--start-date", default="2026-01-01",
                   help="开始日期 YYYY-MM-DD（默认: 2026-01-01）")
    p.add_argument("--end-date", default=None,
                   help="结束日期 YYYY-MM-DD（默认: 今天）")
    p.add_argument("--output", default=None,
                   help="输出 JSON 路径（默认: output/sim_trader/imports/{strategy}_state.json）")
    p.add_argument("--force-overwrite-live", action="store_true",
                   help="⚠️危险: 显式允许覆盖运行态 state.json（默认禁止）")
    return p.parse_args()


def main():
    args = parse_args()
    from app.sim_trader.config import (
        INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
        HARD_STOP, TAKE_PROFIT_TIERS, TRAIL_ACTIVATE, TRAIL_DD,
        TIME_EXIT_DAYS, TIME_EXIT_PROFIT, TIME_FORCE_DAYS,
        LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE, PAUSE_DAYS,
        SAME_STOCK_COOLDOWN, USE_ATR_TRAIL, ATR_TRAIL_MULTIPLIER,
        FIRST_DAY_EXIT_MIN_PROFIT, FIRST_DAY_EXIT_DAYS,
    )

    STRATEGY = args.strategy
    PERIOD = args.period
    START_DATE = date.fromisoformat(args.start_date)
    END_DATE = date.fromisoformat(args.end_date) if args.end_date else date.today()

    # 默认输出路径：output/sim_trader/imports/{strategy}_state.json (P0-2: 与运行态隔离)
    OUTPUT_PATH = Path(args.output) if args.output else \
                  ROOT / "output" / "sim_trader" / "imports" / f"{STRATEGY}_state.json"

    params = {
        "strategy_name": STRATEGY,
        "strategy_type": "tdx",
        "intraday_freq": PERIOD,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "initial_capital": INITIAL_CAPITAL,
        "position_size": POSITION_SIZE,
        "min_buy_amt": MIN_BUY_AMT,
        "hard_stop": HARD_STOP,
        "take_profit_tiers": [tier.copy() for tier in TAKE_PROFIT_TIERS],
        "trail_activate": TRAIL_ACTIVATE,
        "trail_dd": TRAIL_DD,
        "time_exit_days": TIME_EXIT_DAYS,
        "time_exit_profit": TIME_EXIT_PROFIT,
        "time_force_days": TIME_FORCE_DAYS,
        "loss_streak_halve": LOSS_STREAK_HALVE,
        "loss_streak_pause": LOSS_STREAK_PAUSE,
        "pause_days": PAUSE_DAYS,
        "same_stock_cooldown": SAME_STOCK_COOLDOWN,
        "use_atr_trail": USE_ATR_TRAIL,
        "atr_trail_multiplier": ATR_TRAIL_MULTIPLIER,
        "first_day_exit_min_profit": FIRST_DAY_EXIT_MIN_PROFIT,
        "first_day_exit_days": FIRST_DAY_EXIT_DAYS,
        "signal_params": {},
    }

    print("=" * 64)
    print(f"  TDX 模拟盘数据灌入")
    print(f"  策略:   {STRATEGY}")
    print(f"  精度:   {PERIOD}")
    print(f"  区间:   {START_DATE} ~ {END_DATE}")
    print(f"  输出:   {OUTPUT_PATH}")
    print("=" * 64)

    # ── 运行 TDX 回测 ──────────────────────────
    print("\n[1/4] 运行 TDX 回测 ...")
    from app.backtest.tdx_runner import run_tdx_backtest

    def _progress(stage, total, msg):
        print(f"  [{stage}/{total}] {msg}", flush=True)

    import time
    t0 = time.time()
    result = run_tdx_backtest(params, progress_cb=_progress)
    t1 = time.time()
    print(f"\n  TDX 回测耗时: {t1-t0:.1f}秒")

    if result is None or result.get("status") != "ok":
        msg = result.get("message", str(result)) if result is not None else "No result returned"
        print(f"\n[ERROR] 回测失败: {msg}")
        sys.exit(1)

    # ── 写入 JSON Store ───────────────────────
    print("\n[2/4] 转换格式并写入 state.json ...")
    write_to_json_store(result, params['initial_capital'], OUTPUT_PATH,
                        force_overwrite_live=args.force_overwrite_live)

    # ── 打印详细摘要 ─────────────────────────
    print("\n[3/4] 详细统计:")
    print_detailed_stats(result)

    # ── 打印基本摘要 ─────────────────────────
    print("\n[4/4] 完成:")
    s = result["summary"]
    print(f"  策略:       {STRATEGY}")
    print(f"  精度:       {PERIOD}")
    print(f"  区间:       {s.get('start_date')} ~ {s.get('end_date')}")
    print(f"  交易笔数:   {len(result['trades'])}/{s.get('signals','?')}")
    print(f"  收益率:     {s['total_return']:+.2f}%  胜率: {s['win_rate']:.1f}%")
    print(f"  最大回撤:   {s['max_drawdown']:.2f}%  夏普: {s['sharpe']}")
    print(f"  数据源:     {s.get('data_source', '?')}")
    print(f"\n  → 已写入: {OUTPUT_PATH}")
    print(f"  → 刷新前端交易控制 TAB 即可查看")


def write_to_json_store(result: dict, initial_capital: float, output_path: Path,
                        force_overwrite_live: bool = False):
    """将 run_tdx_backtest 结果写入 JsonSimStore 格式"""
    from app.sim_trader.store import JsonSimStore

    # P0-2 护栏: 禁止覆盖运行态账本(除非显式 --force-overwrite-live)
    output_abs = Path(output_path).resolve()
    if output_abs == LIVE_STATE_PATH and not force_overwrite_live:
        raise RuntimeError(
            "❌ 禁止覆盖运行态账本\n"
            f"   路径: {LIVE_STATE_PATH}\n"
            "   原因: TDX 回测数据会污染真实模拟盘(2026-06 数据污染事件根因)\n"
            "   请用 --output 指定独立路径(如 output/sim_trader/imports/)，\n"
            "   或确实要覆盖时显式传 --force-overwrite-live")

    # 自动创建输出目录(如 imports/)
    output_abs.parent.mkdir(parents=True, exist_ok=True)

    store = JsonSimStore(path=str(output_path))
    store._data = {}

    trades_src = result["trades"]
    equity_src = result["equity"]

    # 1) 交易记录
    trades_out = []
    for t in trades_src:
        trades_out.append({
            "code": t["code"],
            "entry_date": str(t["entry_date"]),
            "exit_date": str(t["exit_date"]),
            "entry_price": float(t["entry_px"]),
            "exit_price": float(t["exit_px"]),
            "shares": int(t["shares"]),
            "ret_pct": float(t["ret_pct"]),
            "profit": float(t["profit"]),
            "reason": t["reason"],
            "hold_days": int(t["hold_days"]),
            "entry_time": str(t.get("entry_time", "09:30")),
            "exit_time": str(t.get("exit_time", "15:00")),
        })
    store._data["trades"] = trades_out

    # 2) 净值曲线
    equity_out = []
    for e in equity_src:
        equity_out.append({
            "date": str(e["date"]),
            "equity": float(e["equity"]),
            "cash": float(e["cash"]),
            "pos": int(e["pos"]),
        })
    store._data["equity_curve"] = equity_out

    # 3) 终态
    cash_end = float(equity_src[-1]["cash"]) if equity_src else float(initial_capital)
    store._data["state"] = {
        "cash": cash_end,
        "consecutive_losses": 0,
        "pause_until": None,
        "trade_count": len(trades_out),
    }

    # 4) 持仓 + 5) snap
    store._data["positions"] = {}
    store._data["prev_day_snap"] = {}

    store._save()

    # 校验
    stored = json.loads(json.dumps(store._data, default=str))
    loaded_trades = stored.get("trades", [])
    loaded_equity = stored.get("equity_curve", [])
    loaded_state = stored.get("state", {})
    assert len(loaded_trades) == len(trades_src), \
        f"交易数不一致: saved={len(loaded_trades)} src={len(trades_src)}"
    assert len(loaded_equity) == len(equity_src), \
        f"净值点数不一致: saved={len(loaded_equity)} src={len(equity_src)}"
    assert abs(loaded_state.get("cash", 0) - cash_end) < 2.0, \
        f"终值现金不一致: saved={loaded_state.get('cash')} expected={cash_end}"
    print(f"  校验通过: {len(loaded_trades)}笔交易, {len(loaded_equity)}T净值")


def print_detailed_stats(result: dict):
    """从 result 计算并打印丰富的统计信息"""
    from collections import Counter

    trades = result["trades"]
    equity = result["equity"]
    summary = result["summary"]

    if not trades:
        print("  无交易记录")
        return

    # ── 信号 → 交易转化率 ──
    total_signals = summary.get("signals", summary.get("buy_signals", 0))
    if total_signals:
        rate = len(trades) / total_signals * 100
        print(f"  信号转化率:     {len(trades)}/{total_signals} = {rate:.2f}%")

    # ── 退出原因分布 ──
    reasons = Counter(t["reason"] for t in trades)
    print(f"  退出原因分布:")
    for r, c in reasons.most_common():
        pct = c / len(trades) * 100
        print(f"    {r:<12} {c:>5} 笔  {pct:>5.1f}%")

    # ── 持仓时长分布 ──
    hold_days = [int(t["hold_days"]) for t in trades]
    hold_dist = Counter(hold_days)
    print(f"  持仓天数分布:")
    for d in sorted(hold_dist.keys()):
        c = hold_dist[d]
        bar = "#" * min(40, c * 40 // max(hold_dist.values()))
        print(f"    {d:>2}天: {c:>4} 笔 {bar}")

    # ── 月度收益 ──
    monthly = {}
    for t in trades:
        key = str(t["exit_date"])[:7]
        monthly[key] = monthly.get(key, 0) + float(t["profit"])
    print(f"  月度盈亏:")
    for m in sorted(monthly.keys()):
        p = monthly[m]
        sign = "+" if p >= 0 else ""
        print(f"    {m}: {sign}{p:>10,.0f}")

    # ── 单笔最大盈亏 ──
    sorted_by_profit = sorted(trades, key=lambda t: float(t["profit"]), reverse=True)
    print(f"  Top 3 单笔盈利:")
    for t in sorted_by_profit[:3]:
        print(f"    {t['code']:<6} {t['entry_date']} -> {t['exit_date']}  "
              f"+{float(t['profit']):>8,.0f} (+{float(t['ret_pct']):.1f}%)  {t['reason']}")
    print(f"  Worst 3 单笔:")
    for t in sorted_by_profit[-3:]:
        print(f"    {t['code']:<6} {t['entry_date']} -> {t['exit_date']}  "
              f"{float(t['profit']):>9,.0f} ({float(t['ret_pct']):.1f}%)  {t['reason']}")

    # ── 资金轨迹（最大/最小净值日）──
    if equity:
        max_eq = max(equity, key=lambda e: float(e["equity"]))
        min_eq = min(equity, key=lambda e: float(e["equity"]))
        print(f"  资金轨迹:")
        print(f"    最高净值日: {max_eq['date']}  {float(max_eq['equity']):>14,.0f}")
        print(f"    最低净值日: {min_eq['date']}  {float(min_eq['equity']):>14,.0f}")


if __name__ == "__main__":
    main()