"""
AI 参数优化 — 1分钟线回测
搜索空间从 config/app_setting.json 读取
"""
import sys, os, io, time
from pathlib import Path
from datetime import date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from core.logger import get_logger
from app.backtest.ai_optimizer import AIBacktestOptimizer

log = get_logger("RunAIOpt")

STRATEGY = "ma5_angle"
START = date(2025, 4, 25)
END = date(2026, 4, 30)


def main():
    print(f"\n{'='*60}")
    print(f"AI 参数优化 — 1分钟线精确回测")
    print(f"策略: {STRATEGY}  区间: {START} ~ {END}")
    print(f"搜索空间: 从 config/app_setting.json 读取")
    print(f"LLM: 关闭 (纯数学优化)")
    print(f"{'='*60}\n")

    opt = AIBacktestOptimizer(use_llm=False, n_exploration=32, n_bayesian=20)

    def log_cb(msg):
        print(f"  {msg}")

    t0 = time.time()

    opt.run(
        strategy_name=STRATEGY,
        strategy_params={"rps_threshold": 0},
        start=START,
        end=END,
        exchanges=["SH", "SZ"],
        sectors=[],
        index_filter=[],
        min_mv=None,
        max_mv=None,
        log_callback=log_cb,
    )

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"AI 优化完成! 总耗时 {elapsed:.0f}s ({elapsed/60:.1f}min)")

    # 读取最终状态
    from app.backtest.ai_optimizer import get_task_state
    state = get_task_state()
    top10 = state.get("top10", [])

    if top10:
        print(f"\n{'─'*50}")
        print(f"Top-10 参数组合:")
        print(f"{'─'*50}")
        for i, r in enumerate(top10[:5]):
            print(f"\n#{i+1} | 平盈率: {r['avg_pnl']:+.3f}% | 胜率: {r['win_rate']:.1f}% | "
                  f"最大回撤: {r['max_dd']:.2f}% | PF: {r.get('profit_factor','N/A')} | "
                  f"WFE: {r.get('wfe','N/A')}")
            p = r.get("params", {})
            print(f"  参数: TP1={p.get('tp1_profit','?')}%@{p.get('tp1_ratio','?')}  "
                  f"TP2={p.get('tp2_profit','?')}%@{p.get('tp2_ratio','?')}  "
                  f"SL={p.get('hard_stop_loss_pct','?')}%  "
                  f"Trail={p.get('trailing_activate_pct','?')}/{p.get('trailing_drawdown_pct','?')}%  "
                  f"BE={p.get('breakeven_threshold_pct','?')}/{p.get('breakeven_stop_pnl_pct','?')}%  "
                  f"Days={p.get('time_exit_days','?')}")

        print(f"\n{'='*60}")
        llm_report = state.get("llm_report", "")
        if llm_report:
            print(llm_report)

    # 保存结果
    from database.duckdb_manager import db
    try:
        best = top10[0] if top10 else {}
        bp = best.get("params", {})
        _hist_id = db.save_ai_backtest_history(
            strategy_name=STRATEGY,
            start_date=str(START), end_date=str(END),
            exchanges=["SH", "SZ"], sectors=[],
            index_filter=[], min_mv=None, max_mv=None,
            use_llm=False, n_exploration=8, n_bayesian=20,
            best_avg_pnl=best.get("avg_pnl", 0),
            best_win_rate=best.get("win_rate", 0),
            best_params=str(bp),
            top10_json=str(top10[:5]),
            wfo_json=str(state.get("wfo_results", [])),
            llm_report=state.get("llm_report", ""),
            regime_summary=str(state.get("regime_summary", {})),
        )
        print(f"\n💾 历史已保存 (id={_hist_id})")
    except Exception as e:
        print(f"\n⚠️ 保存历史失败: {e}")


if __name__ == "__main__":
    main()
