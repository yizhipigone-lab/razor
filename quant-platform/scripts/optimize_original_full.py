"""
原版策略全面优化 — 参数扫描 + 方法论测试
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import date
from app.backtest.simple_runner import run_backtest, load_daily_bars
from app.screener.strategies.ma5_angle import generate_signals
from app.sim_trader.config import *
from pathlib import Path
import pandas as pd; import numpy as np

START = date(2024, 1, 1); END = date.today()
BUFFER = START - pd.Timedelta(days=180)

BASE = {
    "start_date": START, "end_date": END,
    "initial_capital": INITIAL_CAPITAL, "position_size": POSITION_SIZE,
    "min_buy_amt": MIN_BUY_AMT,
    "loss_streak_halve": LOSS_STREAK_HALVE,
    "loss_streak_pause": LOSS_STREAK_PAUSE, "pause_days": PAUSE_DAYS,
    "hard_stop": HARD_STOP, "trail_activate": TRAIL_ACTIVATE, "trail_dd": TRAIL_DD,
    "time_exit_days": TIME_EXIT_DAYS, "time_exit_profit": TIME_EXIT_PROFIT,
    "time_force_days": TIME_FORCE_DAYS, "same_stock_cooldown": SAME_STOCK_COOLDOWN,
    "take_profit_tiers": TAKE_PROFIT_TIERS,
}

ORIG_BASE = {"version":"original","filter_st":True,"filter_bj":True}

def bt(name, sp_override=None):
    p = dict(BASE)
    sp = dict(ORIG_BASE)
    if sp_override: sp.update(sp_override)
    p["signal_params"] = sp
    r = run_backtest(p)
    s = r["summary"]
    print(f"  {name:<28} WR={s['win_rate']:.1f}%  收={s['total_return']:+.1f}%  交={s['trades']:>5}  Shar={s['sharpe']:.2f}  DD={s['max_drawdown']:.1f}%  盈亏比={s['profit_factor']:.2f}")
    return s

def bt_exit(name, exit_overrides):
    """测试退出参数"""
    p = dict(BASE); p.update(exit_overrides)
    p["signal_params"] = dict(ORIG_BASE)
    r = run_backtest(p)
    s = r["summary"]
    print(f"  {name:<28} WR={s['win_rate']:.1f}%  收={s['total_return']:+.1f}%  交={s['trades']:>5}  DD={s['max_drawdown']:.1f}%")
    return s

def bt_tp(name, tiers):
    p = dict(BASE); p["signal_params"] = dict(ORIG_BASE)
    p["take_profit_tiers"] = tiers
    r = run_backtest(p)
    s = r["summary"]
    print(f"  {name:<28} WR={s['win_rate']:.1f}%  收={s['total_return']:+.1f}%  交={s['trades']:>5}")
    return s

def bt_method(name, post_filter):
    """后处理过滤测试"""
    p = dict(BASE)
    bars = load_daily_bars(BUFFER, END)
    sig = generate_signals(bars, version="original", filter_st=True, filter_bj=True)
    sig = sig[(sig['date']>=START)&(sig['date']<=END)]
    sig = post_filter(sig, bars)

    from app.backtest.simple_runner import FastEngine
    from collections import defaultdict
    bt_bars = bars[(bars['date']>=START)&(bars['date']<=END)]
    closes = {}; highs = {}
    for d, g in bt_bars.groupby('date'):
        closes[d]=dict(zip(g['code'],g['close']))
        highs[d]=dict(zip(g['code'],g['high']))
    td = sorted(closes.keys())
    sbd = defaultdict(list)
    for _, r in sig.iterrows():
        sbd[r['date']].append((r['code'],float(r['close'])))
    eng = FastEngine(td, p)
    for d in td:
        snap = {}
        for c in eng.positions:
            if d in closes and c in closes[d]:
                snap[c]={'open':closes[d].get(c,0),'high':highs[d].get(c,closes[d].get(c,0)),'low':closes[d].get(c,0),'close':closes[d].get(c,0),'atr':0}
        eng.sell_phase(d, snap)
        if d in sbd:
            for code3, px in sbd[d]:
                if eng.cash<min(eng.max_pos(),p.get('min_buy_amt',5000)): break
                eng.buy(d, code3, px)
        eng.record(d, snap)
    n=len(eng.trades); w=[t for t in eng.trades if t.ret>0]; l=[t for t in eng.trades if t.ret<=0]
    fe=eng.equity[-1]['equity'] if eng.equity else p['initial_capital']
    tr=(fe/p['initial_capital']-1)*100; wr=len(w)/n*100 if n>0 else 0
    eq=pd.DataFrame(eng.equity)
    sh=0; dd=0
    if len(eq)>1:
        eq['dr']=eq['equity'].pct_change()
        sh=round(float(np.sqrt(252)*eq['dr'].mean()/eq['dr'].std()) if eq['dr'].std()>0 else 0,2)
        eq['cmax']=eq['equity'].cummax()
        eq['dd']=(eq['equity']-eq['cmax'])/eq['cmax']*100
        dd=round(float(eq['dd'].min()),1)
    print(f"  {name:<28} WR={wr:.1f}%  收={tr:+.1f}%  交={n:>5}  Shar={sh:.2f}  DD={dd:.1f}%")

print("="*70)
print("原版策略 全面优化 — 2024-01-01 ~ today")
print("="*70)

# ═══════════ 基线 ═══════════
print("\n【基线】")
baseline = bt("基线(原版默认)")

# ═══════════ 1. 止盈止损 ═══════════
print("\n【1. 硬止损】")
for hs in [-0.05, -0.07, -0.08, -0.10]:
    bt_exit(f"HS={hs*100:.0f}%", {"hard_stop":hs})

print("\n【2. TP止盈档位】")
for name, tiers in [
    ("TP1=3%/15%", [{"profit_pct":0.03,"sell_ratio":0.15},{"profit_pct":0.07,"sell_ratio":0.25}]),
    ("TP1=5%/20%", [{"profit_pct":0.05,"sell_ratio":0.20},{"profit_pct":0.08,"sell_ratio":0.25}]),
    ("TP1=3%/10%+TP2=6%/20%", [{"profit_pct":0.03,"sell_ratio":0.10},{"profit_pct":0.06,"sell_ratio":0.20}]),
    ("TP1=4%/10%+TP2=8%/20%", [{"profit_pct":0.04,"sell_ratio":0.10},{"profit_pct":0.08,"sell_ratio":0.20}]),
]:
    bt_tp(name, tiers)

print("\n【3. 时间退出】")
for td in [5, 7, 12]:
    bt_exit(f"T_force={td}d", {"time_force_days":td})
for te in [2, 5]:
    bt_exit(f"T_exit={te}d", {"time_exit_days":te})

# ═══════════ 2. 信号参数 ═══════════
print("\n【4. 可选过滤器】")
for name, extra in [
    ("连续阳线过滤", {"filter_consecutive_up":True}),
    ("跳空过滤", {"filter_gap_quality":True}),
    ("质量排序关", {"disable_quality_sort":True}),
]:
    bt(name, extra)

# ═══════════ 3. 方法论测试 ═══════════
print("\n【5. 趋势过滤 MA20>MA60】")
def trend_filter(sig, bars):
    g=bars.groupby('code')
    b2=bars.copy(); b2['ma20']=g['close'].transform(lambda x:x.rolling(20).mean())
    b2['ma60']=g['close'].transform(lambda x:x.rolling(60).mean())
    b2['up']=b2['ma20']>b2['ma60']
    b2['dd']=pd.to_datetime(b2['date']).dt.date
    v=set(); [v.add((r['code'],r['dd'])) for _,r in b2[b2['up']].iterrows()]
    return sig[sig.apply(lambda r:(r['code'],r['date']) in v, axis=1)]
bt_method("MA20>MA60", trend_filter)

print("\n【6. 大盘过滤 上证MA20】")
def idx_filter(sig, bars):
    f=Path(__file__).parent.parent/"data"/"parquet"/"daily"/"index_000001.parquet"
    if not f.exists(): return sig
    sh=pd.read_parquet(str(f))
    tc='trade_date' if 'trade_date' in sh.columns else 'date'
    sh['date']=pd.to_datetime(sh[tc]).dt.date
    sh=sh.sort_values('date'); sh['ma']=sh['close'].rolling(20).mean()
    bd=set(sh[sh['close']>sh['ma']]['date'])
    return sig[sig['date'].isin(bd)]
bt_method("上证MA20上方", idx_filter)

print("\n【7. 波动率过滤 ATR/close】")
def atr_filter(sig, bars, mx=0.05):
    b2=bars.sort_values(['code','date']).copy(); g=b2.groupby('code')
    b2['tr']=np.maximum(b2['high']-b2['low'],
        np.maximum(abs(b2['high']-b2['close'].shift(1)),abs(b2['low']-b2['close'].shift(1))))
    b2['atr']=g['tr'].transform(lambda x:x.rolling(14).mean())
    b2['ap']=b2['atr']/b2['close']; b2['dd']=pd.to_datetime(b2['date']).dt.date
    v=set(); [v.add((r['code'],r['dd'])) for _,r in b2[b2['ap']<mx].iterrows()]
    return sig[sig.apply(lambda r:(r['code'],r['date']) in v, axis=1)]
for mx in [0.03, 0.05, 0.07]:
    bt_method(f"ATR<{mx*100:.0f}%", lambda s,b,m=mx:atr_filter(s,b,m))

print("\n【8. 回调确认 前日收阴】")
def pullback(sig, bars):
    b2=bars.copy(); b2['red']=b2['close']<b2['open']
    b2['dd']=pd.to_datetime(b2['date']).dt.date
    rm={(r['code'],r['dd']):r['red'] for _,r in b2.iterrows()}
    def chk(r):
        for off in range(1,8):
            d=r['date']-pd.Timedelta(days=off)
            if (r['code'],d) in rm: return rm[(r['code'],d)]
        return True
    return sig[sig.apply(chk, axis=1)]
bt_method("前日收阴", pullback)

print("\n【9. 价格过滤】")
for mp in [20, 30, 50]:
    bt_method(f"价格<={mp}", lambda s,b,m=mp: s[s['close']<=m])

print("\n【10. 信号质量TopN】")
def topn(sig, bars, n=5):
    if 'quality' not in sig.columns: return sig
    return sig.groupby('date',group_keys=False).apply(lambda g:g.nlargest(n,'quality')).reset_index(drop=True)
for n in [3, 5, 10]:
    bt_method(f"每日Top{n}", lambda s,b,n=n: topn(s,b,n))

print("\n【11. 量能递增】")
def vol_up(sig, bars):
    g=bars.groupby('code'); b2=bars.copy()
    b2['pv']=g['volume'].transform(lambda x:x.shift(1))
    b2['up']=b2['volume']>b2['pv']; b2['dd']=pd.to_datetime(b2['date']).dt.date
    v=set(); [v.add((r['code'],r['dd'])) for _,r in b2[b2['up']].iterrows()]
    return sig[sig.apply(lambda r:(r['code'],r['date']) in v, axis=1)]
bt_method("量>前日", vol_up)

print("\n【12. 角度加速 x1>x1.shift(2)】")
def angle_accel(sig, bars):
    """用改进版的角度确认逻辑（只需加速，不需要V型反转）"""
    b2=bars.copy(); g=b2.groupby('code')
    b2['ma5']=g['close'].transform(lambda x:x.rolling(5).mean())
    b2['x1o']=g['ma5'].transform(lambda x:np.degrees(np.arctan((x/x.shift(1)-1)*100)))
    b2['x2o']=g['x1o'].transform(lambda x:x.rolling(5).mean())
    b2['cross']=(b2['x1o']>b2['x2o'])&(b2['x1o'].shift(1)<=b2['x2o'].shift(1))
    b2['accel']=b2['x1o']>b2['x1o'].shift(1)
    b2['sig']=b2['cross']&b2['accel']
    b2['dd']=pd.to_datetime(b2['date']).dt.date
    v=set(); [v.add((r['code'],r['dd'])) for _,r in b2[b2['sig']].iterrows()]
    return sig[sig.apply(lambda r:(r['code'],r['date']) in v, axis=1)]
bt_method("角度加速确认", angle_accel)

print("\n"+"="*70)
print("总结: 最优方法")
print("="*70)
