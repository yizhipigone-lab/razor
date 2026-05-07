#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度策略 — 多精度回测 (2013-01-01 ~ 2026-05-01)
4 种精度模式对比：
  1. 混合精度 (1min > 5min > 日线OHLC)
  2. 5分钟线 (5min > 日线OHLC)
  3. 日线OHLC (用最高/最低价模拟盘中)
  4. 日线收盘价 (仅收盘价)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pandas as pd
import numpy as np
from datetime import date, timedelta, datetime
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from collections import Counter, defaultdict
import time
import warnings
warnings.filterwarnings('ignore')

# ═══════════════════ 配置 ═══════════════════
INITIAL_CAPITAL = 1_000_000
POSITION_CAP    = 50_000
HARD_STOP       = -0.055
TP1_PCT         = 0.04
TP1_RATIO       = 0.20
TP2_PCT         = 0.14
TRAIL_ACTIVATE  = 0.08
TRAIL_DD        = 0.02
TIME_EXIT       = 7
TIME_FORCE      = 10
LOSS_S1         = 3
LOSS_S2         = 5
PAUSE_D         = 3
MIN_BUY         = 5_000
SAME_STOCK_COOLDOWN = 20

BACKTEST_START  = date(2013, 1, 1)
BACKTEST_END    = date(2026, 5, 1)
BUFFER_DAYS     = 365
LOAD_START      = BACKTEST_START - timedelta(days=BUFFER_DAYS)

ROOT = Path(__file__).parent.parent
DAILY_DIR  = ROOT / "data" / "parquet" / "daily"
MIN1_DIR   = ROOT / "data" / "parquet" / "min1"
MIN5_DIR   = ROOT / "data" / "parquet" / "min5"
OUTPUT_DIR = ROOT / "output" / "backtest"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SIGNAL_PARAMS = {
    "version": "improved", "filter_st": True, "filter_bj": True,
    "sh_red_filter": False,
    "vol_threshold": 1.2, "close_position_threshold": 0.6,
    "disable_quality_sort": True,
    "filter_consecutive_up": False, "filter_gap_quality": False,
}
# ════════════════════════════════════════════


@dataclass
class Position:
    code: str
    entry_date: date
    entry_price: float
    entry_time: str
    shares: int
    cost: float
    peak_price: float = 0.0
    remaining_shares: int = 0
    tp1_done: bool = False
    tp2_done: bool = False
    is_active: bool = True

    def __post_init__(self):
        self.peak_price = self.entry_price
        self.remaining_shares = self.shares


@dataclass
class Trade:
    code: str
    entry_date: date
    entry_time: str
    exit_date: date
    exit_time: str
    entry_price: float
    exit_price: float
    shares: int
    return_pct: float
    profit_amount: float
    exit_reason: str
    hold_days: int
    precision: str = ""  # "1min" / "5min" / "daily_ohlc" / "daily_close"


# ═══════════════════ 数据加载 ═══════════════════

def load_daily_bars():
    """从 Parquet 加载全市场日线数据"""
    print("[加载] 日线数据...", end=" ", flush=True)
    t0 = time.time()
    files = list(DAILY_DIR.glob("*.parquet"))
    if not files:
        print("ERROR: 无日线文件!")
        return pd.DataFrame()

    dfs = []
    for f in files:
        try:
            code = f.stem
            # Filter: skip index files and non-stock codes
            if code.startswith('index_') or not (len(code) == 6 and code.isdigit()):
                continue
            df = pd.read_parquet(str(f))
            df['code'] = code  # Force code from filename, fix None values in parquet
            dfs.append(df)
        except:
            pass

    bars = pd.concat(dfs, ignore_index=True)
    for c in ["open", "high", "low", "close", "volume"]:
        if c in bars:
            bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=["close"])
    if "date" in bars.columns:
        bars["date"] = pd.to_datetime(bars["date"]).dt.date
    bars = bars.sort_values(["code", "date"])
    print(f"{len(bars):,}行, {bars['code'].nunique()}只股票, {time.time()-t0:.0f}s")
    return bars


def load_sh_index():
    """加载上证指数日线"""
    sh_path = DAILY_DIR / "index_000001.parquet"
    if not sh_path.exists():
        sh_path = DAILY_DIR / "000001.parquet"
    if not sh_path.exists():
        return pd.DataFrame()
    sh = pd.read_parquet(str(sh_path))
    if 'date' in sh.columns:
        sh['date'] = pd.to_datetime(sh['date']).dt.date
    sh = sh.sort_values('date')
    return sh


def load_intraday(code: str, d: date, min_dir: Path = None) -> pd.DataFrame:
    """加载某只股票某一天的日内数据"""
    if min_dir is None:
        # Try 1-min first, then 5-min
        for mdir in [MIN1_DIR, MIN5_DIR]:
            result = load_intraday(code, d, mdir)
            if not result.empty:
                return result
        return pd.DataFrame()

    f = min_dir / f"{code}.parquet"
    if not f.exists():
        # Also try with SH/SZ suffix
        for suffix in ['SH', 'SZ']:
            alt = min_dir / f"{code}{suffix}.parquet"
            if alt.exists():
                f = alt
                break
        else:
            return pd.DataFrame()

    try:
        df = pd.read_parquet(str(f))
        if 'datetime' not in df.columns or df.empty:
            return pd.DataFrame()
        df['datetime'] = pd.to_datetime(df['datetime'])
        day_mask = df['datetime'].dt.date == d
        if day_mask.sum() < 10:  # need minimum bars
            return pd.DataFrame()
        result = df[day_mask].copy()
        result['time_str'] = result['datetime'].dt.strftime('%H:%M')
        return result.sort_values('datetime')
    except:
        return pd.DataFrame()


# ═══════════════════ 风控检查 ═══════════════════

def check_intraday_stop(pos: Position, bar: dict) -> Optional[Tuple]:
    """用一根K线的OHLC检查止损/止盈，返回 (exit_price, reason, partial_qty)"""
    high = float(bar.get('high', bar.get('close', 0)))
    low = float(bar.get('low', bar.get('close', 0)))
    close = float(bar.get('close', 0))
    minute = bar.get('time_str', '')

    if high > pos.peak_price:
        pos.peak_price = high

    pp = pos.peak_price / pos.entry_price - 1
    cp = close / pos.entry_price - 1
    rem = pos.remaining_shares

    # 1. 硬止损: Low触及 -5.5%
    if low <= pos.entry_price * (1 + HARD_STOP):
        ep = pos.entry_price * (1 + HARD_STOP)
        return (ep, f"硬止损({(ep/pos.entry_price-1)*100:.1f}%)", None)

    # 2. TP2: +14%
    if not pos.tp2_done and high >= pos.entry_price * (1 + TP2_PCT):
        ep = pos.entry_price * (1 + TP2_PCT)
        return (ep, f"TP2 +14%({cp*100:.1f}%)", None)

    # 3. TP1: +4% → 卖20%
    if not pos.tp1_done and high >= pos.entry_price * (1 + TP1_PCT):
        ss = int(rem * TP1_RATIO / 100) * 100
        if ss >= 100:
            ep = pos.entry_price * (1 + TP1_PCT)
            return (ep, f"TP1 +4%({cp*100:.1f}%)", ss)

    # 4. 移动止盈: 峰值≥+8% 且 Low回撤≥2%
    if pp >= TRAIL_ACTIVATE and low <= pos.peak_price * (1 - TRAIL_DD):
        ep = pos.peak_price * (1 - TRAIL_DD)
        return (ep, f"移动止盈(峰{pp*100:.1f}%回采)", None)

    # 5. 保本: 曾盈利≥3%, Low触及成本
    if pp >= 0.03 and low <= pos.entry_price:
        return (pos.entry_price, f"保本(曾+{pp*100:.1f}%)", None)

    return None


def check_eod_stop(pos: Position, close: float, d: date) -> Optional[Tuple]:
    """收盘检查（时间止损），返回 (price, reason) 或 None"""
    cp = close / pos.entry_price - 1
    hd = (d - pos.entry_date).days
    if hd > TIME_FORCE:
        return (close, f"时间强制({hd}天)")
    if hd > TIME_EXIT and cp > 0.01:
        return (close, f"时间条件({hd}天+{cp*100:.1f}%)")
    return None


# ═══════════════════ 回测引擎 ═══════════════════

class BacktestEngine:
    def __init__(self, mode: str, trading_dates: list):
        self.mode = mode  # "hybrid" / "min5" / "daily_ohlc" / "daily_close"
        self.cash = INITIAL_CAPITAL
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity: List[dict] = []
        self.consecutive_losses = 0
        self.pause_until = None
        self.trading_dates = trading_dates

        # Statistics trackers
        self.intraday_exits = 0
        self.eod_exits = 0
        self.precision_used = Counter()  # which precision was actually used per trade
        self.missing_data = 0

    def total_equity(self, prices: dict):
        pv = sum(p.remaining_shares * prices.get(p.code, p.entry_price)
                 for p in self.positions.values() if p.is_active)
        return self.cash + pv

    def pos_count(self):
        return len([p for p in self.positions.values() if p.is_active])

    def max_pos_size(self):
        return POSITION_CAP / 2 if self.consecutive_losses >= LOSS_S1 else POSITION_CAP

    def _td(self, d1, d2):
        return sum(1 for td in self.trading_dates if d1 <= td <= d2)

    def execute_sell(self, pos, price, reason, partial=None, exit_date=None, exit_time="", precision=""):
        ss = partial if partial else pos.remaining_shares
        ss = int(ss // 100 * 100)
        if ss <= 0:
            return None
        rp = (price / pos.entry_price - 1) * 100
        profit = ss * (price - pos.entry_price)
        pos.remaining_shares -= ss
        if "TP2" in reason:
            pos.tp2_done = True
        if "TP1" in reason:
            pos.tp1_done = True
        if pos.remaining_shares <= 0:
            pos.is_active = False
            pos.remaining_shares = 0
        self.cash += ss * price
        hold_days = self._td(pos.entry_date, exit_date) if exit_date else 0
        return Trade(pos.code, pos.entry_date, pos.entry_time,
                     exit_date or date.today(), exit_time,
                     pos.entry_price, price, ss, rp, profit, reason,
                     hold_days, precision)

    def execute_buy(self, d, code, price, time_str="", precision=""):
        if code in self.positions:
            return None
        ma = min(self.max_pos_size(), self.cash)
        if ma < MIN_BUY:
            return None
        shares = int(ma / price / 100) * 100
        # For high-price stocks: if position size < 100 shares, buy 100 shares if affordable
        if shares < 100 and self.cash >= price * 100:
            shares = 100
        if shares < 100:
            return None
        cost = shares * price
        if cost > self.cash:
            return None
        pos = Position(code, d, price, time_str, shares, cost)
        self.cash -= cost
        self.positions[code] = pos
        self.precision_used[precision] += 1
        return pos

    def record(self, d, prices):
        eq = self.total_equity(prices)
        self.equity.append({'date': d, 'equity': eq, 'cash': self.cash, 'positions': self.pos_count()})

    def get_intraday_dir(self):
        if self.mode == "hybrid":
            return None  # Try 1-min first
        elif self.mode == "min5":
            return MIN5_DIR
        return None  # daily modes


# ═══════════════════ 主回测逻辑 ═══════════════════

def run_backtest(mode: str, bars: pd.DataFrame, signals_df: pd.DataFrame,
                 daily_data: dict, trading_dates: list) -> Tuple[BacktestEngine, float]:
    """
    mode: "hybrid" | "min5" | "daily_ohlc" | "daily_close"
    """
    t0 = time.time()
    label = {"hybrid": "混合精度", "min5": "5分钟线", "daily_ohlc": "日线OHLC", "daily_close": "日线收盘价"}
    print(f"\n{'='*60}")
    print(f"  [{label.get(mode, mode)}] 回测中...")
    print(f"{'='*60}")

    engine = BacktestEngine(mode, trading_dates)

    # Build signal lookup: date → [(code, close_price), ...]
    sig_by_date: Dict[date, List[Tuple[str, float]]] = defaultdict(list)
    for _, r in signals_df.iterrows():
        sig_by_date[r['date']].append((r['code'], float(r['close'])))

    no_intraday = 0
    used_1min = 0
    used_5min = 0
    used_daily_ohlc = 0
    used_daily_close = 0

    for day_idx, d in enumerate(trading_dates):
        held_codes = [p.code for p in engine.positions.values() if p.is_active]
        eod_prices = {}

        # ── 处理持仓：检查是否需要卖出 ──
        for code, pos in list(engine.positions.items()):
            if not pos.is_active:
                continue

            intraday = pd.DataFrame()
            prec = "daily_close"

            if mode in ("hybrid", "min5"):
                # Try 1-min first (hybrid mode only)
                if mode == "hybrid":
                    intraday = load_intraday(code, d, MIN1_DIR)

                # Fallback to 5-min
                if intraday.empty:
                    intraday = load_intraday(code, d, MIN5_DIR)

                if not intraday.empty:
                    # Determine which precision was used
                    if mode == "hybrid":
                        # Check if it came from min1 or min5
                        min1_test = load_intraday(code, d, MIN1_DIR)
                        if not min1_test.empty:
                            prec = "1min"
                        else:
                            prec = "5min"
                    else:
                        prec = "5min"

            if intraday.empty and mode in ("hybrid", "min5", "daily_ohlc"):
                # Fallback to daily OHLC simulation
                stock_day = daily_data.get(d, {}).get(code)
                if stock_day:
                    prec = "daily_ohlc"
                    result = check_intraday_stop(pos, {
                        'high': stock_day['high'], 'low': stock_day['low'],
                        'close': stock_day['close'], 'time_str': '15:00'
                    })
                    if result:
                        ep, reason, partial = result
                        trade = engine.execute_sell(pos, ep, reason, partial, d, "15:00", prec)
                        if trade:
                            engine.trades.append(trade)
                            engine.intraday_exits += 1
                            if trade.return_pct <= 0:
                                engine.consecutive_losses += 1
                            else:
                                engine.consecutive_losses = 0
                                engine.pause_until = None
                            if engine.consecutive_losses >= LOSS_S2:
                                engine.pause_until = d + timedelta(days=PAUSE_D)
                            eod_prices[code] = ep
                            used_daily_ohlc += 1
                            continue

                    # EOD check
                    eod_result = check_eod_stop(pos, stock_day['close'], d)
                    if eod_result:
                        ep, reason = eod_result
                        trade = engine.execute_sell(pos, ep, reason, exit_date=d, exit_time="15:00", precision=prec)
                        if trade:
                            engine.trades.append(trade)
                            engine.eod_exits += 1
                        used_daily_ohlc += 1

                eod_prices[code] = stock_day['close'] if stock_day else pos.entry_price
                continue

            if intraday.empty:
                # Pure daily_close mode or no data at all
                stock_day = daily_data.get(d, {}).get(code)
                if stock_day:
                    prec = "daily_close"
                    eod_result = check_eod_stop(pos, stock_day['close'], d)
                    if eod_result:
                        ep, reason = eod_result
                        trade = engine.execute_sell(pos, ep, reason, exit_date=d, exit_time="15:00", precision=prec)
                        if trade:
                            engine.trades.append(trade)
                            engine.eod_exits += 1
                    used_daily_close += 1
                eod_prices[code] = stock_day['close'] if stock_day else pos.entry_price
                continue

            # ── 有日内数据：逐根K线检查 ──
            exited = False
            for _, bar in intraday.iterrows():
                if exited:
                    break
                result = check_intraday_stop(pos, bar.to_dict())
                if result:
                    ep, reason, partial = result
                    trade = engine.execute_sell(pos, ep, reason, partial, d, bar['time_str'], prec)
                    if trade:
                        engine.trades.append(trade)
                        engine.intraday_exits += 1
                        exited = True
                        if trade.return_pct <= 0:
                            engine.consecutive_losses += 1
                        else:
                            engine.consecutive_losses = 0
                            engine.pause_until = None
                        if engine.consecutive_losses >= LOSS_S2:
                            engine.pause_until = d + timedelta(days=PAUSE_D)

            last_bar = intraday.iloc[-1]
            eod_prices[code] = float(last_bar['close'])

            if not exited:
                eod_result = check_eod_stop(pos, float(last_bar['close']), d)
                if eod_result:
                    ep, reason = eod_result
                    trade = engine.execute_sell(pos, ep, reason, exit_date=d,
                                                  exit_time=last_bar['time_str'], precision=prec)
                    if trade:
                        engine.trades.append(trade)
                        engine.eod_exits += 1

            if prec == "1min":
                used_1min += 1
            elif prec == "5min":
                used_5min += 1

        # 清理已平仓
        engine.positions = {k: v for k, v in engine.positions.items() if v.is_active}

        # ── 买入新信号 ──
        if d in sig_by_date and (engine.pause_until is None or d > engine.pause_until):
            for code, price in sig_by_date[d][:50]:
                if any(t.code == code and (d - t.entry_date).days <= SAME_STOCK_COOLDOWN
                       for t in engine.trades):
                    continue

                # Determine precision for entry
                prec = "daily_close"
                if mode in ("hybrid", "min5"):
                    intra = load_intraday(code, d, MIN1_DIR if mode == "hybrid" else None)
                    if intra.empty:
                        intra = load_intraday(code, d, MIN5_DIR)
                    if not intra.empty:
                        open_px = float(intra.iloc[0].get('open', float('nan')))
                        if not pd.isna(open_px) and open_px > 0:
                            price = open_px
                            prec = "1min" if (mode == "hybrid" and not load_intraday(code, d, MIN1_DIR).empty) else "5min"
                    if prec == "daily_close" and mode != "daily_close":
                        stock_day = daily_data.get(d, {}).get(code)
                        if stock_day:
                            price = stock_day['open']
                            prec = "daily_ohlc"

                if pd.isna(price) or price <= 0:
                    continue
                engine.execute_buy(d, code, price, "09:30", prec)

        engine.record(d, eod_prices)

        if (day_idx + 1) % 200 == 0:
            eq = engine.total_equity(eod_prices)
            int_ex = engine.intraday_exits
            print(f"  {d} | {day_idx+1}/{len(trading_dates)} | 净值 {eq:,.0f} | "
                  f"持仓 {engine.pos_count()} | 日内退出 {int_ex} | "
                  f"1min:{used_1min} 5min:{used_5min} ohlc:{used_daily_ohlc} close:{used_daily_close}")

    elapsed = time.time() - t0
    print(f"  完成 ({elapsed:.0f}s) | 日内退出:{engine.intraday_exits} EOD退出:{engine.eod_exits} "
          f"1min:{used_1min} 5min:{used_5min} ohlc:{used_daily_ohlc} close:{used_daily_close}")

    return engine, elapsed


# ═══════════════════ 报告生成 ═══════════════════

def compute_stats(engine: BacktestEngine, label: str, elapsed: float):
    eq_df = pd.DataFrame(engine.equity)
    if eq_df.empty or len(engine.trades) == 0:
        return {"label": label, "trades": 0, "final_equity": INITIAL_CAPITAL,
                "total_return": 0, "annual_return": 0, "max_dd": 0}

    fe = eq_df['equity'].iloc[-1]
    tr = (fe / INITIAL_CAPITAL - 1) * 100
    eq_df['cummax'] = eq_df['equity'].cummax()
    eq_df['dd'] = (eq_df['equity'] - eq_df['cummax']) / eq_df['cummax'] * 100
    md = eq_df['dd'].min()
    days = (BACKTEST_END - BACKTEST_START).days
    ann = ((fe / INITIAL_CAPITAL) ** (365.25 / max(days, 1)) - 1) * 100

    trades = engine.trades
    wins = [t for t in trades if t.return_pct > 0]
    loses = [t for t in trades if t.return_pct <= 0]
    n = len(trades)
    nw, nl = len(wins), len(loses)
    wr = nw / n * 100 if n > 0 else 0
    aw = np.mean([t.return_pct for t in wins]) if wins else 0
    al = np.mean([t.return_pct for t in loses]) if loses else 0
    at_ = np.mean([t.return_pct for t in trades]) if trades else 0
    med = np.median([t.return_pct for t in trades]) if trades else 0
    tg_ = sum(t.return_pct for t in wins)
    tl_ = abs(sum(t.return_pct for t in loses))
    pf = tg_ / tl_ if tl_ > 0 else float('inf')
    tp = sum(t.profit_amount for t in trades)
    ah = np.mean([t.hold_days for t in trades]) if trades else 0
    ah_win = np.mean([t.hold_days for t in wins]) if wins else 0
    ah_lose = np.mean([t.hold_days for t in loses]) if loses else 0

    # Exit reasons
    ed = Counter(t.exit_reason.split('(')[0] for t in trades)

    # Yearly stats
    eq_df['year'] = pd.to_datetime(eq_df['date']).dt.year
    yearly = []
    for yr, g in eq_df.groupby('year'):
        if len(g) < 10:
            continue
        y_ret = (g['equity'].iloc[-1] / g['equity'].iloc[0] - 1) * 100
        y_dd = g['dd'].min()
        yearly.append({'year': int(yr), 'return': y_ret, 'max_dd': y_dd, 'end_equity': g['equity'].iloc[-1]})

    # Precision breakdown
    prec_counts = Counter(t.precision for t in trades)

    return {
        "label": label, "mode": engine.mode,
        "trades": n, "wins": nw, "losses": nl,
        "win_rate": wr, "avg_win": aw, "avg_loss": al,
        "avg_return": at_, "median_return": med,
        "final_equity": fe, "total_return": tr, "annual_return": ann,
        "max_dd": md, "profit_factor": pf, "total_profit": tp,
        "avg_hold": ah, "avg_hold_win": ah_win, "avg_hold_lose": ah_lose,
        "exit_reasons": ed, "yearly": yearly,
        "intraday_exits": engine.intraday_exits, "eod_exits": engine.eod_exits,
        "precision_counts": prec_counts,
        "equity_curve": eq_df, "trades_list": trades,
        "elapsed": elapsed,
    }


def print_report(stats_list: list):
    """Print detailed comparison report"""
    print("\n")
    print("█" * 78)
    print("█  MA5 角度策略 — 多精度回测报告 (2013-2026)")
    print("█" * 78)

    # Summary table
    print(f"\n  {'指标':<20}", end="")
    for s in stats_list:
        print(f" {s['label']:>14}", end="")
    print()
    print("  " + "-" * 76)

    rows = [
        ("总成交笔数", "trades", "d"),
        ("盈利/亏损笔数", None, "wl"),
        ("胜率(%)", "win_rate", ".1f"),
        ("总收益率(%)", "total_return", ".2f"),
        ("年化收益率(%)", "annual_return", ".2f"),
        ("最大回撤(%)", "max_dd", ".2f"),
        ("盈亏比(PF)", "profit_factor", ".2f"),
        ("总盈亏额(元)", "total_profit", ",.0f"),
        ("均盈(%)", "avg_win", ".2f"),
        ("均亏(%)", "avg_loss", ".2f"),
        ("均笔收益(%)", "avg_return", ".2f"),
        ("中位收益(%)", "median_return", ".2f"),
        ("均持(天)", "avg_hold", ".1f"),
        ("均持盈(天)", "avg_hold_win", ".1f"),
        ("均持亏(天)", "avg_hold_lose", ".1f"),
        ("日内退出", "intraday_exits", "d"),
        ("EOD退出", "eod_exits", "d"),
        ("最终净值(元)", "final_equity", ",.0f"),
        ("耗时(秒)", "elapsed", ".0f"),
    ]

    for label, key, fmt in rows:
        print(f"  {label:<20}", end="")
        for s in stats_list:
            if key is None:  # win/loss row
                val = f"{s['wins']}/{s['losses']}"
                print(f" {val:>14}", end="")
            elif fmt == "d":
                print(f" {s[key]:>14,}", end="")
            elif fmt == ",.0f":
                print(f" {s[key]:>14,.0f}", end="")
            else:
                print(f" {s[key]:>14{fmt}}", end="")
        print()

    # Precision usage
    print(f"\n  ── 数据精度使用分布 ──")
    for s in stats_list:
        if s['precision_counts']:
            total = sum(s['precision_counts'].values())
            parts = [f"{k}:{v}({v/total*100:.0f}%)" for k, v in s['precision_counts'].most_common()]
            print(f"  {s['label']:<16} {', '.join(parts)}")

    # Exit reasons (first mode only for brevity)
    print(f"\n  ── 退出原因分布 (混合精度) ──")
    s0 = stats_list[0]
    total = s0['trades']
    for reason, count in s0['exit_reasons'].most_common():
        print(f"  {reason:<32} {count:>6} ({count/total*100:5.1f}%)")

    # Yearly comparison
    print(f"\n  ── 年度收益对比 ──")
    print(f"  {'年份':<8}", end="")
    for s in stats_list:
        print(f" {s['label']:>14}", end="")
    print()
    print(f"  {'':8} {'收益%':>6} {'回撤%':>6}" * len(stats_list))

    all_years = sorted(set(y['year'] for s in stats_list for y in s['yearly']))
    for yr in all_years:
        print(f"  {yr:<8}", end="")
        for s in stats_list:
            y = next((y for y in s['yearly'] if y['year'] == yr), None)
            if y:
                print(f" {y['return']:>+5.1f} {y['max_dd']:>5.1f}", end="")
            else:
                print(f" {'-':>6} {'-':>6}", end="")
        print()

    # Equity curve correlation
    print(f"\n  ── 净值曲线相关性 ──")
    eqs = {}
    for s in stats_list:
        eq_df = s['equity_curve']
        if not eq_df.empty:
            eqs[s['label']] = eq_df.set_index('date')['equity']
    if len(eqs) >= 2:
        labels = list(eqs.keys())
        for i in range(len(labels)):
            for j in range(i+1, len(labels)):
                common = eqs[labels[i]].index.intersection(eqs[labels[j]].index)
                if len(common) > 100:
                    corr = eqs[labels[i]].loc[common].corr(eqs[labels[j]].loc[common])
                    print(f"  {labels[i]} vs {labels[j]}: r = {corr:.4f}")

    print("\n" + "█" * 78)


# ═══════════════════ MAIN ═══════════════════

def main():
    t_total = time.time()
    print("=" * 78)
    print("  MA5 角度策略 — 多精度回测系统")
    print(f"  回测区间: {BACKTEST_START} ~ {BACKTEST_END}")
    print(f"  初始资金: {INITIAL_CAPITAL:,}  单笔上限: {POSITION_CAP:,}")
    print("=" * 78)

    # ── 1. 加载日线 ──
    bars = load_daily_bars()
    if bars.empty:
        print("ERROR: 无法加载日线数据!")
        return

    # ── 2. 生成信号 ──
    print("[信号] 生成MA5角度突破信号...", end=" ", flush=True)
    t0 = time.time()
    from app.screener.strategies.ma5_angle import generate_signals

    # Filter bars to load range for strategy calculation
    strat_bars = bars[(bars["date"] >= LOAD_START) & (bars["date"] <= BACKTEST_END)].copy()

    # Load SH index for bull market filter
    sh_index = load_sh_index()

    signals = generate_signals(strat_bars, **SIGNAL_PARAMS)
    if signals is None or signals.empty:
        print("ERROR: 无信号!")
        return
    signals = signals[(signals["date"] >= BACKTEST_START) & (signals["date"] <= BACKTEST_END)].copy()
    signals["date"] = pd.to_datetime(signals["date"]).dt.date
    signals = signals.sort_values(["date", "code"])
    print(f"{len(signals):,}个信号, {signals['code'].nunique()}只股票, {time.time()-t0:.0f}s")

    # ── 3. 构建快照 ──
    print("[准备] 构建日线快照...", end=" ", flush=True)
    t0 = time.time()
    bt_bars = bars[(bars["date"] >= BACKTEST_START) & (bars["date"] <= BACKTEST_END)]
    daily_data: Dict[date, Dict[str, dict]] = defaultdict(dict)
    daily_closes: Dict[date, Dict[str, float]] = defaultdict(dict)
    for d, g in bt_bars.groupby("date"):
        for _, r in g.iterrows():
            daily_data[d][r['code']] = {
                'open': float(r['open']), 'high': float(r['high']),
                'low': float(r['low']), 'close': float(r['close'])
            }
            daily_closes[d][r['code']] = float(r['close'])
    trading_dates = sorted(daily_closes.keys())
    print(f"{len(trading_dates)}个交易日, {time.time()-t0:.0f}s")

    # ── 4. 多模式回测 ──
    all_stats = []
    modes = ["hybrid", "min5", "daily_ohlc", "daily_close"]

    for mode in modes:
        engine, elapsed = run_backtest(mode, bars, signals, daily_data, trading_dates)
        stats = compute_stats(engine,
            {"hybrid": "混合精度", "min5": "5分钟线", "daily_ohlc": "日线OHLC", "daily_close": "日线收盘价"}[mode],
            elapsed)
        all_stats.append(stats)

    # ── 5. 报告 ──
    print_report(all_stats)

    # ── 6. 保存 ──
    print(f"\n[保存] 结果写入 {OUTPUT_DIR} ...")
    for s in all_stats:
        if not s['equity_curve'].empty:
            s['equity_curve'].to_parquet(str(OUTPUT_DIR / f"equity_{s['mode']}.parquet"), index=False)
        if s.get('trades_list'):
            trades_df = pd.DataFrame([{
                'code': t.code, 'entry_date': t.entry_date, 'exit_date': t.exit_date,
                'entry_price': t.entry_price, 'exit_price': t.exit_price,
                'return_pct': t.return_pct, 'profit': t.profit_amount,
                'exit_reason': t.exit_reason, 'hold_days': t.hold_days,
                'precision': t.precision, 'entry_time': t.entry_time, 'exit_time': t.exit_time,
            } for t in s['trades_list']])
            trades_df.to_parquet(str(OUTPUT_DIR / f"trades_{s['mode']}.parquet"), index=False)

    total_elapsed = time.time() - t_total
    print(f"\n  全部完成! 总耗时: {total_elapsed:.0f}s ({total_elapsed/60:.1f}分钟)")
    print(f"  输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
