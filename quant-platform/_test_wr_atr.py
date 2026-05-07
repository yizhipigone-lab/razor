"""Test: WR 威廉指标 + ATR动态止损"""
import sys, io, time
from pathlib import Path
from datetime import date
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from core.settings import settings
from app.backtest.engine import BacktestEngine
START, END = date(2025,4,25), date(2026,5,2)

v3_rp = {"hard_stop_loss_pct":-5.5,"trailing_stop_activate_pct":8.0,"trailing_stop_drawdown_pct":2.0,
         "time_exit_days":7,"breakeven_threshold_pct":99,"breakeven_stop_pnl_pct":-9,
         "staged_take_profit":[{"profit_pct":4.0,"sell_ratio":0.20,"label":"TP1"},
                               {"profit_pct":14.0,"sell_ratio":0.50,"sell_all":True,"label":"TP2"}]}
base_sp = {"rps_threshold":0,"use_ma_align":False,"use_adx":False,"vol_threshold":1.5,"breadth_threshold":0,
           "use_rsi":False,"use_macd":False,"daily_signal_cap":0}

def test(label, sp, rp, use_atr=False, atr_mul=2.5):
    for k,v in rp.items(): settings.set("risk",k,v,save=False)
    settings.save()
    e = BacktestEngine()
    r = e.run(strategy_name="ma5_angle", strategy_params=sp, start=START, end=END,
              exchanges=["SH","SZ"], sectors=[], index_filter=[], bj_filter=True, sh_red_filter=False,
              use_atr_stop=use_atr, atr_stop_multiplier=atr_mul)
    t = r.trades; n = len(t)
    if n==0: return {"label":label,"n":0,"ret":0,"avg":0,"wr":0,"pf":0,"aw":0,"al":0,"md":0}
    w=[x for x in t if x["pnl_pct"]>0]; l=[x for x in t if x["pnl_pct"]<0]
    a=sum(x["pnl_pct"] for x in t)/n
    wr=len(w)/(len(w)+len(l))*100 if (len(w)+len(l))>0 else 0
    aw=sum(x["pnl_pct"] for x in w)/len(w) if w else 0
    al=sum(x["pnl_pct"] for x in l)/len(l) if l else 0
    pf=abs(aw*len(w)/(al*len(l))) if al!=0 and len(l)>0 else 0
    pr=r.portfolio_total_return if hasattr(r,'portfolio_total_return') else 0
    fd=len(r.portfolio_trades) if hasattr(r,'portfolio_trades') and r.portfolio_trades else n
    sk=r.portfolio_skipped if hasattr(r,'portfolio_skipped') else 0
    mo=getattr(r,"portfolio_monthly",None)
    md=max((m.get("max_dd_pct",0) for m in mo), default=0) if mo else 0
    return {"label":label,"n":n,"fd":fd,"sk":sk,"ret":pr,"avg":a,"wr":wr,"pf":pf,"aw":aw,"al":al,"md":md}

tests = [
    # Phase 1: WR filter variations
    ("W0-V3基线", {**base_sp}, v3_rp),
    ("W1-WR≤-20(排除超买)", {**base_sp,"use_wr":True,"wr_max":-20}, v3_rp),
    ("W2-WR≤-30(更严格)", {**base_sp,"use_wr":True,"wr_max":-30}, v3_rp),
    ("W3-WR≤-15(更宽松)", {**base_sp,"use_wr":True,"wr_max":-15}, v3_rp),

    # Phase 2: ATR dynamic stop (keep hard_sl as fallback)
    ("A1-ATR2.0x(宽止损)", {**base_sp}, {**v3_rp}, True, 2.0),
    ("A2-ATR2.5x(中)", {**base_sp}, {**v3_rp}, True, 2.5),
    ("A3-ATR3.0x(窄止损)", {**base_sp}, {**v3_rp}, True, 3.0),

    # Phase 3: WR + ATR combo
    ("C1-WR≤20+ATR2.5x", {**base_sp,"use_wr":True,"wr_max":-20}, {**v3_rp}, True, 2.5),
    ("C2-WR≤30+ATR2.5x", {**base_sp,"use_wr":True,"wr_max":-30}, {**v3_rp}, True, 2.5),
]

results = []
for args in tests:
    label = args[0]
    sp = args[1]
    rp = args[2]
    use_atr = args[3] if len(args) > 3 else False
    atr_mul = args[4] if len(args) > 4 else 2.5
    print(f"\n{'='*50}\n{label}")
    t0=time.time()
    r = test(label, sp, rp, use_atr, atr_mul)
    r["time"]=time.time()-t0
    results.append(r)
    tag = f" ATR{atr_mul}x" if use_atr else ""
    print(f"  交易{r['n']}笔 成交{r['fd']} 跳过{r['sk']} | 平盈{r['avg']:+.3f}% 胜率{r['wr']:.1f}% PF={r['pf']:.2f}")
    print(f"  收益{r['ret']:+.2f}% | 均赢{r['aw']:+.2f}% 均亏{r['al']:+.2f}% | 月DD{r['md']:.1f}%")

# Restore
tp = [{"profit_pct":4.0,"sell_ratio":0.20,"label":"TP1"},{"profit_pct":14.0,"sell_ratio":0.50,"sell_all":True,"label":"TP2"}]
for k,v in {"hard_stop_loss_pct":-5.5,"trailing_stop_activate_pct":8.0,"trailing_stop_drawdown_pct":2.0,
            "breakeven_threshold_pct":99,"breakeven_stop_pnl_pct":-9,"time_exit_days":7}.items():
    settings.set("risk",k,v,save=False)
settings.set("risk","staged_take_profit",tp,save=False)
settings.save()

print(f"\n{'='*80}")
print(f"WR + ATR 动态止损测试")
print(f"{'='*80}")
for i,r in enumerate(sorted(results, key=lambda x:-x["ret"])):
    stars = "⭐" if r["ret"]>45 else ("✅" if r["ret"]>30 else "")
    print(f"{i+1}. {r['label']:<30} {stars} 收益{r['ret']:>+7.2f}% 平盈{r['avg']:>+7.3f}% 胜率{r['wr']:>5.1f}% PF={r['pf']:.2f} 均赢{r['aw']:>+5.2f} 均亏{r['al']:>+5.2f} 月DD{r['md']:>4.1f}%")
