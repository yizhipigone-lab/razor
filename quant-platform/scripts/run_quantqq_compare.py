"""
QUANTQQ 报告参数 vs V2网格参数 对比回测
目的：用报告的9规则参数（双档止盈+保本止损+hard_stop=-4.6%）
      对比 V2 简化参数（单档止盈，无保本止损）

报告参数来源：docs/_research/QUANTQQ_独立回测_2020-2026.md 第一节A
V2 最优：移动止盈2% / 止损-3%（来自 quantqq_param_search_v2.json）
"""
import sys
import json
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.backtest.tdx_runner import run_tdx_backtest

OUT_DIR = ROOT / "output" / "quantqq_compare"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def run_one(name, params, days_label):
    print(f"\n{'='*60}")
    print(f"[{name}] 区间: {days_label}")
    print(f"{'='*60}")

    t0 = time.time()
    result = run_tdx_backtest(params)
    elapsed = time.time() - t0

    if result.get("status") != "ok":
        print(f"  ERROR: {result.get('message', 'unknown')}")
        return None, elapsed

    s = result["summary"]
    print(f"  耗时:{elapsed:.1f}s | "
          f"收益:{s['total_return']:+.2f}% | "
          f"年化:{s['ann_return']:+.2f}% | "
          f"回撤:{s['max_drawdown']:.2f}% | "
          f"夏普:{s['sharpe']:.2f} | "
          f"胜率:{s['win_rate']:.1f}% | "
          f"交易:{s['trades']}")
    return s, elapsed


def make_params(start, end, **overrides):
    """从 sim_trader.config 读默认参数，再按需覆盖。"""
    from app.sim_trader.config import (
        INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
        HARD_STOP, TAKE_PROFIT_TIERS, TRAIL_ACTIVATE, TRAIL_DD,
        TIME_EXIT_DAYS, TIME_EXIT_PROFIT, TIME_FORCE_DAYS,
        LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE, PAUSE_DAYS,
        SAME_STOCK_COOLDOWN, USE_ATR_TRAIL, ATR_TRAIL_MULTIPLIER,
        FIRST_DAY_EXIT_MIN_PROFIT, FIRST_DAY_EXIT_DAYS,
    )
    p = {
        "strategy_name": "QUANTQQ",
        "strategy_type": "tdx",
        "intraday_freq": "daily",
        "start_date": start,
        "end_date": end,
        "initial_capital": INITIAL_CAPITAL,
        "position_size": POSITION_SIZE,
        "min_buy_amt": MIN_BUY_AMT,
        "hard_stop": HARD_STOP,
        "take_profit_tiers": [dict(t) for t in TAKE_PROFIT_TIERS],
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
    p.update(overrides)
    return p


def main():
    # ── 区间定义 ──────────────────────────────────────────
    # 2024H1：牛市（已知最优参数在此区间表现好）
    S1_START, S1_END = "2024-01-01", "2024-06-30"
    # 2022~2024全区间：含熊市（2022）+ 震荡（2023）+ 牛市（2024）
    S2_START, S2_END = "2022-01-01", "2024-06-30"
    # 2020~2024：更长区间（报告用的是这个）
    S3_START, S3_END = "2020-01-01", "2024-06-30"

    # ── 参数方案 ──────────────────────────────────────────
    # 方案A：报告的9规则参数（双档止盈+保本止损）
    params_report = lambda s, e: make_params(s, e,
        hard_stop=-0.046,
        take_profit_tiers=[
            {"profit_pct": 0.027, "sell_ratio": 0.20},   # TP1: +2.7% 卖 20%
            {"profit_pct": 0.130, "sell_ratio": 0.60},   # TP2: +13%  卖 60%
        ],
        trail_activate=0.039,   # 移动止盈激活 3.9%
        trail_dd=0.017,         # 移动止盈回撤 1.7%
    )

    # 方案B：V2最优——移动止盈2%（简化参数，单档止盈，无保本止损）
    params_v2_best = lambda s, e: make_params(s, e,
        hard_stop=-0.06,
        take_profit_tiers=[{"profit_pct": 0.03, "sell_ratio": 0.30}],  # TP1=3%卖30%
        trail_activate=0.02,    # 移动止盈激活 2%（V2最优）
        trail_dd=0.015,
    )

    # 方案C：V2第二优——止损-3%（简化参数）
    params_v2_2 = lambda s, e: make_params(s, e,
        hard_stop=-0.03,       # 极宽松止损（V2第2名）
        take_profit_tiers=[{"profit_pct": 0.03, "sell_ratio": 0.30}],
        trail_activate=0.05,
        trail_dd=0.02,
    )

    # 方案D：原始默认参数（基准对比）
    params_default = lambda s, e: make_params(s, e)

    # ── 执行 ──────────────────────────────────────────────
    cases = [
        ("报告9规则(双TP+BE)",  params_report),
        ("V2最优(移动止盈2%)",  params_v2_best),
        ("V2第二(止损-3%)",     params_v2_2),
        ("系统默认参数",         params_default),
    ]
    intervals = [
        ("2024H1牛市",   S1_START, S1_END),
        ("2022-2024全区间", S2_START, S2_END),
        # ("2020-2024长区间", S3_START, S3_END),  # 太长，省时间
    ]

    results = {}   # {(case_name, interval_name): summary}

    for i_label, i_start, i_end in intervals:
        print(f"\n\n{'#'*70}")
        print(f"## 区间: {i_label}  ({i_start} ~ {i_end})")
        print(f"{'#'*70}")
        for case_name, params_fn in cases:
            s, elapsed = run_one(case_name, params_fn(i_start, i_end), i_label)
            if s:
                results[(case_name, i_label)] = s

    # ── 汇总对比表 ────────────────────────────────────────
    print("\n\n" + "="*80)
    print("对比汇总（按 2022-2024全区间 年化排序）")
    print("="*80)
    hdr = f"{'方案':<22} {'区间':<12} {'总收益':>9} {'年化':>8} {'回撤':>9} {'夏普':>6} {'胜率':>6} {'交易数':>8}"
    print(hdr)
    print("-"*80)

    # 先按全区间年化排，再按H1
    full_key = "2022-2024全区间"
    sorted_cases = sorted(
        [(cn, s) for (cn, il), s in results.items() if il == full_key],
        key=lambda x: -x[1]["ann_return"]
    )

    for case_name, s in sorted_cases:
        h1_label = "2024H1牛市"
        s_h1 = results.get((case_name, h1_label))
        ann_h1 = f"{s_h1['ann_return']:+.1f}%" if s_h1 else "N/A"

        print(f"{case_name:<22} {full_key:<12} "
              f"{s['total_return']:>+8.1f}% {s['ann_return']:>+7.1f}% {s['max_drawdown']:>8.1f}% "
              f"{s['sharpe']:>5.2f} {s['win_rate']:>5.1f}% {s['trades']:>7}  "
              f"[H1年化:{ann_h1}]")

    print("-"*80)

    # ── 核心结论 ──────────────────────────────────────────
    print("\n=== 核心结论 ===")
    best_full = sorted_cases[0][0] if sorted_cases else ""
    best_h1 = "N/A"
    if results:
        h1_sorted = sorted(
            [(cn, s) for (cn, il), s in results.items() if il == "2024H1牛市"],
            key=lambda x: -x[1]["ann_return"]
        )
        if h1_sorted:
            best_h1 = h1_sorted[0][0]

    print(f"2022-2024全区间最优: {best_full}")
    print(f"2024H1牛市最优:     {best_h1}")

    # 保存
    out = {
        "cases": [cn for cn, _ in cases],
        "intervals": [il for il, _, _ in intervals],
        "results": {f"{k[0]}|||{k[1]}": v for k, v in results.items()},
    }
    out_path = OUT_DIR / "comparison.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n结果已存: {out_path}")


if __name__ == "__main__":
    main()
