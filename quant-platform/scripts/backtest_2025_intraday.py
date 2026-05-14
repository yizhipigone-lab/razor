#!/usr/bin/env python
"""
5分钟线 OHLC 仿真回测：2025-01-01 至今
逐根K线检查止损/止盈，与日线回测对比"盘中触发 vs 尾盘触发"的差异
"""
import sys, os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd, numpy as np
from datetime import date, timedelta, datetime
from collections import defaultdict, Counter
from pathlib import Path
import time, gc, warnings
warnings.filterwarnings('ignore')

from app.sim_trader.config import *
# 从 TAKE_PROFIT_TIERS 提取传统变量名（config 已废弃独立 TP1_PCT/TP2_PCT）
TP1_PCT = TAKE_PROFIT_TIERS[0]["profit_pct"]
TP1_SELL_RATIO = TAKE_PROFIT_TIERS[0]["sell_ratio"]
TP2_PCT = TAKE_PROFIT_TIERS[1]["profit_pct"]
from app.screener.strategies.ma5_angle import generate_signals

START = date(2024, 1, 1)
END   = date.today()

ROOT  = Path(__file__).parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
MIN5_DIR  = ROOT / "data" / "parquet" / "min5"

# ==================================================================
class Position:
    __slots__ = ('code','entry_date','entry_price','shares','cost',
                 'peak_price','remaining','tp1','tp2','active','strategy')
    def __init__(self, code, d, px, shares, cost, strategy=""):
        self.code = code; self.entry_date = d; self.entry_price = px
        self.shares = shares; self.cost = cost
        self.peak_price = px; self.remaining = shares
        self.tp1 = False; self.tp2 = False; self.active = True
        self.strategy = strategy

class Trade:
    __slots__ = ('code','entry_date','exit_date','entry_px','exit_px',
                 'shares','ret','profit','reason','hold','strategy','timing')
    def __init__(self, code, ed, xd, ep, xp, sh, ret, profit, reason, hold,
                 strategy="", timing="close"):
        self.code = code; self.entry_date = ed; self.exit_date = xd
        self.entry_px = ep; self.exit_px = xp; self.shares = sh
        self.ret = ret; self.profit = profit; self.reason = reason
        self.hold = hold; self.strategy = strategy; self.timing = timing

class Engine:
    def __init__(self, td_list):
        self.cash = INITIAL_CAPITAL
        self.positions = {}; self.trades = []; self.equity = []
        self.cl = 0; self.pause = None; self.td_list = td_list
        self.halves = []; self.pauses = []
        self.intraday_exits = 0
        self.eod_exits = 0

    def max_pos(self):
        return POSITION_SIZE/2 if self.cl >= LOSS_STREAK_HALVE else POSITION_SIZE

    def pos_n(self):
        return sum(1 for p in self.positions.values() if p.active)

    def eq(self, prices):
        pv = 0
        for p in self.positions.values():
            if not p.active: continue
            bar = prices.get(p.code, {})
            if isinstance(bar, dict):
                px = bar.get('close', p.entry_price)
            else:
                px = bar if bar else p.entry_price
            pv += p.remaining * px
        return self.cash + pv

    def _td(self, d1, d2):
        return sum(1 for td in self.td_list if d1 <= td <= d2)

    def buy(self, d, code, px):
        if code in self.positions: return None
        ma = min(self.max_pos(), self.cash)
        if ma < MIN_BUY_AMT: return None
        sh = int(ma/px/100)*100
        if sh < 100: return None
        cost = sh * px
        if cost > self.cash: return None
        p = Position(code, d, px, sh, cost, STRATEGY_NAME)
        self.cash -= cost; self.positions[code] = p
        return p

    def sell(self, p, px, reason, partial=None, xd=None, timing="intraday"):
        ss = partial if partial else p.remaining
        ss = int(ss//100*100)
        if ss <= 0: return None
        ret = (px/p.entry_price-1)*100
        profit = ss*(px-p.entry_price)
        p.remaining -= ss
        if "TP2" in reason: p.tp2 = True
        if "TP1" in reason: p.tp1 = True
        if p.remaining <= 0: p.active = False; p.remaining = 0
        self.cash += ss*px
        if timing == "intraday":
            self.intraday_exits += 1
        else:
            self.eod_exits += 1
        return Trade(p.code, p.entry_date, xd or date.today(),
                     p.entry_price, px, ss, ret, profit, reason, 0,
                     p.strategy, timing)

    def check_bar(self, p, d, bar):
        """逐根5分钟K线检查止损/止盈"""
        o = bar['open']; h = bar['high']; l = bar['low']; c = bar['close']
        if h > p.peak_price: p.peak_price = h
        pp = p.peak_price/p.entry_price-1
        cur = c/p.entry_price-1

        # 1. 硬止损: Low触及 -5.5%
        if l <= p.entry_price * (1+HARD_STOP):
            return (p.entry_price*(1+HARD_STOP), f"HS({HARD_STOP*100:.1f}%)", None)
        # 2. TP2: +14% 清仓
        if not p.tp2 and cur >= TP2_PCT:
            return (c, f"TP2({cur*100:.1f}%)", None)
        # 3. TP1: +4% 卖20%
        if not p.tp1 and cur >= TP1_PCT:
            ss = int(p.remaining*TP1_SELL_RATIO/100)*100
            if ss >= 100:
                return (c, f"TP1({cur*100:.1f}%)", ss)
        # 4. 移动止盈: 盈利>8%后从峰回撤2%
        if pp >= TRAIL_ACTIVATE:
            dd_low = l/p.peak_price-1
            if dd_low <= -TRAIL_DD:
                tp = p.peak_price*(1-TRAIL_DD)
                return (min(tp, c), f"TR({pp*100:.1f}%>{dd_low*100:.1f}%)", None)
        return None

    def check_eod(self, p, d, close):
        """收盘时间止损"""
        cur = close/p.entry_price-1
        hd = self._td(p.entry_date, d)
        if cur <= HARD_STOP:
            return (close, f"HS({cur*100:.1f}%)", None)
        if hd > TIME_FORCE_DAYS:
            return (close, f"TF({hd}d)", None)
        if hd > TIME_EXIT_DAYS and cur > TIME_EXIT_PROFIT:
            return (close, f"TC({hd}d+{cur*100:.1f}%)", None)
        return None

    def record_trade(self, t):
        self.trades.append(t)
        if t.ret <= 0:
            self.cl += 1
            if self.cl == LOSS_STREAK_HALVE:
                self.halves.append((t.exit_date, self.cl, t.code, t.ret))
            if self.cl >= LOSS_STREAK_PAUSE:
                self.pause = t.exit_date + timedelta(days=PAUSE_DAYS)
                self.pauses.append((t.exit_date, self.pause, self.cl))
        else:
            self.cl = 0; self.pause = None

    def record(self, d, prices):
        eq = self.eq(prices)
        self.equity.append({'date':d,'equity':eq,'cash':self.cash,'pos':self.pos_n()})

# ==================================================================
def load_daily():
    print("[1/4] Loading daily bars...")
    files = [f for f in DAILY_DIR.glob("*.parquet")
             if not f.stem.startswith('index_') and len(f.stem)==6 and f.stem.isdigit()]
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(str(f))
        except Exception:
            continue
        cmap = {}
        for c in df.columns:
            cl = c.lower()
            if cl in ('vol','volume') and 'volume' not in df.columns:
                cmap[c] = 'volume'
            elif cl in ('trade_date','datetime') and c != 'date' and 'date' not in df.columns:
                cmap[c] = 'date'
        if cmap:
            df.rename(columns=cmap, inplace=True)
        df = df.loc[:, ~df.columns.duplicated()].copy()
        keep = [c for c in ['date','open','high','low','close','volume'] if c in df.columns]
        if 'date' not in keep or 'close' not in keep:
            continue
        df = df[keep].copy()
        df['code'] = f.stem
        df['date'] = pd.to_datetime(df['date']).dt.date
        dfs.append(df)
    bars = pd.concat(dfs, ignore_index=True)
    for c in ['open','high','low','close','volume']:
        if c in bars.columns: bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=['close'])
    bars = bars[(bars['date']>=date(2024,6,1))&(bars['date']<=END)]
    bars = bars.sort_values(['code','date']).reset_index(drop=True)
    print(f"  {bars.code.nunique():,} stocks, {len(bars):,} rows")
    return bars

def gen_signals(bars):
    sig = generate_signals(bars, **SIGNAL_PARAMS)
    sig = sig[(sig['date']>=START)&(sig['date']<=END)].copy()
    sig['date'] = pd.to_datetime(sig['date']).dt.date
    return sig.sort_values(['date','code'])

# 缓存：code → {date: DataFrame}
_min5_cache = {}
def _find_min5_file(code):
    # 优先带后缀的（最新同步的数据），再试无后缀
    suffix = "SH" if code.startswith(('6','5','9')) else "SZ"
    fp = MIN5_DIR / f"{code}{suffix}.parquet"
    if fp.exists():
        return fp
    fp2 = MIN5_DIR / f"{code}.parquet"
    if fp2.exists():
        return fp2
    return None

def load_min5_for_code(code, d):
    """加载单只股票某一天的5分钟线（从两种命名文件中查找）"""
    if code not in _min5_cache:
        # 尝试两种命名，合并数据
        suffix = "SH" if code.startswith(('6','5','9')) else "SZ"
        df_all = []
        for stem in [f"{code}{suffix}", code]:
            fp = MIN5_DIR / f"{stem}.parquet"
            if fp.exists():
                try:
                    df = pd.read_parquet(str(fp))
                    if 'datetime' in df.columns and not df.empty:
                        df['datetime'] = pd.to_datetime(df['datetime'])
                        for c in ['open','high','low','close']:
                            if c in df.columns:
                                df[c] = pd.to_numeric(df[c], errors='coerce')
                        df['date'] = df['datetime'].dt.date
                        df_clean = df.dropna(subset=['open','high','low','close']).sort_values('datetime')
                        if not df_clean.empty:
                            df_all.append(df_clean)
                except Exception:
                    pass

        if not df_all:
            _min5_cache[code] = {}
        else:
            merged = pd.concat(df_all, ignore_index=True)
            merged = merged.drop_duplicates('datetime').sort_values('datetime')
            grouped = {}
            for dt, g in merged.groupby('date'):
                if not g.empty:
                    grouped[dt] = g
            _min5_cache[code] = grouped

    return _min5_cache[code].get(d)

def run_bt(name, bars, sig):
    print(f"\n{'='*60}")
    print(f"  {name}  |  {START} ~ {END}  |  信号 {len(sig):,}")
    print(f"{'='*60}")
    bt = bars[(bars['date']>=START)&(bars['date']<=END)]

    # 每日快照
    closes = {}
    for d,g in bt.groupby('date'):
        closes[d]=dict(zip(g['code'],g['close']))
    td = sorted(closes.keys())
    td_set = set(td)
    print(f"  交易日: {len(td)}")

    # 信号按日分组
    sbd = defaultdict(list)
    for _,r in sig.iterrows():
        sbd[r['date']].append((r['code'],float(r['close'])))

    eng = Engine(td)
    missing_min5 = 0
    intraday_bars_checked = 0

    for i,d in enumerate(td):
        # ── 卖出阶段：用 5分钟线逐根检查 ──
        for code, p in list(eng.positions.items()):
            if not p.active or p.remaining <= 0:
                continue

            # 加载当日5分钟线
            bars_5m = load_min5_for_code(code, d)

            if bars_5m is None or bars_5m.empty:
                # 没有5分钟线：回退到日线收盘价检查
                missing_min5 += 1
                cp = closes.get(d, {}).get(code, p.entry_price)
                result = eng.check_eod(p, d, cp)
                if result:
                    ep, reason, partial = result
                    t = eng.sell(p, ep, reason, partial, d, timing="close")
                    if t:
                        t.hold = eng._td(p.entry_date, d)
                        eng.record_trade(t)
                continue

            # 逐根K线检查
            exited = False
            for _, bar in bars_5m.iterrows():
                if exited:
                    break
                intraday_bars_checked += 1
                bd = {'open':float(bar['open']),'high':float(bar['high']),
                      'low':float(bar['low']),'close':float(bar['close'])}
                result = eng.check_bar(p, d, bd)
                if result:
                    ep, reason, partial = result
                    t = eng.sell(p, ep, reason, partial, d, timing="intraday")
                    if t:
                        t.hold = eng._td(p.entry_date, d)
                        eng.record_trade(t)
                        exited = True

            # 日内未退出：收盘检查时间止损
            if not exited:
                last_close = float(bars_5m['close'].iloc[-1])
                result = eng.check_eod(p, d, last_close)
                if result:
                    ep, reason, partial = result
                    t = eng.sell(p, ep, reason, partial, d, timing="close")
                    if t:
                        t.hold = eng._td(p.entry_date, d)
                        eng.record_trade(t)

        # 清理已退出的持仓
        eng.positions = {k:v for k,v in eng.positions.items() if v.active}

        # ── 买入阶段：下一根5分钟线的open，这里用日线close近似 ──
        paused = eng.pause is not None and d <= eng.pause
        if d in sbd and not paused:
            for code,px in sbd[d]:
                if eng.cash < min(eng.max_pos(), MIN_BUY_AMT):
                    break
                if any(t.code==code and (d-t.entry_date).days<=SAME_STOCK_COOLDOWN
                       for t in eng.trades):
                    continue
                eng.buy(d, code, px)

        # ── 记录净值（用日线收盘价）──
        snap_close = {code: closes.get(d,{}).get(code,0) for code in eng.positions}
        eng.record(d, snap_close)

        if (i+1)%80==0:
            print(f"  {d} | {i+1}/{len(td)} | 净值 {eng.eq(snap_close):,.0f} | "
                  f"持仓 {eng.pos_n()} | 连亏 {eng.cl} | "
                  f"日内{eng.intraday_exits}/尾盘{eng.eod_exits}")

    print(f"\n  缺5分钟线: {missing_min5} 次 | 检查K线: {intraday_bars_checked:,} 根")
    return eng

def analyze(eng, name):
    trades = eng.trades
    eq = pd.DataFrame(eng.equity)
    if not trades or eq.empty:
        print("  无交易"); return
    fe = eq['equity'].iloc[-1]
    tr = (fe/INITIAL_CAPITAL-1)*100
    eq['cmax']=eq['equity'].cummax()
    eq['dd']=(eq['equity']-eq['cmax'])/eq['cmax']*100
    md=eq['dd'].min()
    n=len(trades); wins=[t for t in trades if t.ret>0]
    loses=[t for t in trades if t.ret<=0]
    nw,nl=len(wins),len(loses)
    wr=nw/n*100 if n>0 else 0
    aw=np.mean([t.ret for t in wins]) if wins else 0
    al=np.mean([t.ret for t in loses]) if loses else 0
    tp_=sum(t.profit for t in trades)

    print(f"\n  [总览] {name}")
    print(f"  净值 {fe:,.0f} | 总收益 {tr:+.2f}% | 最大回撤 {md:.2f}%")
    print(f"  成交 {n} | 盈{nw}/亏{nl} | 胜率{wr:.1f}% | 均盈{aw:+.2f}% 均亏{al:+.2f}% | 盈亏额{tp_:+,.0f}")
    print(f"  日内退出 {eng.intraday_exits} | 尾盘退出 {eng.eod_exits}")

    # 月度
    eq['month']=eq['date'].apply(lambda d: d.strftime('%Y-%m'))
    monthly=eq.groupby('month').agg(s=('equity','first'),e=('equity','last'),dd=('dd','min'))
    monthly['ret']=(monthly['e']/monthly['s']-1)*100
    tm=defaultdict(lambda:{'t':0,'w':0,'p':0.0})
    for t in trades:
        m=t.exit_date.strftime('%Y-%m'); tm[m]['t']+=1
        if t.ret>0: tm[m]['w']+=1
        tm[m]['p']+=t.profit
    print(f"\n  [月度盈亏]")
    print(f"  {'月':<8} {'起始':>11} {'期末':>11} {'收益%':>8} {'回撤%':>7} {'笔':>4} {'盈':>4} {'盈亏额':>11}")
    for m,r in monthly.iterrows():
        t=tm.get(m,{})
        print(f"  {m:<8} {r['s']:>11,.0f} {r['e']:>11,.0f} {r['ret']:>+7.2f} {r['dd']:>6.2f} "
              f"{t.get('t',0):>4} {t.get('w',0):>4} {t.get('p',0):>+11,.0f}")

    # 退出时机对比
    intra_t = [t for t in trades if t.timing=="intraday"]
    eod_t = [t for t in trades if t.timing=="close"]
    print(f"\n  [退出时机对比]")
    if intra_t:
        print(f"  盘中退出: {len(intra_t)}笔 均收益{np.mean([t.ret for t in intra_t]):+.2f}% 盈亏额{sum(t.profit for t in intra_t):+,.0f}")
    if eod_t:
        print(f"  尾盘退出: {len(eod_t)}笔 均收益{np.mean([t.ret for t in eod_t]):+.2f}% 盈亏额{sum(t.profit for t in eod_t):+,.0f}")

    # 退出原因
    rc=Counter(t.reason.split('(')[0] for t in trades)
    print(f"\n  [退出原因]")
    for r,c in rc.most_common():
        ar=np.mean([t.ret for t in trades if t.reason.startswith(r)])
        print(f"  {r:<8} {c:>5}笔 {c/n*100:>5.1f}% 均{ar:>+7.2f}%")

    # 连亏减仓
    print(f"\n  [连亏减仓: 连亏{LOSS_STREAK_HALVE}笔→半仓]")
    if eng.halves:
        for d,cl,code,rp in eng.halves[:15]:
            print(f"  {d} 连亏{cl}笔 触发股{code}({rp:+.2f}%)")
        if len(eng.halves)>15: print(f"  ... 共{len(eng.halves)}次")
    else: print(f"  无")

    # 暂停
    print(f"\n  [暂停交易: 连亏{LOSS_STREAK_PAUSE}笔→停{PAUSE_DAYS}天]")
    if eng.pauses:
        for d,pu,cl in eng.pauses[:15]:
            print(f"  {d} → {pu}  连亏{cl}笔")
        if len(eng.pauses)>15: print(f"  ... 共{len(eng.pauses)}次")
    else: print(f"  无")

    # 年度
    eq['year']=eq['date'].apply(lambda d: d.year)
    print(f"\n  [年度]")
    for yr,g in eq.groupby('year'):
        ret=(g['equity'].iloc[-1]/g['equity'].iloc[0]-1)*100
        yt=[t for t in trades if t.exit_date.year==yr]
        print(f"  {yr}: 收益{ret:>+8.2f}% 回撤{g['dd'].min():>+7.2f}% 交易{len(yt)}笔")

# ==================================================================
if __name__=="__main__":
    t0=time.time()
    bars=load_daily()
    print("[2/3] Generating signals...")
    sig=gen_signals(bars)
    print(f"  信号: {len(sig):,} ({sig.code.nunique():,} stocks, {sig.date.nunique()} days)")

    print("[3/3] Running 5-min intraday backtest...")
    eng=run_bt("[5分钟线仿真]", bars, sig)
    analyze(eng, "5分钟线仿真")
    print(f"\n  耗时 {time.time()-t0:.0f}s")
