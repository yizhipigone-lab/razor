"""
大盘过滤优化 — 多种指数 + 均线组合
只在大盘在均线上方时买入，下方时空仓
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import date
from app.backtest.simple_runner import run_backtest, load_daily_bars
from app.screener.strategies.ma5_angle import generate_signals
from app.sim_trader.config import *
import pandas as pd; import numpy as np
from pathlib import Path

START = date(2017, 1, 1); END = date.today()
BUFFER = START - pd.Timedelta(days=365)

BASE = {
    "start_date": START, "end_date": END,
    "initial_capital": INITIAL_CAPITAL, "position_size": POSITION_SIZE,
    "min_buy_amt": MIN_BUY_AMT,
    "loss_streak_halve": LOSS_STREAK_HALVE,
    "loss_streak_pause": LOSS_STREAK_PAUSE, "pause_days": PAUSE_DAYS,
    "hard_stop": HARD_STOP, "trail_activate": TRAIL_ACTIVATE, "trail_dd": TRAIL_DD,
    "use_atr_trail": USE_ATR_TRAIL, "atr_trail_multiplier": ATR_TRAIL_MULTIPLIER,
    "time_exit_days": TIME_EXIT_DAYS, "time_exit_profit": TIME_EXIT_PROFIT,
    "time_force_days": TIME_FORCE_DAYS, "same_stock_cooldown": SAME_STOCK_COOLDOWN,
    "take_profit_tiers": TAKE_PROFIT_TIERS,
}

SP = {"version":"original","filter_st":True,"filter_bj":True,"skip_limit_up":True}

# 加载指数数据
INDICES = {
    '上证指数': 'index_000001',
    '沪深300':  'index_000300',
    '中证500':  'index_000905',
    '中证1000': 'index_000852',
    '创业板指': 'index_399006',
    '中证A500': 'index_000510',
}
DAILY = Path(__file__).parent.parent / "data" / "parquet" / "daily"

def load_idx(name):
    fp = DAILY / f"{name}.parquet"
    if not fp.exists(): return None
    df = pd.read_parquet(str(fp))
    tc = 'trade_date' if 'trade_date' in df.columns else 'date'
    df['date'] = pd.to_datetime(df[tc]).dt.date
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    return df.dropna(subset=['close']).sort_values('date')

def make_filter(idx_df, ma_period):
    """返回过滤函数"""
    df = idx_df.copy()
    df['ma'] = df['close'].rolling(ma_period).mean()
    df['bull'] = df['close'] > df['ma']
    bull_dates = set(df[df['bull']]['date'])
    def f(sig, bars):
        return sig[sig['date'].isin(bull_dates)]
    return f

def bt_method(name):
    p = dict(BASE); p["signal_params"] = dict(SP)
    r = run_backtest(p)
    s = r["summary"]
    print(f"  {name:<32} WR={s['win_rate']:.1f}%  收益={s['total_return']:+.1f}%  交易={s['trades']:>5}  DD={s['max_drawdown']:.1f}%  Shar={s['sharpe']:.2f}")
    return s

def bt_idx(name, idx_key, ma_period):
    idx = load_idx(idx_key)
    if idx is None: return None
    f = make_filter(idx, ma_period)
    # 生成信号 + 后处理过滤
    bars = load_daily_bars(BUFFER, END)
    sig = generate_signals(bars, version="original", filter_st=True, filter_bj=True, skip_limit_up=True)
    sig = sig[(sig['date']>=START)&(sig['date']<=END)]
    sig = f(sig, bars)

    from app.backtest.simple_runner import FastEngine
    from collections import defaultdict
    bt_bars = bars[(bars['date']>=START)&(bars['date']<=END)]
    closes={}; highs={}
    for d,g in bt_bars.groupby('date'):
        closes[d]=dict(zip(g['code'],g['close']))
        highs[d]=dict(zip(g['code'],g['high']))
    td=sorted(closes.keys())
    sbd=defaultdict(list)
    for _,r in sig.iterrows(): sbd[r['date']].append((r['code'],float(r['close'])))
    eng=FastEngine(td, dict(BASE))
    for d in td:
        snap={}
        for c in eng.positions:
            if d in closes and c in closes[d]:
                snap[c]={'open':closes[d].get(c,0),'high':highs[d].get(c,closes[d].get(c,0)),'low':closes[d].get(c,0),'close':closes[d].get(c,0),'atr':0}
        eng.sell_phase(d, snap)
        if d in sbd:
            for code3,px in sbd[d]:
                if eng.cash<min(eng.max_pos(),BASE.get('min_buy_amt',5000)): break
                eng.buy(d, code3, px)
        eng.record(d, snap)
    n=len(eng.trades); w=[t for t in eng.trades if t.ret>0]; l=[t for t in eng.trades if t.ret<=0]
    fe=eng.equity[-1]['equity'] if eng.equity else BASE['initial_capital']
    tr=(fe/BASE['initial_capital']-1)*100; wr=len(w)/n*100 if n>0 else 0
    eq=pd.DataFrame(eng.equity)
    sh=0; dd=0
    if len(eq)>1:
        eq['dr']=eq['equity'].pct_change()
        sh=round(float(np.sqrt(252)*eq['dr'].mean()/eq['dr'].std()) if eq['dr'].std()>0 else 0,2)
        eq['cmax']=eq['equity'].cummax()
        eq['dd']=(eq['equity']-eq['cmax'])/eq['cmax']*100
        dd=round(float(eq['dd'].min()),1)
    print(f"  {name:<32} WR={wr:.1f}%  收益={tr:+.1f}%  交易={n:>5}  DD={dd:.1f}%  Shar={sh:.2f}")
    return {'total_return':tr, 'win_rate':wr, 'trades':n, 'max_drawdown':dd, 'sharpe':sh}

print("="*70)
print("大盘过滤测试 2017-01-01 ~ today")
print("="*70)

print("\n【基线 无过滤】")
baseline = bt_method("基线(无大盘过滤)")

MAs = [20, 60, 120, 250]
for idx_name, idx_file in INDICES.items():
    print(f"\n【{idx_name}】")
    for ma in MAs:
        bt_idx(f"  {idx_name} MA{ma}", idx_file, ma)
