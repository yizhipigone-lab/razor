"""
1分钟线精确回测脚本
使用 BacktestEngine 直接运行，OHLC-aware 仿真
止盈止损参数从 config/app_setting.json 读取
"""
import sys, os, json, time, io
from pathlib import Path
from datetime import date, timedelta

# 强制 UTF-8 输出，避免 GBK 编码错误
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from core.logger import get_logger
from app.backtest.engine import BacktestEngine
from database.duckdb_manager import db

log = get_logger("RunBacktest1m")

STRATEGY = "ma5_angle"
START = date(2025, 4, 25)
END = date(2026, 4, 30)


def main():
    print(f"\n{'='*60}")
    print(f"1分钟线 OHLC 精确回测")
    print(f"策略: {STRATEGY}")
    print(f"区间: {START} ~ {END}  ({(END-START).days} 天)")
    print(f"数据精度: 1分钟线 (OHLC感知)")
    print(f"止盈止损: 从 config/app_setting.json 读取")
    print(f"{'='*60}\n")

    engine = BacktestEngine()

    def progress_cb(step, total, msg):
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] [{step}/{total}] {msg}")

    try:
        t0 = time.time()
        result = engine.run(
            strategy_name=STRATEGY,
            strategy_params={"rps_threshold": 0},
            start=START,
            end=END,
            exchanges=["SH", "SZ"],
            sectors=[],
            index_filter=[],
            min_mv=None,
            max_mv=None,
            progress_callback=progress_cb,
            time_exit_min_pnl=1.0,
        )
        elapsed = time.time() - t0

        # ── 结果汇总 ──────────────────────────────
        print(f"\n{'='*60}")
        print(f"回测完成! 耗时 {elapsed:.0f}s")
        print(f"{'='*60}")

        trades = result.trades
        n = len(trades)
        if n == 0:
            print("⚠️ 无交易记录")
            return

        wins = [t for t in trades if t["pnl_pct"] > 0]
        losses = [t for t in trades if t["pnl_pct"] < 0]
        flat = [t for t in trades if t["pnl_pct"] == 0]

        avg_pnl = sum(t["pnl_pct"] for t in trades) / n
        win_rate = len(wins) / (len(wins) + len(losses)) * 100 if (len(wins) + len(losses)) > 0 else 0
        avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

        # 计算最大回撤（真实净值回撤率）
        max_dd = 0
        portfolio = 1.0; nav_peak = 1.0
        for t in trades:
            portfolio *= (1 + t["pnl_pct"] / 100)
            nav_peak = max(nav_peak, portfolio)
            dd = (nav_peak - portfolio) / nav_peak * 100
            max_dd = max(max_dd, dd)

        pf = abs(avg_win * len(wins) / (avg_loss * len(losses))) if avg_loss != 0 and len(losses) > 0 else 0

        print(f"\n{'指标':<20} {'数值':>10}")
        print(f"{'-'*30}")
        print(f"{'总交易笔数':<20} {n:>10}")
        print(f"{'盈利笔数':<20} {len(wins):>10}")
        print(f"{'亏损笔数':<20} {len(losses):>10}")
        print(f"{'平手笔数':<20} {len(flat):>10}")
        print(f"{'平盈率':<20} {avg_pnl:>+10.3f}%")
        print(f"{'胜率':<20} {win_rate:>10.1f}%")
        print(f"{'平均盈利':<20} {avg_win:>+10.3f}%")
        print(f"{'平均亏损':<20} {avg_loss:>+10.3f}%")
        print(f"{'盈亏比(PF)':<20} {pf:>10.2f}")
        print(f"{'最大回撤':<20} {max_dd:>10.2f}%")
        print(f"{'平均持仓天数':<20} {sum(t['hold_days'] for t in trades)/n:>10.1f}天")

        # 止盈止损分布
        reasons = {}
        for t in trades:
            r = t.get("exit_reason", "未知")
            reasons[r] = reasons.get(r, 0) + 1
        print(f"\n离场原因分布:")
        for r, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  {r}: {cnt} 笔 ({cnt/n*100:.1f}%)")

        # 盈亏分布
        bins = [(-100, -6), (-6, -3), (-3, 0), (0, 3), (3, 5), (5, 10), (10, 100)]
        print(f"\n盈亏分布:")
        for lo, hi in bins:
            cnt = len([t for t in trades if lo <= t["pnl_pct"] < hi])
            print(f"  [{lo:>4}% ~ {hi:>4}%): {cnt:>5} 笔 ({cnt/n*100:5.1f}%)")

        # Save to DB
        try:
            _hist_id = db.save_ai_backtest_history(
                strategy_name=STRATEGY,
                start_date=str(START), end_date=str(END),
                exchanges=["SH", "SZ"], sectors=[],
                index_filter=[], min_mv=None, max_mv=None,
                use_llm=False, n_exploration=0, n_bayesian=0,
                best_avg_pnl=avg_pnl, best_win_rate=win_rate,
                best_params=json.dumps({}, ensure_ascii=False),
                top10_json=json.dumps([{
                    "rank": 1, "avg_pnl": avg_pnl, "win_rate": win_rate,
                    "max_dd": max_dd, "n_trades": n, "pf": pf
                }], ensure_ascii=False),
                wfo_json="[]",
                llm_report=f"1分钟线精确回测 | 分档止盈3/5/10% | 回落3/2% | 硬止损-6% | 时间止盈5天/1%",
                regime_summary=json.dumps(reasons, ensure_ascii=False),
            )
            print(f"\n💾 历史已保存 (id={_hist_id})")
        except Exception as e:
            print(f"\n⚠️ 保存历史失败: {e}")

        # Top 10 盈利/亏损交易
        print(f"\nTop 5 最佳交易:")
        for t in sorted(trades, key=lambda x: -x["pnl_pct"])[:5]:
            print(f"  {t['code']} {t['name']}: {t['pnl_pct']:+.2f}% | 持仓{t['hold_days']}天 | {t.get('exit_reason','?')}")

        print(f"\nTop 5 最差交易:")
        for t in sorted(trades, key=lambda x: x["pnl_pct"])[:5]:
            print(f"  {t['code']} {t['name']}: {t['pnl_pct']:+.2f}% | 持仓{t['hold_days']}天 | {t.get('exit_reason','?')}")

    except Exception as e:
        import traceback
        print(f"\n❌ 回测崩溃: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()
