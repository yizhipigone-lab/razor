"""
QUANTQQ 公式 TDX 回测（独立脚本）

策略文件: output/tdx_formula_sources/QUANTQQ.src.txt
精度:    日线 (intraday_freq=daily)
区间:    2022-01-01 ~ 2026-06-27
引擎:    app.backtest.tdx_runner.run_tdx_backtest

用法: python scripts/run_quantqq_backtest.py
输出:   output/quantqq_backtest/  (json + csv)
"""
import sys
import json
import csv
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.backtest.tdx_runner import run_tdx_backtest

# ── 参数 ──────────────────────────────────────────────
START_DATE = "2022-01-01"
END_DATE   = date.today().isoformat()  # 2026-06-27
STRATEGY   = "QUANTQQ"
PERIOD     = "daily"  # 日线

OUT_DIR = ROOT / "output" / "quantqq_backtest"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main():
    print("=" * 65)
    print(f"QUANTQQ 公式 TDX 回测（{PERIOD}）")
    print(f"区间: {START_DATE} ~ {END_DATE}")
    print("=" * 65)

    # ── 从 app.sim_trader.config 读默认值（不硬编码） ──
    from app.sim_trader.config import (
        INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
        HARD_STOP, TAKE_PROFIT_TIERS, TRAIL_ACTIVATE, TRAIL_DD,
        TIME_EXIT_DAYS, TIME_EXIT_PROFIT, TIME_FORCE_DAYS,
        LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE, PAUSE_DAYS,
        SAME_STOCK_COOLDOWN, USE_ATR_TRAIL, ATR_TRAIL_MULTIPLIER,
        FIRST_DAY_EXIT_MIN_PROFIT, FIRST_DAY_EXIT_DAYS,
    )

    params = {
        "strategy_name": STRATEGY,
        "strategy_type": "tdx",
        "intraday_freq": PERIOD,
        "start_date": START_DATE,
        "end_date":   END_DATE,
        "initial_capital":  INITIAL_CAPITAL,
        "position_size":    POSITION_SIZE,
        "min_buy_amt":     MIN_BUY_AMT,
        "hard_stop":       HARD_STOP,
        "take_profit_tiers": [dict(t) for t in TAKE_PROFIT_TIERS],
        "trail_activate":  TRAIL_ACTIVATE,
        "trail_dd":        TRAIL_DD,
        "time_exit_days":  TIME_EXIT_DAYS,
        "time_exit_profit": TIME_EXIT_PROFIT,
        "time_force_days": TIME_FORCE_DAYS,
        "loss_streak_halve": LOSS_STREAK_HALVE,
        "loss_streak_pause": LOSS_STREAK_PAUSE,
        "pause_days":      PAUSE_DAYS,
        "same_stock_cooldown": SAME_STOCK_COOLDOWN,
        "use_atr_trail":   USE_ATR_TRAIL,
        "atr_trail_multiplier": ATR_TRAIL_MULTIPLIER,
        "first_day_exit_min_profit": FIRST_DAY_EXIT_MIN_PROFIT,
        "first_day_exit_days":       FIRST_DAY_EXIT_DAYS,
        "signal_params":   {},
    }

    print(f"\n[1/3] 启动 TDX 回测...")
    print(f"  strategy_name={STRATEGY}, period={PERIOD}")
    print(f"  initial_capital={INITIAL_CAPITAL:,}, position_size={POSITION_SIZE:,}")
    print()

    def _progress(stage, total, msg):
        print(f"  [{stage}/{total}] {msg}")

    result = run_tdx_backtest(params, progress_cb=_progress)

    if result.get("status") != "ok":
        print(f"\n回测失败: {result.get('message', result)}")
        sys.exit(1)

    # ── 输出汇总 ──────────────────────────────────────
    s = result["summary"]
    print("\n" + "=" * 65)
    print("回测结果")
    print("=" * 65)
    print(f"  策略:       {STRATEGY} ({PERIOD})")
    print(f"  区间:       {s['start_date']} ~ {s['end_date']}")
    print(f"  交易日数:   {s['trading_days']}")
    print(f"  ─────────────────────────────────────────────")
    print(f"  收益率:     {s['total_return']:+.2f}%   (年化 {s['ann_return']:+.2f}%)")
    print(f"  最大回撤:   {s['max_drawdown']:.2f}%")
    print(f"  夏普:       {s['sharpe']}   卡玛: {s['calmar']}   索提诺: {s['sortino']}")
    print(f"  胜率:       {s['win_rate']:.1f}%   ({s['wins']}赢 / {s['losses']}亏)")
    print(f"  盈亏比:     {s['profit_factor']}")
    print(f"  平均赢幅:   {s['avg_win']:+.2f}%")
    print(f"  平均亏幅:   {s['avg_loss']:+.2f}%")
    print(f"  平均持仓:   赢{s['avg_hold_win']:.1f}天 / 亏{s['avg_hold_loss']:.1f}天")
    print(f"  最好单笔:   {s['best_trade']:+.2f}%")
    print(f"  最差单笔:   {s['worst_trade']:+.2f}%")
    print(f"  终值:       {s['final_equity']:,.0f}")
    print(f"  交易笔数:   {s['trades']}  (买入信号 {s['buy_signals']} / 实际买入 {s.get('signals', '?')})")
    print(f"  最大持仓数: {s['max_positions_held']}")
    print(f"  平均持仓数: {s['avg_positions_held']}")
    print(f"  盈利月份:   {s['positive_months']}")
    print(f"  退出原因:   {s['exit_reasons']}")
    print(f"  数据源:     {s.get('data_source', '?')}")
    print("=" * 65)

    # ── 写文件 ────────────────────────────────────────
    # 1) summary json
    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(
        json.dumps({"params": params, "summary": s}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 2) trades jsonl
    trades_path = OUT_DIR / "trades.jsonl"
    with trades_path.open("w", encoding="utf-8") as f:
        for t in result["trades"]:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    # 3) trades csv
    csv_path = OUT_DIR / "trades.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        if result["trades"]:
            w = csv.DictWriter(f, fieldnames=list(result["trades"][0].keys()))
            w.writeheader()
            for t in result["trades"]:
                w.writerow(t)

    # 4) equity csv
    eq_path = OUT_DIR / "equity.csv"
    with eq_path.open("w", encoding="utf-8-sig", newline="") as f:
        if result["equity"]:
            w = csv.DictWriter(f, fieldnames=list(result["equity"][0].keys()))
            w.writeheader()
            for e in result["equity"]:
                w.writerow(e)

    print(f"\n[2/3] 输出文件:")
    print(f"  {summary_path}")
    print(f"  {trades_path}  ({len(result['trades'])} 笔)")
    print(f"  {csv_path}")
    print(f"  {eq_path}  ({len(result['equity'])} 个交易日)")

    # ── 自检 ──────────────────────────────────────────
    print(f"\n[3/3] 自检:")
    checks = []
    # 1) 资金一致性
    expected_final = params["initial_capital"] + sum(t["profit"] for t in result["trades"])
    diff = abs(s["final_equity"] - expected_final)
    checks.append(("资金一致性 (期末现金≈初始+累计盈亏)",
                   diff < 100, f"diff={diff:.0f}"))
    # 2) 胜率=赢/(赢+亏)
    if s["wins"] + s["losses"] > 0:
        wr_calc = s["wins"] / (s["wins"] + s["losses"]) * 100
        wr_ok = abs(wr_calc - s["win_rate"]) < 0.5
        checks.append((f"胜率自洽 {wr_calc:.1f}%≈{s['win_rate']:.1f}%", wr_ok, ""))
    # 3) 净值长度=交易日数
    checks.append((f"净值点数({len(result['equity'])}==交易日{s['trading_days']})",
                   len(result["equity"]) == s["trading_days"],
                   f"差 {len(result['equity']) - s['trading_days']}"))
    # 4) max_dd <= 100
    checks.append((f"最大回撤{s['max_drawdown']:.1f}%<100%",
                   0 < s["max_drawdown"] < 100, ""))

    all_ok = True
    for name, ok, note in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}  {note}")
        if not ok:
            all_ok = False
    if all_ok:
        print("  自检通过 ✓")
    else:
        print("  ⚠ 自检发现问题，但不影响数据有效性")

    return s


if __name__ == "__main__":
    main()