#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MA5 角度策略 — 5分钟线精确回测 (2023-01-01 ~ 2026-05-02)
使用 baostock 5分钟数据 + QMT 1分钟数据混合方案
止损/止盈用日内精确价位检查，更真实地模拟执行
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import pandas as pd
import numpy as np
from datetime import date, timedelta, datetime
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from collections import Counter
import time
import warnings
warnings.filterwarnings('ignore')

from database.duckdb_manager import db, PARQUET_DAILY_DIR
from app.screener.strategies.ma5_angle import generate_signals

# ═══════════════════════════════════════════════════════════════
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
MIN_BUY         = 5000

BACKTEST_START = date(2024, 1, 1)
BACKTEST_END   = date(2026, 5, 7)
BUFFER_DAYS    = 365
LOAD_START     = BACKTEST_START - timedelta(days=BUFFER_DAYS)

# 数据目录
MIN5_BS_DIR = Path(__file__).parent.parent / "data" / "parquet" / "min5_bs"
MIN1_QM_DIR = Path(__file__).parent.parent / "data" / "parquet" / "min1"
MIN5_MERGED_DIR = Path(__file__).parent.parent / "data" / "parquet" / "min5"  # TDX+QMT 合并
MIN5_BS_DIR.mkdir(parents=True, exist_ok=True)

SIGNAL_PARAMS = {
    "version": "improved", "filter_st": True, "filter_bj": True,
    "sh_red_filter": False,  # 回测中不需要
    "vol_threshold": 1.5, "close_position_threshold": 0.8,
    "disable_quality_sort": True,
    "filter_consecutive_up": False, "filter_gap_quality": False,
}
# ═══════════════════════════════════════════════════════════════


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


def load_intraday_data(code: str, d: date) -> pd.DataFrame:
    """
    加载单只股票某一天的日内数据
    优先级: QMT 1分钟 > TDX+QMT合并5分钟 > baostock 5分钟缓存
    """
    # 尝试 QMT 1分钟数据
    qmt_path = MIN1_QM_DIR / f"{code}.parquet"
    if qmt_path.exists():
        try:
            df = pd.read_parquet(str(qmt_path))
            if 'datetime' in df.columns and not df.empty:
                df['datetime'] = pd.to_datetime(df['datetime'])
                day_mask = df['datetime'].dt.date == d
                if day_mask.sum() >= 48:  # 至少有半天数据
                    result = df[day_mask].copy()
                    result['time_str'] = result['datetime'].dt.strftime('%H:%M')
                    return result.sort_values('datetime')
        except:
            pass

    # 尝试 TDX+QMT 合并5分钟数据
    merged_path = MIN5_MERGED_DIR / f"{code}.parquet"
    if merged_path.exists():
        try:
            df = pd.read_parquet(str(merged_path))
            if 'datetime' in df.columns and not df.empty:
                df['datetime'] = pd.to_datetime(df['datetime'])
                day_mask = df['datetime'].dt.date == d
                if day_mask.sum() > 0:
                    result = df[day_mask].copy()
                    result['time_str'] = result['datetime'].dt.strftime('%H:%M')
                    return result.sort_values('datetime')
        except:
            pass

    # 尝试 baostock 5分钟数据（缓存）
    bs_path = MIN5_BS_DIR / f"{code}.parquet"
    if bs_path.exists():
        try:
            df = pd.read_parquet(str(bs_path))
            if 'datetime' in df.columns and not df.empty:
                df['datetime'] = pd.to_datetime(df['datetime'])
                day_mask = df['datetime'].dt.date == d
                if day_mask.sum() > 0:
                    result = df[day_mask].copy()
                    result['time_str'] = result['datetime'].dt.strftime('%H:%M')
                    return result.sort_values('datetime')
        except:
            pass

    # 没有日内数据：返回空 DataFrame
    return pd.DataFrame()


def download_bs_min5(code: str, d: date) -> pd.DataFrame:
    """从 baostock 实时下载5分钟数据（带缓存）"""
    out_path = MIN5_BS_DIR / f"{code}.parquet"

    # 尝试获取当月数据
    import baostock as bs
    if not hasattr(download_bs_min5, '_logged_in'):
        bs.login()
        download_bs_min5._logged_in = True

    # 判断交易所
    if code.startswith(('6', '5', '9')):
        bs_code = f'sh.{code}'
    else:
        bs_code = f'sz.{code}'

    m_start = d.strftime('%Y-%m-01')
    # 下个月第一天
    if d.month == 12:
        m_end = f'{d.year+1}-01-01'
    else:
        m_end = f'{d.year}-{d.month+1:02d}-01'

    try:
        rs = bs.query_history_k_data_plus(
            bs_code, 'date,time,open,high,low,close,volume,amount',
            start_date=m_start, end_date=m_end,
            frequency='5', adjustflag='3'
        )
        if rs.error_code != '0':
            return pd.DataFrame()

        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=['date','time','open','high','low','close','volume','amount'])
        df['datetime'] = pd.to_datetime(df['date'] + ' ' + df['time'])
        for c in ['open','high','low','close','volume','amount']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna(subset=['open','high','low','close'])
        df = df.drop(columns=['date', 'time'])

        # 合并缓存
        if out_path.exists():
            old = pd.read_parquet(str(out_path))
            old['datetime'] = pd.to_datetime(old['datetime'])
            merged = pd.concat([old, df]).drop_duplicates('datetime').sort_values('datetime')
        else:
            merged = df
        merged.to_parquet(str(out_path), index=False)

        # 返回当天数据
        day_mask = merged['datetime'].dt.date == d
        result = merged[day_mask].copy()
        result['time_str'] = result['datetime'].dt.strftime('%H:%M')
        return result.sort_values('datetime')

    except Exception as e:
        return pd.DataFrame()


def check_intraday_stop(pos: Position, bar: dict, d: date) -> Optional[Tuple]:
    """检查单根K线是否触发止损/止盈"""
    high = float(bar['high'])
    low = float(bar['low'])
    close = float(bar['close'])
    minute = bar.get('time_str', '')

    if high > pos.peak_price:
        pos.peak_price = high
    pp = pos.peak_price / pos.entry_price - 1
    cp = close / pos.entry_price - 1
    rem = pos.remaining_shares

    # 1. 硬止损 -5.5%: 用这根K线的Low检查
    if low <= pos.entry_price * (1 + HARD_STOP):
        exit_px = pos.entry_price * (1 + HARD_STOP)
        return (exit_px, f"硬止损({(exit_px/pos.entry_price-1)*100:.1f}%)", None)

    # 2. TP2: +14% 清仓
    if not pos.tp2_done and cp >= TP2_PCT:
        return (close, f"TP2 +14%({cp*100:.1f}%)", None)

    # 3. TP1: +4% 卖20%
    if not pos.tp1_done and cp >= TP1_PCT:
        ss = int(rem * TP1_RATIO / 100) * 100
        if ss >= 100:
            return (close, f"TP1 +4%({cp*100:.1f}%)", ss)

    # 4. 移动止盈: 盈利>8%后回撤2%
    if pp >= TRAIL_ACTIVATE:
        # 用这根K线的Low检查
        dd_low = low / pos.peak_price - 1
        if dd_low <= -TRAIL_DD:
            trail_price = pos.peak_price * (1 - TRAIL_DD)
            return (min(trail_price, close), f"移动止盈(峰{pp*100:.1f}%回{dd_low*100:.1f}%)", None)

    # 5. 保本: Low触及成本价（已盈利>3%后）
    if pp >= 0.03 and low <= pos.entry_price:
        return (pos.entry_price, f"保本(曾+{pp*100:.1f}%)", None)

    return None


def check_eod_stop(pos: Position, close: float, d: date) -> Optional[Tuple]:
    """收盘检查（时间止损）"""
    cp = close / pos.entry_price - 1
    hd = (d - pos.entry_date).days

    if hd > TIME_FORCE:
        return (close, f"时间强制({hd}天)")
    if hd > TIME_EXIT and cp > 0.01:
        return (close, f"时间条件({hd}天+{cp*100:.1f}%)")
    return None


class IntradayBacktestEngine:
    def __init__(self, trading_dates):
        self.cash = INITIAL_CAPITAL
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity = []
        self.consecutive_losses = 0
        self.pause_until = None
        self.trading_dates = trading_dates

    def total_equity(self, prices):
        pv = sum(p.remaining_shares * prices.get(p.code, p.entry_price)
                 for p in self.positions.values() if p.is_active)
        return self.cash + pv

    def pos_count(self):
        return len([p for p in self.positions.values() if p.is_active])

    def max_pos(self):
        return POSITION_CAP / 2 if self.consecutive_losses >= LOSS_S1 else POSITION_CAP

    def _td(self, d1, d2):
        return sum(1 for td in self.trading_dates if d1 <= td <= d2)

    def execute_sell(self, pos, price, reason, partial=None, exit_date=None, exit_time=""):
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
        return Trade(pos.code, pos.entry_date, pos.entry_time, exit_date or date.today(),
                     exit_time, pos.entry_price, price, ss, rp, profit, reason, 0)

    def execute_buy(self, d, code, price, time_str=""):
        if code in self.positions:
            return None
        ma = min(self.max_pos(), self.cash)
        if ma < MIN_BUY:
            return None
        shares = int(ma / price / 100) * 100
        if shares < 100:
            return None
        cost = shares * price
        if cost > self.cash:
            return None
        pos = Position(code, d, price, time_str, shares, cost)
        self.cash -= cost
        self.positions[code] = pos
        return pos

    def record(self, d, prices):
        eq = self.total_equity(prices)
        self.equity.append({'date': d, 'equity': eq, 'cash': self.cash, 'positions': self.pos_count()})


def run_intraday_backtest():
    t0 = time.time()
    print("=" * 72)
    print("  MA5 角度策略 — 5分钟/1分钟混合精确回测")
    print(f"  区间: {BACKTEST_START} ~ {BACKTEST_END}")
    print("=" * 72)

    # ── 1. 加载日线 + 生成信号 ──────────────────────────
    print(f"\n[1/4] 加载日线 + 生成信号 ...")
    bars = db.load_all_bars(freq="daily", start=LOAD_START, end=BACKTEST_END)
    bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
    for c in ["open", "high", "low", "close", "volume"]:
        if c in bars:
            bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=["close"])
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    bars = bars.sort_values(["code", "date"])

    sig = generate_signals(bars, **SIGNAL_PARAMS)
    sig = sig[(sig["date"] >= BACKTEST_START) & (sig["date"] <= BACKTEST_END)].copy()
    sig["date"] = pd.to_datetime(sig["date"]).dt.date
    sig = sig.sort_values(["date", "code"])
    print(f"  信号: {len(sig):,}")

    # ── 2. 构建快照 ──────────────────────────────────
    bt_bars = bars[(bars["date"] >= BACKTEST_START) & (bars["date"] <= BACKTEST_END)]
    daily_closes, daily_highs = {}, {}
    for d, g in bt_bars.groupby("date"):
        daily_closes[d] = dict(zip(g['code'], g['close']))
        daily_highs[d] = dict(zip(g['code'], g['high']))
    trading_dates = sorted(daily_closes.keys())

    sig_by_date: Dict[date, List[str]] = {}
    for _, r in sig.iterrows():
        sig_by_date.setdefault(r['date'], []).append(r['code'])

    # ── 3. 逐日回测（用日内数据） ──────────────────────
    print(f"\n[2/4] 日内精确回测 ({len(trading_dates)}天) ...")
    engine = IntradayBacktestEngine(trading_dates)
    intraday_exits = 0
    eod_exits = 0
    skipped_days = 0
    missing_data_count = 0

    for day_idx, d in enumerate(trading_dates):
        held_codes = [p.code for p in engine.positions.values() if p.is_active]
        signal_codes = sig_by_date.get(d, [])
        needed = list(set(held_codes + signal_codes))

        eod_prices = {}
        eod_times = {}

        # ── 处理每个持仓：检查日内数据 ──────────────────
        for code, pos in list(engine.positions.items()):
            if not pos.is_active:
                continue

            intraday = load_intraday_data(code, d)

            if intraday.empty:
                # baostock 实时下载已禁用（使用预缓存的 merged min5 数据）
                missing_data_count += 1
                # 没有日内数据：用日线Low检查硬止损，Close检查其他
                dc = daily_closes.get(d, {}).get(code)
                if dc is not None and dc > 0:
                    dh = daily_highs.get(d, {}).get(code, dc)
                    # 用日最低价模拟
                    day_bar = daily_closes.copy()
                    # 获取该股票在当日的日线
                    stock_row = bt_bars[(bt_bars['code']==code) & (bt_bars['date']==d)]
                    if not stock_row.empty:
                        sr = stock_row.iloc[0]
                        result = check_intraday_stop(pos, {
                            'high': float(sr['high']), 'low': float(sr['low']),
                            'close': float(sr['close']), 'time_str': '15:00'
                        }, d)
                        if result:
                            exit_price, reason, partial = result
                            trade = engine.execute_sell(pos, exit_price, reason, partial, d, "15:00")
                            if trade:
                                trade.hold_days = engine._td(pos.entry_date, d)
                                engine.trades.append(trade)
                                intraday_exits += 1
                                if trade.return_pct <= 0:
                                    engine.consecutive_losses += 1
                                else:
                                    engine.consecutive_losses = 0
                                    engine.pause_until = None
                                if engine.consecutive_losses >= LOSS_S2:
                                    engine.pause_until = d + timedelta(days=PAUSE_D)
                                continue

                    # EOD检查
                    eod_result = check_eod_stop(pos, dc, d)
                    if eod_result:
                        exit_price, reason = eod_result
                        trade = engine.execute_sell(pos, exit_price, reason, exit_date=d, exit_time="15:00")
                        if trade:
                            trade.hold_days = engine._td(pos.entry_date, d)
                            engine.trades.append(trade)
                            eod_exits += 1

                missing_data_count += 1
                eod_prices[code] = dc or pos.entry_price
                eod_times[code] = "15:00"
                continue

            # 有日内数据：逐根K线检查
            exited = False
            for _, bar in intraday.iterrows():
                if exited:
                    break
                result = check_intraday_stop(pos, bar.to_dict(), d)
                if result:
                    ep, reason, partial = result
                    trade = engine.execute_sell(pos, ep, reason, partial, d, bar['time_str'])
                    if trade:
                        trade.hold_days = engine._td(pos.entry_date, d)
                        engine.trades.append(trade)
                        intraday_exits += 1
                        exited = True
                        if trade.return_pct <= 0:
                            engine.consecutive_losses += 1
                        else:
                            engine.consecutive_losses = 0
                            engine.pause_until = None
                        if engine.consecutive_losses >= LOSS_S2:
                            engine.pause_until = d + timedelta(days=PAUSE_D)

            # 记录收盘价
            last_bar = intraday.iloc[-1]
            eod_prices[code] = float(last_bar['close'])
            eod_times[code] = last_bar['time_str']

            # 日内未退出，收盘检查
            if not exited:
                eod_result = check_eod_stop(pos, float(last_bar['close']), d)
                if eod_result:
                    ep, reason = eod_result
                    trade = engine.execute_sell(pos, ep, reason, exit_date=d, exit_time=last_bar['time_str'])
                    if trade:
                        trade.hold_days = engine._td(pos.entry_date, d)
                        engine.trades.append(trade)
                        eod_exits += 1

        # 清理
        engine.positions = {k: v for k, v in engine.positions.items() if v.is_active}

        # ── 买入新信号 ──────────────────────────────────
        if d in sig_by_date:
            paused = engine.pause_until and d <= engine.pause_until
            if not paused:
                for code in sig_by_date[d][:50]:  # 每日最多50个新买入
                    price = daily_closes.get(d, {}).get(code)
                    if price is None:
                        continue
                    if any(t.code == code and d - t.entry_date <= timedelta(days=20)
                           for t in engine.trades):
                        continue
                    engine.execute_buy(d, code, price, "15:00")

        engine.record(d, eod_prices)

        if (day_idx + 1) % 100 == 0:
            eq = engine.total_equity(eod_prices)
            print(f"  {d} | {day_idx+1}/{len(trading_dates)} | "
                  f"净值 {eq:,.0f} | 持仓 {engine.pos_count()} | "
                  f"日内退出 {intraday_exits} | EOD退出 {eod_exits} | 缺数据 {missing_data_count}")

    # ── 4. 报告 ────────────────────────────────────────
    elapsed = time.time() - t0
    print(f"\n[3/4] 生成报告 (耗时 {elapsed:.0f}s) ...")

    eq_df = pd.DataFrame(engine.equity)
    if eq_df.empty:
        print("  无交易!")
        return

    fe = eq_df['equity'].iloc[-1]
    tr = (fe / INITIAL_CAPITAL - 1) * 100
    eq_df['cummax'] = eq_df['equity'].cummax()
    eq_df['dd'] = (eq_df['equity'] - eq_df['cummax']) / eq_df['cummax'] * 100
    md = eq_df['dd'].min()
    days = (BACKTEST_END - BACKTEST_START).days
    ann = ((fe / INITIAL_CAPITAL) ** (365.25 / days) - 1) * 100

    trades = engine.trades
    if trades:
        wins = [t for t in trades if t.return_pct > 0]
        loses = [t for t in trades if t.return_pct <= 0]
        n = len(trades)
        nw = len(wins)
        nl = len(loses)
        wr = nw / n * 100
        aw = np.mean([t.return_pct for t in wins]) if wins else 0
        al = np.mean([t.return_pct for t in loses]) if loses else 0
        at_ = np.mean([t.return_pct for t in trades])
        med = np.median([t.return_pct for t in trades])
        tg_ = sum(t.return_pct for t in wins)
        tl_ = abs(sum(t.return_pct for t in loses))
        pf = tg_ / tl_ if tl_ > 0 else float('inf')
        tp = sum(t.profit_amount for t in trades)
        ah = np.mean([t.hold_days for t in trades])
        ed = Counter(t.exit_reason.split('(')[0] for t in trades)
        # 日内vs收盘
        intra_count = sum(1 for t in trades if t.exit_time != "15:00")
        eod_count = sum(1 for t in trades if t.exit_time == "15:00")
    else:
        n = nw = nl = 0
        wr = aw = al = at_ = med = pf = ah = tp = 0
        ed = Counter()
        intra_count = eod_count = 0

    # 按年统计
    eq_df['year'] = pd.to_datetime(eq_df['date']).dt.year
    y_agg = eq_df.groupby('year').agg(start=('equity','first'), end=('equity','last'), dd=('dd','min'))
    y_agg['ret'] = (y_agg['end'] / y_agg['start'] - 1) * 100

    print("\n" + "=" * 72)
    print("  5分钟/1分钟混合精确回测报告")
    print("=" * 72)

    print(f"\n  ┌─────────────────────────────────────────────────┐")
    print(f"  │ 初始资金: {INITIAL_CAPITAL:>13,}                     │")
    print(f"  │ 最终净值: {fe:>13,.0f}                     │")
    print(f"  │ 总收益: {tr:>+12.2f}%  年化: {ann:>+7.2f}%                │")
    print(f"  │ 最大回撤: {md:>12.2f}%                          │")
    print(f"  │ PF: {pf:>10.2f}  胜率: {wr:>7.1f}%                    │")
    print(f"  │ 总盈利: {tp:>+13,.0f}                           │")
    print(f"  │ 耗时: {elapsed:>10.0f}s  缺数据天数: {missing_data_count}              │")
    print(f"  └─────────────────────────────────────────────────┘")

    print(f"\n  ┌─────────────────────────────────────────────────┐")
    print(f"  │ 总成交: {n:>6}笔  盈: {nw:<6}笔  亏: {nl:<6}笔              │")
    print(f"  │ 均盈: {aw:>+9.2f}%  均亏: {al:>+9.2f}%                      │")
    print(f"  │ 均笔: {at_:>+9.2f}%  中位: {med:>+9.2f}%                      │")
    print(f"  │ 均持: {ah:>9.1f}天  日内退出: {intra_count:>6}  EOD退出: {eod_count:>6}       │")
    print(f"  └─────────────────────────────────────────────────┘")

    print(f"\n  [退出分布]")
    print(f"  {'原因':<32} {'笔数':>6} {'占比':>8}")
    print(f"  {'-'*48}")
    for reason, count in ed.most_common():
        print(f"  {reason:<32} {count:>6} {count/n*100:>7.1f}%")

    print(f"\n  [年度表现]")
    print(f"  {'年份':<6} {'收益%':>10} {'回撤%':>10}")
    for yr, row in y_agg.iterrows():
        print(f"  {int(yr):<6} {row['ret']:>+9.2f} {row['dd']:>9.2f}")

    return eq_df, trades


if __name__ == "__main__":
    run_intraday_backtest()
