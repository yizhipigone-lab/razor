"""Round 1: 系统化参数搜索 - 退出规则优化 + 入场过滤调整"""
import sys, io, time, json
from pathlib import Path
from datetime import date
from itertools import product

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from core.settings import settings
from app.backtest.engine import BacktestEngine

START = date(2025, 4, 25)
END = date(2026, 5, 2)

def run_one(label, strategy_params, risk_overrides):
    """Run a single backtest with overridden risk params"""
    # Save original
    orig_risk = settings.get("risk") or {}
    # Apply overrides
    for k, v in risk_overrides.items():
        settings.set("risk", k, v, save=False)
    settings.save()

    engine = BacktestEngine()
    result = engine.run(
        strategy_name="ma5_angle",
        strategy_params=strategy_params,
        start=START, end=END,
        exchanges=["SH", "SZ"], sectors=[], index_filter=[],
        bj_filter=True, sh_red_filter=False,
    )

    trades = result.trades
    n = len(trades)
    if n == 0:
        return {"label": label, "n": 0, "ret": 0}

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] < 0]
    avg_pnl = sum(t["pnl_pct"] for t in trades) / n
    wr = len(wins)/(len(wins)+len(losses))*100 if (len(wins)+len(losses))>0 else 0
    avg_win = sum(t["pnl_pct"] for t in wins)/len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses)/len(losses) if losses else 0
    pf = abs(avg_win*len(wins)/(avg_loss*len(losses))) if avg_loss!=0 and len(losses)>0 else 0

    pf_ret = result.portfolio_total_return if hasattr(result,'portfolio_total_return') else 0
    pf_skip = result.portfolio_skipped if hasattr(result,'portfolio_skipped') else 0
    funded = len(result.portfolio_trades) if hasattr(result,'portfolio_trades') and result.portfolio_trades else n

    monthly = getattr(result, "portfolio_monthly", None)
    max_mdd = 0
    if monthly:
        for m in monthly:
            max_mdd = max(max_mdd, m.get("max_dd_pct", 0))

    return {
        "label": label, "n": n, "funded": funded, "skipped": pf_skip,
        "ret": pf_ret, "avg_pnl": avg_pnl, "wr": wr, "pf": pf,
        "avg_win": avg_win, "avg_loss": avg_loss, "max_mdd": max_mdd,
    }

# Baseline - current config
print("=" * 100)
print("Round 1: 退出规则优化 + 入场过滤调整")
print("=" * 100)

configs = [
    # (label, strategy_params, risk_overrides)
    # Baseline
    ("0-Baseline",
     {"rps_threshold":0,"breadth_threshold":0},
     {}),

    # Round 1A: Remove entry filters
    ("1A-去均线排列",
     {"rps_threshold":0,"breadth_threshold":0,"use_ma_align":False},
     {}),
    ("1B-去ADX",
     {"rps_threshold":0,"breadth_threshold":0,"use_adx":False},
     {}),
    ("1C-去均线+ADX",
     {"rps_threshold":0,"breadth_threshold":0,"use_ma_align":False,"use_adx":False},
     {}),

    # Round 1D: Relax volume
    ("1D-量比1.5x",
     {"rps_threshold":0,"breadth_threshold":0,"vol_threshold":1.5},
     {}),
    ("1E-量比1.2x+去均线ADX",
     {"rps_threshold":0,"breadth_threshold":0,"vol_threshold":1.2,"use_ma_align":False,"use_adx":False},
     {}),

    # Round 2: Tighter exits
    ("2A-硬止损-6%",
     {"rps_threshold":0,"breadth_threshold":0},
     {"hard_stop_loss_pct":-6.0}),
    ("2B-硬止损-7%+移动8/2.5",
     {"rps_threshold":0,"breadth_threshold":0},
     {"hard_stop_loss_pct":-7.0,"trailing_stop_activate_pct":8.0,"trailing_stop_drawdown_pct":2.5}),
    ("2C-移动6/3+保卫5/1+时间6天",
     {"rps_threshold":0,"breadth_threshold":0},
     {"trailing_stop_activate_pct":6.0,"trailing_stop_drawdown_pct":3.0,
      "breakeven_threshold_pct":5.0,"breakeven_stop_pnl_pct":1.0,
      "time_exit_days":6}),

    # Round 3: Combined - best filters + best exits
    ("3A-去均线ADX+硬止损-6%+移动6/3",
     {"rps_threshold":0,"breadth_threshold":0,"use_ma_align":False,"use_adx":False,"vol_threshold":1.5},
     {"hard_stop_loss_pct":-6.0,"trailing_stop_activate_pct":6.0,"trailing_stop_drawdown_pct":3.0,
      "breakeven_threshold_pct":5.0,"breakeven_stop_pnl_pct":1.0,"time_exit_days":6}),

    ("3B-去均线ADX+硬止损-7%+移动8/2+时间7",
     {"rps_threshold":0,"breadth_threshold":0,"use_ma_align":False,"use_adx":False,"vol_threshold":1.5},
     {"hard_stop_loss_pct":-7.0,"trailing_stop_activate_pct":8.0,"trailing_stop_drawdown_pct":2.0,
      "breakeven_threshold_pct":6.0,"breakeven_stop_pnl_pct":1.5,"time_exit_days":7}),
]

results = []
for label, strat_p, risk_p in configs:
    print(f"\n{'='*60}")
    print(f"Running: {label}")
    print(f"  Strategy: {strat_p}")
    if risk_p:
        print(f"  Risk overrides: {risk_p}")
    t0 = time.time()
    r = run_one(label, strat_p, risk_p)
    r["time"] = time.time() - t0
    results.append(r)
    print(f"  -> 交易{r['n']}笔 | 平盈{r['avg_pnl']:+.3f}% | 胜率{r['wr']:.1f}% | PF={r['pf']:.2f}")
    print(f"  -> 组合收益{r['ret']:+.2f}% | 成交{r['funded']}笔 | 跳过{r['skipped']} | 月回撤{r['max_mdd']:.1f}%")

# Restore baseline
for k in ["hard_stop_loss_pct","trailing_stop_activate_pct","trailing_stop_drawdown_pct",
          "breakeven_threshold_pct","breakeven_stop_pnl_pct","time_exit_days"]:
    settings._data["risk"][k] = settings._data["risk"].get(k)  # already saved during run
settings.save()

print(f"\n{'='*100}")
print(f"Ranking by Portfolio Return")
print(f"{'='*100}")
print(f"{'Rank':<5} {'配置':<30} {'收益':>8} {'平盈':>8} {'胜率':>7} {'PF':>6} {'成交':>6} {'跳过':>6} {'月DD':>6} {'耗时':>6}")
print("-" * 100)
for i, r in enumerate(sorted(results, key=lambda x: -x["ret"])):
    print(f"{i+1:<5} {r['label']:<30} {r['ret']:>+7.2f}% {r['avg_pnl']:>+7.3f}% {r['wr']:>6.1f}% {r['pf']:>5.2f} {r['funded']:>6} {r['skipped']:>6} {r['max_mdd']:>5.1f}% {r['time']:>5.0f}s")
