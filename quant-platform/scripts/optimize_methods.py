"""
改进版赢率优化 — 方法论层面（非参数微调）
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import date
from app.backtest.simple_runner import run_backtest, load_daily_bars, load_index_data
from app.screener.strategies.ma5_angle import generate_signals
from app.sim_trader.config import *
import pandas as pd
import numpy as np
from pathlib import Path

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

def bt(name, sp_extra=None, post_filter=None):
    """运行回测，可选信号后处理"""
    import copy
    p = copy.deepcopy(BASE)

    if post_filter:
        # 先全量生成信号，再后处理筛选
        bars = load_daily_bars(BUFFER, END)
        sig = generate_signals(bars, version="improved",
                               filter_st=True, filter_bj=True,
                               vol_threshold=1.5, close_position_threshold=0.8)
        sig = sig[(sig['date'] >= START) & (sig['date'] <= END)]
        # 后处理过滤
        sig = post_filter(sig, bars)
        # 直接跑回测（不用signal_params）
        from app.backtest.simple_runner import FastEngine
        import pandas as pd
        from collections import defaultdict
        from datetime import timedelta

        bt_bars = bars[(bars['date'] >= START) & (bars['date'] <= END)]
        closes, highs = {}, {}
        for d, g in bt_bars.groupby('date'):
            closes[d] = dict(zip(g['code'], g['close']))
            highs[d] = dict(zip(g['code'], g['high']))
        td = sorted(closes.keys())
        sbd = defaultdict(list)
        for _, r in sig.iterrows():
            sbd[r['date']].append((r['code'], float(r['close'])))

        eng = FastEngine(td, p)
        for d in td:
            snap = {}
            for code2 in eng.positions:
                if d in closes and code2 in closes[d]:
                    snap[code2] = {'open': closes[d].get(code2, 0),
                                   'high': highs[d].get(code2, closes[d].get(code2, 0)),
                                   'low': closes[d].get(code2, 0),
                                   'close': closes[d].get(code2, 0),
                                   'atr': 0}
            eng.sell_phase(d, snap)
            if d in sbd:
                for code3, px in sbd[d]:
                    if eng.cash < min(eng.max_pos(), p.get('min_buy_amt', 5000)): break
                    eng.buy(d, code3, px)
            eng.record(d, snap)

        n = len(eng.trades)
        wins = [t for t in eng.trades if t.ret > 0]
        loses = [t for t in eng.trades if t.ret <= 0]
        fe = eng.equity[-1]['equity'] if eng.equity else p['initial_capital']
        total_ret = (fe / p['initial_capital'] - 1) * 100
        wr = len(wins) / n * 100 if n > 0 else 0
        s = {"total_return": total_ret, "win_rate": wr, "trades": n, "signals": len(sig)}

        # Sharpe
        eq = pd.DataFrame(eng.equity)
        if len(eq) > 1:
            eq['dr'] = eq['equity'].pct_change()
            s['sharpe'] = round(float(np.sqrt(252) * eq['dr'].mean() / eq['dr'].std()) if eq['dr'].std() > 0 else 0, 2)
            eq['cmax'] = eq['equity'].cummax()
            eq['dd'] = (eq['equity'] - eq['cmax']) / eq['cmax'] * 100
            s['max_drawdown'] = round(float(eq['dd'].min()), 1)
        else:
            s['sharpe'] = 0; s['max_drawdown'] = 0
    else:
        sp = {"version": "improved", "filter_st": True, "filter_bj": True,
              "vol_threshold": 1.5, "close_position_threshold": 0.8}
        if sp_extra: sp.update(sp_extra)
        p["signal_params"] = sp
        r = run_backtest(p)
        s = r["summary"]

    print(f"  {name:<22} WR={s['win_rate']:.1f}%  收益={s['total_return']:+.1f}%  交易={s['trades']}  Shar={s['sharpe']:.2f}  DD={s['max_drawdown']:.1f}%")
    return s

# ═══════════════════════════════════════════════════════════

print("=" * 70)
print("改进版赢率优化 — 方法论层面")
print("=" * 70)

# ── 基线 ──
print("\n【基线】")
baseline = bt("基线(改进版默认)")

# ═══════════════════════════════════════════════
# 1. 趋势过滤: 只买 MA20 > MA60 的股票
# ═══════════════════════════════════════════════
print("\n【1. 趋势过滤】MA20 > MA60 只买上升趋势")
def trend_filter(sig, bars):
    g = bars.groupby('code')
    bars2 = bars.copy()
    bars2['ma20'] = g['close'].transform(lambda x: x.rolling(20).mean())
    bars2['ma60'] = g['close'].transform(lambda x: x.rolling(60).mean())
    bars2['trend_up'] = bars2['ma20'] > bars2['ma60']
    bars2['date_d'] = pd.to_datetime(bars2['date']).dt.date
    valid = set()
    for _, r in bars2[bars2['trend_up']].iterrows():
        valid.add((r['code'], r['date_d']))
    return sig[sig.apply(lambda r: (r['code'], r['date']) in valid, axis=1)]

bt("MA20>MA60趋势过滤", post_filter=trend_filter)

# ═══════════════════════════════════════════════
# 2. 大盘过滤: 上证/沪深300在MA20上方才买
# ═══════════════════════════════════════════════
print("\n【2. 大盘过滤】指数在MA20上方才买")
def index_filter_000001(sig, bars):
    idx_bars = load_daily_bars(date(2021, 9, 1), END)
    sh = idx_bars[idx_bars['code'] == 'index_000001'].copy()
    if sh.empty:
        # try loading from index parquet
        idx_path = Path(__file__).parent.parent / "data" / "parquet" / "daily" / "index_000001.parquet"
        if idx_path.exists():
            sh = pd.read_parquet(str(idx_path))
            if 'trade_date' in sh.columns: sh['date'] = pd.to_datetime(sh['trade_date']).dt.date
            elif 'date' in sh.columns: sh['date'] = pd.to_datetime(sh['date']).dt.date
            sh = sh.sort_values('date')
    if sh.empty: return sig
    sh['ma20'] = sh['close'].rolling(20).mean()
    sh['bull'] = sh['close'] > sh['ma20']
    bull_dates = set(sh[sh['bull']]['date'])
    return sig[sig['date'].isin(bull_dates)]

bt("上证MA20上方才买", post_filter=index_filter_000001)

# ═══════════════════════════════════════════════
# 3. 波动率过滤: ATR(14)/close < 5%
# ═══════════════════════════════════════════════
print("\n【3. 波动率过滤】ATR/收盘价 < X%")
def atr_filter(sig, bars, max_atr_pct=0.05):
    bars2 = bars.sort_values(['code', 'date']).copy()
    g = bars2.groupby('code')
    bars2['tr'] = np.maximum(
        bars2['high'] - bars2['low'],
        np.maximum(
            abs(bars2['high'] - bars2['close'].shift(1)),
            abs(bars2['low'] - bars2['close'].shift(1))
        )
    )
    bars2['atr14'] = g['tr'].transform(lambda x: x.rolling(14).mean())
    bars2['atr_pct'] = bars2['atr14'] / bars2['close']
    bars2['date_d'] = pd.to_datetime(bars2['date']).dt.date
    valid = set()
    for _, r in bars2[bars2['atr_pct'] < max_atr_pct].iterrows():
        valid.add((r['code'], r['date_d']))
    return sig[sig.apply(lambda r: (r['code'], r['date']) in valid, axis=1)]

for atr_pct in [0.03, 0.05, 0.07]:
    bt(f"ATR/close<{atr_pct*100:.0f}%", post_filter=lambda s,b,p=atr_pct: atr_filter(s,b,p))

# ═══════════════════════════════════════════════
# 4. 回调确认: 前一天收阴线才买
# ═══════════════════════════════════════════════
print("\n【4. 回调确认】前一天收阴")
def pullback_filter(sig, bars):
    bars2 = bars.copy()
    bars2['red_day'] = bars2['close'] < bars2['open']
    bars2['date_d'] = pd.to_datetime(bars2['date']).dt.date
    # 需要信号日前一天的数据
    red_map = {}
    for _, r in bars2.iterrows():
        red_map[(r['code'], r['date_d'])] = r['red_day']
    def check(r):
        prev = r['date'] - pd.Timedelta(days=1)
        # 找前一天（可能不是交易日，取最近）
        for offset in range(1, 8):
            d = r['date'] - pd.Timedelta(days=offset)
            if (r['code'], d) in red_map:
                return red_map[(r['code'], d)]
        return True  # 找不到就保留
    return sig[sig.apply(check, axis=1)]

bt("前一日收阴", post_filter=pullback_filter)

# ═══════════════════════════════════════════════
# 5. 价格区间: 只买中低价股
# ═══════════════════════════════════════════════
print("\n【5. 价格区间过滤】")
for max_p in [20, 30, 50]:
    def price_filter(sig, bars, mp=max_p):
        return sig[sig['close'] <= mp]
    bt(f"价格<= {max_p}", post_filter=price_filter)

# ═══════════════════════════════════════════════
# 6. 信号质量 topN: 每天只取质量最高的前N个信号
# ═══════════════════════════════════════════════
print("\n【6. 信号质量 TopN】")
def quality_topn(sig, bars, n=5):
    if 'quality' not in sig.columns:
        return sig  # quality already computed in generate_signals
    return sig.groupby('date', group_keys=False).apply(
        lambda g: g.nlargest(n, 'quality')
    ).reset_index(drop=True)

for n in [3, 5, 10]:
    bt(f"每天Top{n}质量", post_filter=lambda s,b,n=n: quality_topn(s,b,n))

# ═══════════════════════════════════════════════
# 7. 量能额外确认: 当日量 > 前一日量（放量）
# ═══════════════════════════════════════════════
print("\n【7. 量能递增】当日量 > 前一日量")
def vol_increasing(sig, bars):
    bars2 = bars.copy()
    g = bars2.groupby('code')
    bars2['prev_vol'] = g['volume'].transform(lambda x: x.shift(1))
    bars2['vol_up'] = bars2['volume'] > bars2['prev_vol']
    bars2['date_d'] = pd.to_datetime(bars2['date']).dt.date
    valid = set()
    for _, r in bars2[bars2['vol_up']].iterrows():
        valid.add((r['code'], r['date_d']))
    return sig[sig.apply(lambda r: (r['code'], r['date']) in valid, axis=1)]

bt("量>前一日", post_filter=vol_increasing)

# ═══════════════════════════════════════════════
# 8. 角度公式: 线性回归斜率 vs 简单斜率
# ═══════════════════════════════════════════════
print("\n【8. 线性回归斜率】替代简单5日斜率")
from scipy import stats
def linreg_slope(y):
    """对最近5个MA5值做线性回归，取斜率"""
    if len(y) < 5: return np.nan
    x = np.arange(len(y))
    slope, _, _, _, _ = stats.linregress(x, y)
    return slope / y.mean() * 100  # 归一化为百分比

def generate_signals_linreg(bars):
    """用线性回归斜率替代简单斜率"""
    import copy
    # 直接用改进版生成信号的逻辑，但换角度公式
    df = bars.copy()
    df = df[~df['name'].str.contains('ST', na=False, case=True)]
    df = df[~df['code'].astype(str).str.startswith('8')]
    g = df.groupby('code', group_keys=False)
    df['ma5'] = g['close'].transform(lambda x: x.rolling(5).mean())
    df['ma20'] = g['close'].transform(lambda x: x.rolling(20).mean())
    df['ma60'] = g['close'].transform(lambda x: x.rolling(60).mean())

    # 线性回归斜率
    df['x1'] = g['ma5'].transform(lambda x: x.rolling(5).apply(linreg_slope, raw=False))
    df['x2'] = g['x1'].transform(lambda x: x.rolling(5).mean())
    df['cross_up'] = (df['x1'] > df['x2']) & (df['x1'].shift(1) <= df['x2'].shift(1))
    df['cond_angle'] = df['cross_up'] & (df['x1'] > df['x1'].shift(1))

    df['avg_vol_20'] = g['volume'].transform(lambda x: x.shift(1).rolling(20).mean())
    df['cond_vol'] = df['volume'] > df['avg_vol_20'] * 1.5
    df['close_pos'] = (df['close'] - df['low']) / (df['high'] - df['low'])
    df['cond_close_strong'] = df['close_pos'] > 0.8
    df['range_high_20'] = g['high'].transform(lambda x: x.shift(1).rolling(20).max())
    df['range_low_20'] = g['low'].transform(lambda x: x.shift(1).rolling(20).min())
    df['range_mid_20'] = (df['range_high_20'] + df['range_low_20']) / 2
    df['cond_price'] = (df['close'] > df['ma20']) & (df['close'] > df['range_mid_20']) & (df['ma60'] >= df['ma60'].shift(10))

    df['za'] = df['cond_angle'] & df['cond_price'] & df['cond_vol'] & df['cond_close_strong']
    df['za_int'] = df['za'].astype(int)
    df['count_20'] = g['za_int'].transform(lambda x: x.rolling(20, min_periods=1).sum())
    df['buy'] = df['za'] & (df['count_20'] == 1)
    df['buy_signal'] = df['buy'] & (~g['buy'].transform(lambda x: x.shift(1)).fillna(False))

    date_col = "date" if "date" in df.columns else "datetime"
    df[date_col] = pd.to_datetime(df[date_col]).dt.date
    result = df[df['buy_signal'] == True].copy()
    return result[(result['date'] >= START) & (result['date'] <= END)]

bt("线性回归斜率", post_filter=lambda s,b: generate_signals_linreg(b))

# ═══════════════════════════════════════════════
print("\n" + "=" * 70)
print("排名（按赢率）")
print("=" * 70)
