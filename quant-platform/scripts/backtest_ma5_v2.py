#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度策略 �?多精度回�?v2 (优化�?
关键优化：按股票缓存日内数据，避免反复读�?parquet
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta, datetime
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from collections import Counter, defaultdict
import time, warnings
warnings.filterwarnings('ignore')

# ══════════════════�?配置 ══════════════════�?INITIAL_CAPITAL = 1_000_000
POSITION_CAP    = 50_000
HARD_STOP, TP1_PCT, TP1_RATIO, TP2_PCT = -0.055, 0.04, 0.20, 0.14
TRAIL_ACTIVATE, TRAIL_DD = 0.08, 0.02
TIME_EXIT, TIME_FORCE = 7, 10
LOSS_S1, LOSS_S2, PAUSE_D = 3, 5, 3
MIN_BUY, SAME_COOLDOWN = 5_000, 20

BACKTEST_START = date(2018, 1, 1)
BACKTEST_END   = date(2026, 5, 1)
BUFFER_DAYS    = 365
LOAD_START     = BACKTEST_START - timedelta(days=BUFFER_DAYS)

ROOT = Path(__file__).parent.parent
DAILY_DIR  = ROOT / "data" / "parquet" / "daily"
MIN1_DIR   = ROOT / "data" / "parquet" / "min1"
MIN5_DIR   = ROOT / "data" / "parquet" / "min5"

SIGNAL_PARAMS = {
    "version": "improved", "filter_st": True, "filter_bj": True,
    "vol_threshold": 1.2, "close_position_threshold": 0.6,
    "disable_quality_sort": True,
    "filter_consecutive_up": False, "filter_gap_quality": False,
}
# ════════════════════════════════════════════

@dataclass
class Pos:
    code: str; entry_date: date; entry_price: float; entry_time: str
    shares: int; cost: float
    peak_price: float = 0.0; remaining_shares: int = 0
    tp1_done: bool = False; tp2_done: bool = False; is_active: bool = True
    def __post_init__(self):
        self.peak_price = self.entry_price
        self.remaining_shares = self.shares

@dataclass
class Trade:
    code: str; entry_date: date; entry_time: str; exit_date: date; exit_time: str
    entry_price: float; exit_price: float; shares: int
    return_pct: float; profit_amount: float; exit_reason: str; hold_days: int
    precision: str = ""

# ══════════════════�?数据加载 ══════════════════�?
def load_daily_bars():
    print("[加载] 日线...", end=" ", flush=True); t0 = time.time()
    files = [f for f in DAILY_DIR.glob("*.parquet")
             if not f.stem.startswith('index_') and len(f.stem) == 6 and f.stem.isdigit()]
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(str(f), columns=['date','open','high','low','close','volume'])
            df['code'] = f.stem
            dfs.append(df)
        except: pass
    bars = pd.concat(dfs, ignore_index=True)
    for c in ['open','high','low','close','volume']:
        bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=['close'])
    bars['date'] = pd.to_datetime(bars['date']).dt.date
    bars = bars.sort_values(['code','date'])
    print(f"{len(bars):,}�?{bars['code'].nunique()}�?{time.time()-t0:.0f}s")
    return bars


def load_intraday_cached(code: str, cache: dict, min_dir: Path) -> pd.DataFrame:
    """加载一只股票的完整日内数据，按日期缓存"""
    key = (str(min_dir), code)
    if key in cache:
        return cache[key]

    f = min_dir / f"{code}.parquet"
    if not f.exists():
        for suffix in ['SH','SZ']:
            alt = min_dir / f"{code}{suffix}.parquet"
            if alt.exists(): f = alt; break
        else:
            cache[key] = None; return None

    try:
        df = pd.read_parquet(str(f))
        if 'datetime' in df.columns and not df.empty:
            df['datetime'] = pd.to_datetime(df['datetime'])
            df['date'] = df['datetime'].dt.date
            df['time_str'] = df['datetime'].dt.strftime('%H:%M')
            cache[key] = df
            return df
    except: pass
    cache[key] = None
    return None


# ══════════════════�?风控 ══════════════════�?
def check_bar(pos: Pos, bar: dict) -> Optional[Tuple]:
    high, low, close = float(bar.get('high',bar.get('close',0))), float(bar.get('low',bar.get('close',0))), float(bar.get('close',0))
    if high > pos.peak_price: pos.peak_price = high
    pp, cp = pos.peak_price/pos.entry_price-1, close/pos.entry_price-1
    # 1. 硬止�?    if low <= pos.entry_price*(1+HARD_STOP): return (pos.entry_price*(1+HARD_STOP), f"硬止�?)
    # 2. TP2
    if not pos.tp2_done and high >= pos.entry_price*(1+TP2_PCT): return (pos.entry_price*(1+TP2_PCT), f"TP2")
    # 3. TP1
    if not pos.tp1_done and high >= pos.entry_price*(1+TP1_PCT):
        ss = int(pos.remaining_shares*TP1_RATIO/100)*100
        if ss >= 100: return (pos.entry_price*(1+TP1_PCT), f"TP1", ss)
    # 4. 移动止盈
    if pp >= TRAIL_ACTIVATE and low <= pos.peak_price*(1-TRAIL_DD): return (pos.peak_price*(1-TRAIL_DD), f"移动止盈")
    # 5. 保本
    if pp >= 0.03 and low <= pos.entry_price: return (pos.entry_price, f"保本")
    return None

def check_eod(pos: Pos, close: float, d: date) -> Optional[Tuple]:
    cp, hd = close/pos.entry_price-1, (d-pos.entry_date).days
    if hd > TIME_FORCE: return (close, f"时间强制({hd}�?")
    if hd > TIME_EXIT and cp > 0.01: return (close, f"时间条件({hd}�?")
    return None

# ══════════════════�?引擎 ══════════════════�?
class Engine:
    def __init__(self, mode, trading_dates):
        self.mode = mode; self.cash = INITIAL_CAPITAL
        self.positions: Dict[str, Pos] = {}
        self.trades: List[Trade] = []
        self.equity: List[dict] = []
        self.closses = 0; self.pause_until = None
        self.trading_dates = trading_dates
        self.stats = Counter()  # precision usage
        self.intra_exits = 0; self.eod_exits = 0
        # Intraday data caches (one per stock, reused across days)
        self.cache_min1 = {}
        self.cache_min5 = {}

    def eq(self, px): return self.cash + sum(p.remaining_shares*px.get(p.code,p.entry_price) for p in self.positions.values() if p.is_active)
    def npos(self): return len([p for p in self.positions.values() if p.is_active])
    def maxpos(self): return POSITION_CAP/2 if self.closses >= LOSS_S1 else POSITION_CAP
    def td(self, d1, d2): return sum(1 for td in self.trading_dates if d1<=td<=d2)

    def do_sell(self, pos, price, reason, partial=None, ed=None, et="", prec=""):
        ss = int((partial or pos.remaining_shares)//100*100)
        if ss <= 0: return None
        rp = (price/pos.entry_price-1)*100
        pos.remaining_shares -= ss
        if "TP2" in reason: pos.tp2_done = True
        if "TP1" in reason: pos.tp1_done = True
        if pos.remaining_shares <= 0: pos.is_active = False; pos.remaining_shares = 0
        self.cash += ss*price; self.stats[prec] += 1
        return Trade(pos.code, pos.entry_date, pos.entry_time, ed or date.today(), et,
                     pos.entry_price, price, ss, rp, ss*(price-pos.entry_price), reason,
                     self.td(pos.entry_date, ed) if ed else 0, prec)

    def do_buy(self, d, code, price, et="", prec=""):
        if code in self.positions: return None
        ma = min(self.maxpos(), self.cash)
        if ma < MIN_BUY: return None
        shares = int(ma/price/100)*100
        if shares < 100 and self.cash >= price*100: shares = 100
        if shares < 100 or shares*price > self.cash: return None
        pos = Pos(code, d, price, et, shares, shares*price)
        self.cash -= pos.cost; self.positions[code] = pos
        self.stats[prec] += 1; return pos

    def record(self, d, px):
        self.equity.append({'date':d, 'equity':self.eq(px), 'cash':self.cash, 'positions':self.npos()})


# ══════════════════�?主逻辑 ══════════════════�?
def run_one_mode(mode, bars, signals_df, daily_data, trading_dates, label):
    t0 = time.time()
    print(f"\n{'='*60}\n  [{label}] 回测...\n{'='*60}")
    eng = Engine(mode, trading_dates)

    sig_by_date = defaultdict(list)
    for _, r in signals_df.iterrows():
        sig_by_date[r['date']].append((r['code'], float(r['close'])))

    for day_i, d in enumerate(trading_dates):
        eod_px = {}

        # ── 处理持仓 ──
        for code, pos in list(eng.positions.items()):
            if not pos.is_active: continue
            prec = "daily_close"
            exited = False
            intra_df = None

            if mode in ("hybrid", "min5"):
                # Try 1min
                if mode == "hybrid":
                    df = load_intraday_cached(code, eng.cache_min1, MIN1_DIR)
                    if df is not None:
                        day_df = df[df['date'] == d]
                        if len(day_df) >= 10: intra_df = day_df; prec = "1min"

                # Try 5min
                if intra_df is None:
                    df = load_intraday_cached(code, eng.cache_min5, MIN5_DIR)
                    if df is not None:
                        day_df = df[df['date'] == d]
                        if len(day_df) >= 10: intra_df = day_df; prec = "5min"

            if intra_df is not None and not intra_df.empty:
                # Bar-by-bar check
                for _, bar in intra_df.iterrows():
                    if exited: break
                    result = check_bar(pos, bar.to_dict())
                    if result:
                        ep, reason = result[0], result[1]
                        partial = result[2] if len(result) > 2 else None
                        trade = eng.do_sell(pos, ep, reason, partial, d, bar['time_str'], prec)
                        if trade:
                            eng.trades.append(trade); eng.intra_exits += 1; exited = True
                            if trade.return_pct <= 0:
                                eng.closses += 1
                            else:
                                eng.closses = 0; eng.pause_until = None
                            if eng.closses >= LOSS_S2: eng.pause_until = d + timedelta(days=PAUSE_D)
                last = intra_df.iloc[-1]
                eod_px[code] = float(last['close'])
                if not exited:
                    result = check_eod(pos, float(last['close']), d)
                    if result:
                        trade = eng.do_sell(pos, result[0], result[1], ed=d, et=last['time_str'], prec=prec)
                        if trade: eng.trades.append(trade); eng.eod_exits += 1
            else:
                # Fallback to daily
                sd = daily_data.get(d, {}).get(code)
                if sd:
                    prec = "daily_ohlc" if mode != "daily_close" else "daily_close"
                    result = check_bar(pos, {'high':sd['high'],'low':sd['low'],'close':sd['close'],'time_str':'15:00'})
                    if result and mode != "daily_close":
                        ep, reason = result[0], result[1]
                        partial = result[2] if len(result) > 2 else None
                        trade = eng.do_sell(pos, ep, reason, partial, d, "15:00", prec)
                        if trade:
                            eng.trades.append(trade); eng.intra_exits += 1; exited = True
                            if trade.return_pct <= 0: eng.closses += 1
                            else: eng.closses = 0; eng.pause_until = None
                            if eng.closses >= LOSS_S2: eng.pause_until = d + timedelta(days=PAUSE_D)
                    if not exited:
                        result = check_eod(pos, sd['close'], d)
                        if result:
                            trade = eng.do_sell(pos, result[0], result[1], ed=d, et="15:00", prec=prec)
                            if trade: eng.trades.append(trade); eng.eod_exits += 1
                    eod_px[code] = sd['close']
                else:
                    eod_px[code] = pos.entry_price

        eng.positions = {k:v for k,v in eng.positions.items() if v.is_active}

        # ── 买入 ──
        if d in sig_by_date and (eng.pause_until is None or d > eng.pause_until):
            for code, price in sig_by_date[d][:50]:
                if any(t.code == code and (d-t.entry_date).days <= SAME_COOLDOWN for t in eng.trades):
                    continue
                prec = "daily_close"; entry_px = price
                if mode in ("hybrid","min5"):
                    df = None
                    if mode == "hybrid":
                        df = load_intraday_cached(code, eng.cache_min1, MIN1_DIR)
                        if df is not None:
                            dd = df[df['date'] == d]
                            if len(dd) >= 10 and not pd.isna(dd.iloc[0].get('open',float('nan'))):
                                entry_px = float(dd.iloc[0]['open']); prec = "1min"
                    if prec == "daily_close":
                        df = load_intraday_cached(code, eng.cache_min5, MIN5_DIR)
                        if df is not None:
                            dd = df[df['date'] == d]
                            if len(dd) >= 10 and not pd.isna(dd.iloc[0].get('open',float('nan'))):
                                entry_px = float(dd.iloc[0]['open']); prec = "5min"
                    if prec == "daily_close" and mode != "daily_close":
                        sd = daily_data.get(d,{}).get(code)
                        if sd: entry_px = sd['open']; prec = "daily_ohlc"
                if pd.isna(entry_px) or entry_px <= 0: continue
                eng.do_buy(d, code, entry_px, "09:30", prec)

        eng.record(d, eod_px)

        if (day_i+1) % 200 == 0:
            print(f"  {d} | {day_i+1}/{len(trading_dates)} | 净�?{eng.eq(eod_px):,.0f} | "
                  f"持仓 {eng.npos()} | 退�?{eng.intra_exits+eng.eod_exits} | "
                  f"1min:{eng.stats.get('1min',0)} 5min:{eng.stats.get('5min',0)} ohlc:{eng.stats.get('daily_ohlc',0)} close:{eng.stats.get('daily_close',0)}")

    elapsed = time.time()-t0
    print(f"  完成({elapsed:.0f}s) 日内退出{eng.intra_exits} EOD{eng.eod_exits}")
    return eng, elapsed


# ══════════════════�?报告 ══════════════════�?
def compute_stats(eng, label, elapsed):
    eq_df = pd.DataFrame(eng.equity)
    if eq_df.empty or len(eng.trades) == 0:
        return {"label":label, "trades":0}
    fe = eq_df['equity'].iloc[-1]
    tr = (fe/INITIAL_CAPITAL-1)*100
    eq_df['cummax'] = eq_df['equity'].cummax()
    eq_df['dd'] = (eq_df['equity']-eq_df['cummax'])/eq_df['cummax']*100
    md = eq_df['dd'].min()
    days = max((BACKTEST_END-BACKTEST_START).days, 1)
    ann = ((fe/INITIAL_CAPITAL)**(365.25/days)-1)*100

    trades = eng.trades
    wins = [t for t in trades if t.return_pct > 0]
    loses = [t for t in trades if t.return_pct <= 0]
    n = len(trades); nw, nl = len(wins), len(loses)
    wr = nw/n*100 if n else 0
    aw = np.mean([t.return_pct for t in wins]) if wins else 0
    al = np.mean([t.return_pct for t in loses]) if loses else 0
    at_ = np.mean([t.return_pct for t in trades]) if trades else 0
    med = np.median([t.return_pct for t in trades]) if trades else 0
    tg_ = sum(t.return_pct for t in wins)
    tl_ = abs(sum(t.return_pct for t in loses))
    pf = tg_/tl_ if tl_ > 0 else float('inf')
    tp = sum(t.profit_amount for t in trades)
    ah = np.mean([t.hold_days for t in trades]) if trades else 0
    ed = Counter(t.exit_reason.split('(')[0] for t in trades)

    eq_df['year'] = pd.to_datetime(eq_df['date']).dt.year
    yearly = []
    for yr, g in eq_df.groupby('year'):
        if len(g) < 10: continue
        yearly.append({'year':int(yr), 'return':(g['equity'].iloc[-1]/g['equity'].iloc[0]-1)*100,
                       'max_dd':g['dd'].min(), 'end_equity':g['equity'].iloc[-1]})

    return {"label":label, "mode":eng.mode, "trades":n, "wins":nw, "losses":nl,
            "win_rate":wr, "avg_win":aw, "avg_loss":al, "avg_return":at_, "median_return":med,
            "final_equity":fe, "total_return":tr, "annual_return":ann, "max_dd":md,
            "profit_factor":pf, "total_profit":tp, "avg_hold":ah,
            "exit_reasons":ed, "yearly":yearly,
            "intraday_exits":eng.intra_exits, "eod_exits":eng.eod_exits,
            "precision_counts":dict(eng.stats), "equity_curve":eq_df, "trades_list":trades,
            "elapsed":elapsed}


def print_report(stats_list):
    print("\n"+"�?*78)
    print("�? MA5 角度策略 �?多精度回测报�?(2018-2026)")
    print("�?*78)
    print(f"\n  {'指标':<20}", end="")
    for s in stats_list: print(f" {s['label']:>14}", end="")
    print("\n  "+"-"*76)
    rows = [
        ("总成交笔�?, "trades", "d"), ("胜率(%)", "win_rate", ".1f"),
        ("总收益率(%)", "total_return", ".2f"), ("年化收益�?%)", "annual_return", ".2f"),
        ("最大回�?%)", "max_dd", ".2f"), ("盈亏�?PF)", "profit_factor", ".2f"),
        ("总盈亏额(�?", "total_profit", ",.0f"), ("均盈(%)", "avg_win", ".2f"),
        ("均亏(%)", "avg_loss", ".2f"), ("均笔(%)", "avg_return", ".2f"),
        ("均持(�?", "avg_hold", ".1f"), ("日内退�?, "intraday_exits", "d"),
        ("EOD退�?, "eod_exits", "d"), ("最终净�?�?", "final_equity", ",.0f"),
        ("耗时(�?", "elapsed", ".0f"),
    ]
    for label, key, fmt in rows:
        print(f"  {label:<20}", end="")
        for s in stats_list:
            if fmt == "d": print(f" {s[key]:>14,}", end="")
            elif fmt == ",.0f": print(f" {s[key]:>14,.0f}", end="")
            else: print(f" {s[key]:>14{fmt}}", end="")
        print()

    print(f"\n  ── 数据精度分布 ──")
    for s in stats_list:
        if s['precision_counts']:
            total = sum(s['precision_counts'].values())
            parts = [f"{k}:{v}({v/total*100:.0f}%)" for k,v in Counter(s['precision_counts']).most_common()]
            print(f"  {s['label']:<16} {', '.join(parts)}")

    s0 = stats_list[0]
    print(f"\n  ── 退出原�?(混合精度) ──")
    for reason, count in s0['exit_reasons'].most_common():
        print(f"  {reason:<32} {count:>6} ({count/s0['trades']*100:5.1f}%)")

    print(f"\n  ── 年度收益 ──")
    print(f"  {'年份':<8}", end="")
    for s in stats_list: print(f" {'收益%':>6} {'回撤%':>6}", end="")
    print()
    all_years = sorted(set(y['year'] for s in stats_list for y in s['yearly']))
    for yr in all_years:
        print(f"  {yr:<8}", end="")
        for s in stats_list:
            y = next((y for y in s['yearly'] if y['year']==yr), None)
            print(f" {y['return']:>+5.1f} {y['max_dd']:>5.1f}" if y else f" {'-':>6} {'-':>6}", end="")
        print()

    print("\n"+"�?*78)


# ══════════════════�?MAIN ══════════════════�?
def main():
    t_total = time.time()
    print("="*78)
    print("  MA5 角度策略 �?多精度回�?v2 (缓存优化)")
    print(f"  区间: {BACKTEST_START} ~ {BACKTEST_END}  初始: {INITIAL_CAPITAL:,}  单笔上限: {POSITION_CAP:,}")
    print("="*78)

    # 1. 加载日线
    bars = load_daily_bars()
    if bars.empty: print("ERROR: 无日�?"); return

    # 2. 信号
    print("[信号] 生成�?..", end=" ", flush=True); t0 = time.time()
    from app.screener.strategies.ma5_angle import generate_signals
    strat_bars = bars[(bars["date"]>=LOAD_START)&(bars["date"]<=BACKTEST_END)].copy()
    sig = generate_signals(strat_bars, **SIGNAL_PARAMS)
    sig = sig[(sig["date"]>=BACKTEST_START)&(sig["date"]<=BACKTEST_END)].copy()
    sig["date"] = pd.to_datetime(sig["date"]).dt.date
    sig = sig.sort_values(["date","code"])
    print(f"{len(sig):,}个信�?{sig['code'].nunique()}�?{time.time()-t0:.0f}s")

    # 3. 快照
    print("[准备] 日线快照...", end=" ", flush=True); t0 = time.time()
    bt = bars[(bars["date"]>=BACKTEST_START)&(bars["date"]<=BACKTEST_END)]
    daily_data = defaultdict(dict)
    daily_closes = defaultdict(dict)
    for d_, g in bt.groupby("date"):
        for _, r in g.iterrows():
            daily_data[d_][r['code']] = {'open':float(r['open']),'high':float(r['high']),
                                          'low':float(r['low']),'close':float(r['close'])}
            daily_closes[d_][r['code']] = float(r['close'])
    trading_dates = sorted(daily_closes.keys())
    print(f"{len(trading_dates)}�?{time.time()-t0:.0f}s")

    # 释放原始 bars
    del bars, bt, strat_bars

    # 4. 多模式回�?    all_stats = []
    for mode, label in [("hybrid","混合精度"), ("min5","5分钟�?),
                         ("daily_ohlc","日线OHLC"), ("daily_close","日线收盘�?)]:
        eng, elapsed = run_one_mode(mode, None, sig, daily_data, trading_dates, label)
        all_stats.append(compute_stats(eng, label, elapsed))

    # 5. 报告
    print_report(all_stats)

    print(f"\n  总耗时: {time.time()-t_total:.0f}s")
    print(f"  结果: {ROOT}/output/backtest/")


if __name__ == "__main__":
    main()
