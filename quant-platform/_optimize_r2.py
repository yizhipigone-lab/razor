"""Round 2: RSI+MACD filters + aggressive exit optimization"""
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

def run_one(label, strategy_params, risk_overrides):
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
    funded = len(result.portfolio_trades) if hasattr(result,'portfolio_trades') and result.portfolio_trades else n
    skipped = result.portfolio_skipped if hasattr(result,'portfolio_skipped') else 0
    monthly = getattr(result, "portfolio_monthly", None)
    max_mdd = max((m.get("max_dd_pct",0) for m in monthly), default=0) if monthly else 0
    return {"label": label, "n": n, "funded": funded, "skipped": skipped,
            "ret": pf_ret, "avg_pnl": avg_pnl, "wr": wr, "pf": pf,
            "avg_win": avg_win, "avg_loss": avg_loss, "max_mdd": max_mdd}

print("=" * 100)
print("Round 2: RSI/MACD + Aggressive Exits")
print("=" * 100)

# Best baseline from Round 1 will be known later, for now test new filters
configs = [
    # 2A: RSI 40-75 only
    ("2A-RSI40-75",
     {"rps_threshold":0,"use_ma_align":False,"use_adx":False,"vol_threshold":1.5,"use_rsi":True,"rsi_min":40,"rsi_max":75},
     {}),
    # 2B: MACD only
    ("2B-MACD",
     {"rps_threshold":0,"use_ma_align":False,"use_adx":False,"vol_threshold":1.5,"use_macd":True},
     {}),
    # 2C: RSI+MACD
    ("2C-RSI+MACD",
     {"rps_threshold":0,"use_ma_align":False,"use_adx":False,"vol_threshold":1.5,"use_rsi":True,"rsi_min":40,"rsi_max":75,"use_macd":True},
     {}),
    # 2D: RSI+MACD + aggressive exits
    ("2D-RSI+MACD+硬-5%+移4/2+保2/0.5+时5",
     {"rps_threshold":0,"use_ma_align":False,"use_adx":False,"vol_threshold":1.5,"use_rsi":True,"rsi_min":40,"rsi_max":75,"use_macd":True},
     {"hard_stop_loss_pct":-5.0,"trailing_stop_activate_pct":4.0,"trailing_stop_drawdown_pct":2.0,
      "breakeven_threshold_pct":2.0,"breakeven_stop_pnl_pct":0.5,"time_exit_days":5}),
    # 2E: RSI+MACD + moderate exits
    ("2E-RSI+MACD+硬-6%+移6/2.5+保3/1+时6",
     {"rps_threshold":0,"use_ma_align":False,"use_adx":False,"vol_threshold":1.5,"use_rsi":True,"rsi_min":40,"rsi_max":75,"use_macd":True},
     {"hard_stop_loss_pct":-6.0,"trailing_stop_activate_pct":6.0,"trailing_stop_drawdown_pct":2.5,
      "breakeven_threshold_pct":3.0,"breakeven_stop_pnl_pct":1.0,"time_exit_days":6}),
    # 2F: RSI 50-70 (narrower) + MACD + tight exits
    ("2F-RSI50-70+MACD+硬-5%+移5/2+保3/1",
     {"rps_threshold":0,"use_ma_align":False,"use_adx":False,"vol_threshold":1.5,"use_rsi":True,"rsi_min":50,"rsi_max":70,"use_macd":True},
     {"hard_stop_loss_pct":-5.0,"trailing_stop_activate_pct":5.0,"trailing_stop_drawdown_pct":2.0,
      "breakeven_threshold_pct":3.0,"breakeven_stop_pnl_pct":1.0,"time_exit_days":6}),
]

results = []
for label, strat_p, risk_p in configs:
    print(f"\n{'='*60}")
    print(f"Running: {label}")
    t0 = time.time()
    r = run_one(label, strat_p, risk_p)
    r["time"] = time.time() - t0
    results.append(r)
    print(f"  -> 交易{r['n']}笔 | 平盈{r['avg_pnl']:+.3f}% | 胜率{r['wr']:.1f}% | PF={r['pf']:.2f}")
    print(f"  -> 组合收益{r['ret']:+.2f}% | 成交{r['funded']}笔 | 跳过{r['skipped']} | 月DD{r['max_mdd']:.1f}%")

# Restore original risk params
settings.set("risk","hard_stop_loss_pct",-9.0,save=False)
settings.set("risk","trailing_stop_activate_pct",11.4,save=False)
settings.set("risk","trailing_stop_drawdown_pct",1.6,save=False)
settings.set("risk","breakeven_threshold_pct",10.9,save=False)
settings.set("risk","breakeven_stop_pnl_pct",4.4,save=False)
settings.set("risk","time_exit_days",8,save=False)
settings.save()

print(f"\n{'='*100}")
print(f"Round 2 Ranking")
print(f"{'='*100}")
for i, r in enumerate(sorted(results, key=lambda x: -x["ret"])):
    print(f"{i+1}. {r['label']:<35} 收益{r['ret']:>+7.2f}% 平盈{r['avg_pnl']:>+7.3f}% 胜率{r['wr']:>5.1f}% PF={r['pf']:.2f} 月DD{r['max_mdd']:>5.1f}% 信号{r['funded']}/{r['skipped']}跳过")
