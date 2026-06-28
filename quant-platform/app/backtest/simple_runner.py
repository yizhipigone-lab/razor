"""
轻量回测引擎 — 日线收盘价 + 日内逐Bar仿真
纯 parquet 数据源，无 DuckDB 依赖
"""
import pandas as pd
import numpy as np
from datetime import date, timedelta, datetime
from collections import defaultdict, Counter
from pathlib import Path
from typing import Callable, Optional
import json
from core.logger import get_logger
from app.backtest.execution import can_buy, calc_buy_cost, calc_sell_revenue

log = get_logger("SimpleBT")
ROOT = Path(__file__).resolve().parent.parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
STRATEGY_NAME = "ma5_angle"


class Position:
    __slots__ = ('code','entry_date','entry_price','shares','cost','peak_price',
                 'remaining','tp_triggered','active','strategy')
    def __init__(self, c, d, px, sh, cost, s=""):
        self.code=c; self.entry_date=d; self.entry_price=px; self.shares=sh
        self.cost=cost; self.peak_price=px; self.remaining=sh
        self.tp_triggered=set(); self.active=True; self.strategy=s


class Trade:
    __slots__ = ('code','entry_date','exit_date','entry_time','exit_time',
                 'entry_px','exit_px','shares','ret','profit','reason','hold')
    def __init__(self, c, ed, xd, ep, xp, sh, ret, profit, reason, hold,
                 et='09:30', xt='15:00'):
        self.code=c; self.entry_date=ed; self.exit_date=xd
        self.entry_time=et; self.exit_time=xt
        self.entry_px=ep; self.exit_px=xp; self.shares=sh
        self.ret=ret; self.profit=profit; self.reason=reason; self.hold=hold


class FastEngine:
    """快速回测引擎，所有参数显式传入"""

    def __init__(self, td_list, params):
        self.cash = params['initial_capital']
        self.position_size = params['position_size']
        self.min_buy = params.get('min_buy_amt', 5000)
        self.positions = {}
        self.trades = []
        self.equity = []
        self.cl = 0
        self.pause = None
        self.td_list = td_list
        self.p = params
        if 'take_profit_tiers' not in self.p:
            tiers = []
            if 'tp1_pct' in self.p:
                tiers.append({'profit_pct': self.p['tp1_pct'],
                              'sell_ratio': self.p.get('tp1_sell_ratio', 0.15)})
            if 'tp2_pct' in self.p:
                tiers.append({'profit_pct': self.p['tp2_pct'],
                              'sell_ratio': self.p.get('tp2_sell_ratio', 1.0)})
            self.p['take_profit_tiers'] = tiers

    def max_pos(self):
        if self.cl >= self.p.get('loss_streak_halve', 3):
            return self.position_size / 2
        return self.position_size

    def pos_n(self):
        return sum(1 for p in self.positions.values() if p.active)

    def _td(self, d1, d2):
        return sum(1 for t in self.td_list if d1 <= t <= d2)

    def eq(self, prices):
        pv = 0
        for p in self.positions.values():
            if not p.active: continue
            bar = prices.get(p.code, {})
            px = bar.get('close', p.entry_price) if isinstance(bar, dict) else (bar if bar else p.entry_price)
            pv += p.remaining * px
        return self.cash + pv

    def buy(self, d, code, px):
        if code in self.positions: return None
        # L28 修复: 统一成交执行层 - 涨停过滤
        # simple_runner 没有 prev_close 历史,简化处理:prev_close = px (无涨停判断)
        # 严格过滤由 strict_runner / engine 承担
        prev_close = px
        can_buy_ok, _ = can_buy(code, prev_close, px)
        if not can_buy_ok:
            return None
        ma = min(self.max_pos(), self.cash)
        if ma < self.min_buy: return None
        sh = int(ma / px / 100) * 100
        if sh < 100: return None
        # L28 修复: 统一成交执行层 - 买入成本(佣金+滑点)
        cost_result = calc_buy_cost(px, sh)
        cost = cost_result['total']
        if cost > self.cash: return None
        p = Position(code, d, px, sh, cost, STRATEGY_NAME)
        self.cash -= cost
        self.positions[code] = p
        return p

    def check_stops(self, d, snap, prev_snap=None):
        from app.backtest.exit_rules import exit_rule_engine, RuleContext, adjust_for_gap

        sells = []
        for code, p in list(self.positions.items()):
            if not p.active or p.remaining <= 0: continue
            bar = snap.get(code)
            if bar is None: continue
            cp = bar['close']; hp = bar.get('high', cp)
            if hp > p.peak_price: p.peak_price = hp
            hd = self._td(p.entry_date, d)

            if prev_snap:
                prev_bar = prev_snap.get(code)
                if prev_bar:
                    p.entry_price, p.peak_price = adjust_for_gap(
                        code, p.entry_price, p.peak_price,
                        cp, prev_bar.get('close', 0)
                    )

            ctx = exit_rule_engine.build_context(p, bar, hd, self.p, use_high_for_tp=True)
            signal = exit_rule_engine.check(ctx)
            if signal:
                if signal.reason.startswith('TP'):
                    idx = int(signal.reason[2]) - 1
                    p.tp_triggered.add(idx)
                if signal.sell_ratio < 1.0:
                    ss = int(p.shares * signal.sell_ratio / 100) * 100
                    if ss > p.remaining:
                        ss = int(p.remaining // 100 * 100)
                    if ss < 100:
                        ss = min(100, int(p.remaining))
                    sells.append((p, signal.sell_price, signal.reason, ss))
                else:
                    sells.append((p, signal.sell_price, signal.reason, None))
        return sells

    def sell(self, p, px, reason, partial=None, xd=None):
        ss = partial if partial else p.remaining
        ss = int(ss // 100 * 100)
        if ss < 100:
            ss = min(100, int(p.remaining))
        if ss <= 0: return None
        ss = min(ss, int(p.remaining))
        # L28 修复: 统一成交执行层 - 卖出净收入(扣佣金+印花+滑点)
        sell_rev = calc_sell_revenue(px, ss)
        revenue = sell_rev['total']
        cost_basis = ss * p.entry_price
        profit = revenue - cost_basis
        ret = (px / p.entry_price - 1) * 100
        p.remaining -= ss
        if p.remaining <= 0: p.active = False; p.remaining = 0
        self.cash += revenue
        return Trade(p.code, p.entry_date, xd or date.today(),
                     p.entry_price, px, ss, ret, profit, reason, 0)

    def sell_phase(self, d, snap, prev_snap=None):
        streak_pause = self.p.get('loss_streak_pause', 5)
        pause_days = self.p.get('pause_days', 3)
        if self.pause and d > self.pause:
            self.pause = None
            self.cl = 0
        for p, px, reason, partial in self.check_stops(d, snap, prev_snap):
            t = self.sell(p, px, reason, partial, d)
            if t:
                t.hold = self._td(p.entry_date, d)
                self.trades.append(t)
                if t.ret <= 0:
                    self.cl += 1
                    if self.cl >= streak_pause:
                        self.pause = d + timedelta(days=pause_days)
                else:
                    self.cl = 0
                    self.pause = None
        self.positions = {k: v for k, v in self.positions.items() if v.active}

    def record(self, d, prices):
        eq = self.eq(prices)
        self.equity.append({
            'date': str(d), 'equity': round(eq, 2),
            'cash': round(self.cash, 2), 'pos': self.pos_n()
        })


def load_daily_bars(start_buffer=date(2021, 9, 1), end=date.today()):
    """从 parquet 加载全市场日线 — 逐文件过滤避免 concat 内存溢出"""
    files = [f for f in DAILY_DIR.glob("*.parquet")
             if not f.stem.startswith('index_') and len(f.stem) == 6 and f.stem.isdigit()]
    chunks = []
    for f in files:
        try:
            df = pd.read_parquet(str(f))
        except Exception:
            continue
        cmap = {}
        for c in df.columns:
            cl = c.lower()
            if cl in ('vol', 'volume') and 'volume' not in df.columns:
                cmap[c] = 'volume'
            elif cl in ('trade_date', 'datetime') and c != 'date' and 'date' not in df.columns:
                cmap[c] = 'date'
        if cmap:
            df.rename(columns=cmap, inplace=True)
        if 'date' not in df.columns or 'close' not in df.columns:
            continue
        df = df[['date', 'open', 'high', 'low', 'close', 'volume']].copy()
        df['date'] = pd.to_datetime(df['date']).dt.date
        df = df[(df['date'] >= start_buffer) & (df['date'] <= end)]
        if len(df) == 0:
            continue
        df['code'] = f.stem
        for c in ['open', 'high', 'low', 'close', 'volume']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna(subset=['close'])
        if len(df) > 0:
            chunks.append(df)
    if not chunks:
        return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'volume', 'code'])
    bars = pd.concat(chunks, ignore_index=True)
    try:
        from database.duckdb_manager import db
        stock_df = db.get_all_stocks()
        if not stock_df.empty and 'name' in stock_df.columns:
            name_map = dict(zip(stock_df['code'], stock_df['name']))
            bars['name'] = bars['code'].map(name_map).fillna('')
    except Exception:
        bars['name'] = ''
    return bars.sort_values(['code', 'date']).reset_index(drop=True)


def load_index_data(start_date: date = None):
    """加载八大指数日线，归一化到 start_date（默认数据最早日）"""
    indices = {
        '上证指数': 'index_000001',
        '沪深300':  'index_000300',
        '中证500':  'index_000905',
        '中证1000': 'index_000852',
        '中证A500': 'index_000510',
        '创业板指': 'index_399006',
    }
    result = {}
    for name, fname in indices.items():
        fp = DAILY_DIR / f"{fname}.parquet"
        if not fp.exists():
            continue
        try:
            df = pd.read_parquet(str(fp))
            if 'trade_date' in df.columns:
                df['date'] = pd.to_datetime(df['trade_date']).dt.date
            elif 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date']).dt.date
            else:
                continue
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df = df.dropna(subset=['close']).sort_values('date')
            if not df.empty:
                if start_date:
                    base_rows = df[df['date'] >= start_date]
                    if not base_rows.empty:
                        base = float(base_rows['close'].iloc[0])
                    else:
                        base = float(df['close'].iloc[-1])
                else:
                    base = float(df['close'].iloc[0])
                df['norm'] = df['close'] / base
                if start_date:
                    df = df[df['date'] >= start_date]
                result[name] = [
                    {'date': str(d), 'close': float(c), 'norm': round(float(n), 4)}
                    for d, c, n in zip(df['date'], df['close'], df['norm'])
                ]
        except Exception:
            pass
    return result


# ═══════════════════════════════════════════════════════
# 日内回测（5分钟 / 1分钟）
# ═══════════════════════════════════════════════════════

def _run_intraday_backtest(
    bars: pd.DataFrame, signals_df: pd.DataFrame,
    start: date, end: date, params: dict,
    progress_cb=None, stop_event=None, stock_names=None,
    period: str = "5m",
) -> dict:
    """日内逐Bar回测：对每只有信号的股票，用分钟线逐Bar检查止盈止损"""
    from app.backtest.exit_rules import exit_rule_engine

    intraday_dir = ROOT / "data" / "parquet" / period

    # 预处理信号
    signals_df = signals_df[(signals_df['date'] >= start) & (signals_df['date'] <= end)].copy()
    signals_df['date'] = pd.to_datetime(signals_df['date']).dt.date
    sbd = defaultdict(list)
    for _, r in signals_df.iterrows():
        sbd[r['date']].append((r['code'], float(r['close'])))

    # 预加载日线 close/high/low/open
    bt = bars[(bars['date'] >= start) & (bars['date'] <= end)]
    closes, highs, lows, opens = {}, {}, {}, {}
    for d, g in bt.groupby('date'):
        closes[d] = dict(zip(g['code'], g['close']))
        highs[d] = dict(zip(g['code'], g['high']))
        lows[d] = dict(zip(g['code'], g['low']))
        opens[d] = dict(zip(g['code'], g['open']))
    td = sorted(closes.keys())

    # 预加载日内数据
    intraday_cache = {}
    signal_codes = set(signals_df['code'].unique())

    def _load_intraday(code):
        if code in intraday_cache:
            return intraday_cache[code]
        fp = intraday_dir / f"{code}.parquet"
        if not fp.exists():
            intraday_cache[code] = None
            return None
        try:
            df = pd.read_parquet(str(fp))
            if 'datetime' in df.columns:
                df['datetime'] = pd.to_datetime(df['datetime'])
            elif 'date' in df.columns:
                df['datetime'] = pd.to_datetime(df['date'])
            else:
                return None
            for c in ['open', 'high', 'low', 'close']:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors='coerce')
            df = df.dropna(subset=['close'])
            intraday_cache[code] = df
            return df
        except Exception:
            intraday_cache[code] = None
            return None

    # 预加载有信号的股票
    loaded = 0
    for code in signal_codes:
        if _load_intraday(code) is not None:
            loaded += 1
    log.info(f"日内回测: {loaded}/{len(signal_codes)}只有{period}数据")

    if progress_cb:
        progress_cb(0, 4, f"日内逐Bar回放 ({period}, {loaded}只)...")

    # 引擎状态
    cash = params['initial_capital']
    positions = {}
    trades_all = []
    equity_curve = []
    cooldown = {}
    sell_reasons = Counter()
    total_buy_signals = 0
    position_ratio = params.get('position_size', 50000) / params['initial_capital']

    # 逐日循环
    prev_snap = {}
    for d_idx, d in enumerate(td):
        if stop_event and stop_event.is_set():
            return {'status': 'stopped'}

        d_str = str(d)

        # 当日快照
        day_snap = {code: {'close': closes[d].get(code, 0),
                            'high': highs[d].get(code, closes[d].get(code, 0)),
                            'low': lows[d].get(code, closes[d].get(code, 0)),
                            'open': opens[d].get(code, closes[d].get(code, 0))}
                    for code in list(positions.keys()) + [c for c, _ in sbd.get(d, [])]
                    if code in closes[d]}

        # 先检查持仓
        for code, pos in list(positions.items()):
            if not pos.active or pos.remaining <= 0:
                continue
            intra_df = _load_intraday(code)
            if intra_df is None:
                # 无日内数据：用日线OHLC检查一次
                bar = day_snap.get(code, {})
                if bar:
                    cp, hp = bar.get('close', 0), bar.get('high', 0)
                    if hp > pos.peak_price:
                        pos.peak_price = hp
                    hd = sum(1 for t in td if pos.entry_date <= t <= d)
                    if prev_snap.get(code):
                        from app.backtest.exit_rules import adjust_for_gap
                        pos.entry_price, pos.peak_price = adjust_for_gap(
                            code, pos.entry_price, pos.peak_price,
                            cp, prev_snap[code].get('close', 0))
                    ctx = exit_rule_engine.build_context(pos, bar, hd, params, use_high_for_tp=True)
                    sig = exit_rule_engine.check(ctx)
                    if sig:
                        t = _execute_signal(pos, code, d, sig, cash, trades_all, sell_reasons, cooldown)
                        if t:
                            cash = t[0]
            else:
                # 有日内数据：逐Bar检查
                day_bars = intra_df[intra_df['datetime'].dt.date == d]
                if day_bars.empty:
                    continue
                for _, bar_row in day_bars.iterrows():
                    bar = {'open': float(bar_row.get('open', 0)),
                           'high': float(bar_row.get('high', 0)),
                           'low': float(bar_row.get('low', 0)),
                           'close': float(bar_row.get('close', 0))}
                    if bar['high'] > pos.peak_price:
                        pos.peak_price = bar['high']
                    hd = sum(1 for t in td if pos.entry_date <= t <= d)
                    ctx = exit_rule_engine.build_context(pos, bar, hd, params, use_high_for_tp=True)
                    sig = exit_rule_engine.check(ctx)
                    if sig:
                        t = _execute_signal(pos, code, d, sig, cash, trades_all, sell_reasons, cooldown)
                        if t:
                            cash = t[0]
                        break

        # 清理已平仓
        positions = {k: v for k, v in positions.items() if v.active}

        # 买入
        paused = False
        # 连败保护
        streak_pause = params.get('loss_streak_pause', 5)
        if len([t for t in trades_all if t.ret <= 0]) >= streak_pause:
            paused = True
        if not paused and d in sbd:
            dyn_size = cash * position_ratio
            for code, px in sbd[d]:
                if code in positions:
                    continue
                if code in cooldown and (d - cooldown[code]).days < params.get('same_stock_cooldown', 20):
                    continue
                if cash < dyn_size * 0.5:
                    break
                if px <= 0:
                    continue
                sh = int(dyn_size / px / 100) * 100
                if sh < 100:
                    continue
                # 任务一: 日内买入扣成本(佣金+滑点)，与日线 FastEngine.buy(:99) 一致
                _bc = calc_buy_cost(px, sh)
                cost = _bc['total']
                if cost > cash:
                    continue
                cash -= cost
                positions[code] = Position(code, d, px, sh, cost)
                total_buy_signals += 1

        # 记录净值
        pos_value = 0
        for pc, p in positions.items():
            pos_value += p.remaining * closes[d].get(pc, p.entry_price)
        equity_curve.append({
            'date': d_str, 'equity': round(cash + pos_value, 2),
            'cash': round(cash, 2), 'pos': len(positions),
        })
        prev_snap = day_snap

    # 最终清仓
    for code, p in list(positions.items()):
        if not p.active:
            continue
        px = closes.get(td[-1], {}).get(code, p.entry_price) if td else p.entry_price
        ret = (px / p.entry_price - 1) * 100
        profit = p.remaining * (px - p.entry_price)
        cash += p.remaining * px
        trades_all.append(Trade(code, p.entry_date, td[-1] if td else end,
                                p.entry_price, px, p.remaining,
                                round(ret, 2), round(profit, 0), "FE",
                                sum(1 for t in td if p.entry_date <= t <= td[-1]) if td else 0))
        sell_reasons["FE"] += 1

    # 构建结果
    indices = {}
    try:
        indices = load_index_data(start_date=start)
    except Exception:
        pass

    n = len(trades_all)
    wins = [t for t in trades_all if t.ret > 0]
    losses = [t for t in trades_all if t.ret <= 0]
    nw, nl = len(wins), len(losses)
    wr = nw / n * 100 if n > 0 else 0
    aw = np.mean([t.ret for t in wins]) if wins else 0
    al = np.mean([t.ret for t in losses]) if losses else 0
    gross_profit = sum(t.profit for t in wins)
    gross_loss = abs(sum(t.profit for t in losses)) if losses else 1
    pf = gross_profit / gross_loss if gross_loss > 0 else 0
    best_trade = max(trades_all, key=lambda t: t.ret) if trades_all else None
    worst_trade = min(trades_all, key=lambda t: t.ret) if trades_all else None
    avg_hold_win = np.mean([t.hold for t in wins]) if wins else 0
    avg_hold_loss = np.mean([t.hold for t in losses]) if losses else 0

    trades_json = []
    for t in trades_all:
        trades_json.append({
            'code': t.code, 'name': stock_names.get(t.code, '') if stock_names else '',
            'entry_date': str(t.entry_date), 'entry_time': getattr(t, 'entry_time', '09:30'),
            'exit_date': str(t.exit_date), 'exit_time': getattr(t, 'exit_time', '15:00'),
            'entry_px': round(float(t.entry_px), 2), 'exit_px': round(float(t.exit_px), 2),
            'shares': int(t.shares), 'ret_pct': round(float(t.ret), 2),
            'profit': round(float(t.profit), 0), 'entry_total': round(float(t.shares * t.entry_px), 0),
            'exit_total': round(float(t.shares * t.exit_px), 0), 'reason': t.reason,
            'hold_days': int(t.hold),
        })

    eq_df = pd.DataFrame(equity_curve)
    if not eq_df.empty:
        initial = params['initial_capital']
        eq_df['norm'] = eq_df['equity'] / initial
        peak = eq_df['equity'].expanding().max()
        eq_df['dd'] = ((peak - eq_df['equity']) / peak * 100)
    else:
        eq_df = pd.DataFrame()

    final_equity = eq_df['equity'].iloc[-1] if not eq_df.empty else params['initial_capital']
    total_ret = (final_equity / params['initial_capital'] - 1) * 100
    max_dd = float(eq_df['dd'].min()) if not eq_df.empty else 0

    returns = []
    if not eq_df.empty:
        eq_vals = eq_df['equity'].values
        for i in range(1, len(eq_vals)):
            if eq_vals[i-1] > 0:
                returns.append(eq_vals[i] / eq_vals[i-1] - 1)
    sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252) if returns and np.std(returns) > 0 else 0
    calmar = total_ret / max_dd if max_dd > 0 else 0
    neg = [r for r in returns if r < 0]
    sortino = np.mean(returns) / np.std(neg) * np.sqrt(252) if neg and np.std(neg) > 0 else 0
    years = len(td) / 252 if td else 1
    ann_ret = ((1 + total_ret/100) ** (1/years) - 1) * 100 if years > 0 else 0

    monthly = {}
    for _, r in eq_df.iterrows():
        m = r['date'][:7]
        monthly.setdefault(m, []).append(r['equity'])
    pos_months = sum(1 for m, vals in monthly.items() if vals[-1] >= vals[0])

    pos_counts = [e['pos'] for e in equity_curve]
    max_pos_held = max(pos_counts) if pos_counts else 0
    avg_pos_held = round(float(np.mean(pos_counts)), 1) if pos_counts else 0

    rc = Counter(t.reason for t in trades_all)
    trading_days_span = (end - start).days

    equity_json = [
        {'date': r['date'], 'equity': r['equity'], 'norm': round(float(r['norm']), 4),
         'cash': r['cash'], 'pos': r['pos'], 'dd': round(float(r.get('dd', 0)), 2) if 'dd' in r else None}
        for _, r in eq_df.iterrows()
    ] if not eq_df.empty else []

    summary = {
        'total_return': round(total_ret, 2), 'max_drawdown': round(max_dd, 2),
        'win_rate': round(wr, 1), 'initial_capital': params['initial_capital'],
        'final_equity': round(float(final_equity), 0), 'trading_days': len(td),
        'total_calendar_days': trading_days_span, 'start_date': str(start), 'end_date': str(end),
        'sharpe': round(sharpe, 2), 'calmar': round(calmar, 2), 'sortino': round(sortino, 2),
        'profit_ratio': round(float(pf), 2), 'ann_return': round(float(ann_ret), 2),
        'signals': len(signals_df), 'buy_signals': total_buy_signals,
        'sell_signals': n, 'trades': n, 'wins': nw, 'losses': nl,
        'profit_factor': round(float(pf), 2), 'best_trade': round(float(best_trade.ret), 2) if best_trade else 0,
        'worst_trade': round(float(worst_trade.ret), 2) if worst_trade else 0,
        'avg_win': round(float(aw), 2), 'avg_loss': round(float(al), 2),
        'avg_hold_win': round(float(avg_hold_win), 1), 'avg_hold_loss': round(float(avg_hold_loss), 1),
        'positive_months': f"{pos_months}/{len(monthly)}" if monthly else "0/0",
        'max_positions_held': max_pos_held, 'avg_positions_held': avg_pos_held,
        'exit_reasons': dict(rc.most_common()), 'data_source': f"py_{period}",
    }

    return {
        'status': 'ok', 'summary': summary,
        'equity': equity_json, 'trades': trades_json,
        'daily_trades': {}, 'indices': indices,
        'params': {k: v for k, v in params.items() if not callable(v) and k != 'signal_params'},
    }


def _execute_signal(pos, code, d, sig, cash, trades_all, sell_reasons, cooldown):
    """执行一个卖出信号，返回 (new_cash,) 或 None"""
    import inspect
    sell_shares = pos.shares
    if sig.sell_ratio < 1.0:
        ss = int(pos.shares * sig.sell_ratio / 100) * 100
        if ss > pos.remaining: ss = int(pos.remaining // 100 * 100)
        if ss < 100: ss = min(100, int(pos.remaining))
        sell_shares = min(ss, pos.shares)
    if sell_shares <= 0:
        sell_shares = pos.shares
    # 任务一: 卖出扣成本(佣金+印花+滑点)。pos.cost 含买入成本，按卖出股数摊分成本基。
    _sr = calc_sell_revenue(sig.sell_price, sell_shares)
    sell_revenue = _sr['total']
    cost_basis = pos.cost * (sell_shares / pos.shares) if pos.shares else 0.0
    profit = sell_revenue - cost_basis
    ret = (profit / cost_basis * 100) if cost_basis else 0.0
    cash += sell_revenue
    pos.remaining -= sell_shares
    if pos.remaining <= 0:
        pos.active = False
        pos.remaining = 0
    trades_all.append(Trade(code, pos.entry_date, d,
                            pos.entry_price, sig.sell_price, sell_shares,
                            round(ret, 2), round(profit, 0), sig.reason,
                            (d - pos.entry_date).days))
    sell_reasons[sig.reason] += 1
    cooldown[code] = d
    return (cash,)


def run_backtest(params: dict, progress_cb: Optional[Callable] = None,
                 stop_event=None, stock_names: Optional[dict] = None,
                 stock_pool: Optional[list] = None) -> dict:
    """
    执行回测，返回完整结果字典
    params 包含所有回测参数，支持 intraday_freq 选择精度
    """
    start = params.get('start_date', date(2023, 1, 1))
    end = params.get('end_date', date.today())
    if isinstance(start, str): start = date.fromisoformat(start)
    if isinstance(end, str): end = date.fromisoformat(end)

    if progress_cb: progress_cb(0, 4, "加载日线数据...")

    buffer_start = start - timedelta(days=180)
    bars = load_daily_bars(buffer_start, end)
    if stop_event and stop_event.is_set(): return {'status': 'stopped'}

    if stock_pool:
        bars = bars[bars['code'].isin(stock_pool)].copy()

    if progress_cb: progress_cb(1, 4, "生成交易信号...")

    strategy_name = params.get('strategy_name', STRATEGY_NAME)
    user_signal_params = params.get('signal_params', {})
    strategy_files = {
        '盘整突破': 'panzheng_tupo',
        'MA5角度_原版': 'ma5_angle',
        'MA5角度_TDXv2': 'ma5_angle_tdx_v2',
        'MA5金叉': 'ma5_angle_cross',
    }
    fname = strategy_files.get(strategy_name, strategy_name)
    mod = __import__(f'app.screener.strategies.{fname}', fromlist=['generate_signals', 'PARAMS'])
    fn = getattr(mod, 'generate_signals', None)
    if fn is None:
        raise ImportError(f'策略 {strategy_name} ({fname}) 缺少 generate_signals 函数')
    import inspect as _inspect
    sig_param_names = set(_inspect.signature(fn).parameters.keys()) - {'df', 'bars'}
    default_params = getattr(mod, 'PARAMS', {})
    merged = {k: v for k, v in default_params.items() if k in sig_param_names}
    variants = getattr(mod, 'STRATEGY_VARIANTS', {})
    if strategy_name in variants:
        merged.update({k: v for k, v in variants[strategy_name].items() if k in sig_param_names})
    merged.update({k: v for k, v in user_signal_params.items() if k in sig_param_names})
    sig = fn(bars, **merged)
    sig = sig[(sig['date'] >= start) & (sig['date'] <= end)].copy()
    sig['date'] = pd.to_datetime(sig['date']).dt.date

    # 精度选择
    period = params.get('intraday_freq', 'daily')
    if period in ('5m', '1m'):
        if progress_cb: progress_cb(2, 4, f"日内逐Bar回放 ({period})...")
        result = _run_intraday_backtest(
            bars, sig, start, end, params, progress_cb, stop_event, stock_names, period)
        if progress_cb: progress_cb(5, 5, "完成")
        return result

    # 日线回测（原有逻辑）
    bt = bars[(bars['date'] >= start) & (bars['date'] <= end)]
    closes, highs, lows, opens, atrs = {}, {}, {}, {}, {}
    use_atr = params.get('use_atr_trail', False)
    if use_atr and 'atr14' not in bt.columns:
        def _compute_atr(grp):
            h, l, c = grp['high'], grp['low'], grp['close'].shift(1)
            tr = pd.concat([h-l, (h-c).abs(), (l-c).abs()], axis=1).max(axis=1)
            return tr.rolling(14).mean()
        bt = bt.sort_values(['code', 'date'])
        bt['atr14'] = bt.groupby('code', group_keys=False).apply(_compute_atr).reset_index(level=0, drop=True)
    for d, g in bt.groupby('date'):
        closes[d] = dict(zip(g['code'], g['close']))
        highs[d] = dict(zip(g['code'], g['high']))
        lows[d] = dict(zip(g['code'], g['low']))
        opens[d] = dict(zip(g['code'], g['open']))
        if use_atr and 'atr14' in g.columns:
            atrs[d] = dict(zip(g['code'], g['atr14']))
    td_list = sorted(closes.keys())
    sbd = defaultdict(list)
    for _, r in sig.iterrows():
        sbd[r['date']].append((r['code'], float(r['close'])))

    if stop_event and stop_event.is_set(): return {'status': 'stopped'}
    if progress_cb: progress_cb(2, 4, f"逐日回测 ({len(td_list)}个交易日)...")

    eng = FastEngine(td_list, params)
    cooldown = params.get('same_stock_cooldown', 20)
    daily_trades = {}
    total_buy_signals = 0
    prev_snap = {}
    max_positions_held = 0
    pos_counts = []

    for i, d in enumerate(td_list):
        if stop_event and stop_event.is_set(): return {'status': 'stopped'}
        day_info = {'bought': [], 'sold': []}
        prev_trade_count = len(eng.trades)
        snap = {}
        for code in eng.positions:
            if d in closes and code in closes[d]:
                snap[code] = {
                    'open': opens[d].get(code, closes[d].get(code, 0)),
                    'high': highs[d].get(code, closes[d].get(code, 0)),
                    'low': lows[d].get(code, closes[d].get(code, 0)),
                    'close': closes[d].get(code, 0),
                    'atr': atrs.get(d, {}).get(code, 0) if atrs else 0,
                }
        eng.sell_phase(d, snap, prev_snap)
        prev_snap = {k: dict(v) for k, v in snap.items()}
        new_trades = eng.trades[prev_trade_count:]
        for t in new_trades:
            day_info['sold'].append({
                'code': t.code, 'name': stock_names.get(t.code, '') if stock_names else '',
                'price': round(float(t.exit_px), 2), 'shares': int(t.shares),
                'reason': t.reason, 'ret': round(float(t.ret), 2),
            })
        paused = eng.pause is not None and d <= eng.pause
        if d in sbd and not paused:
            for code, px in sbd[d]:
                if eng.cash < min(eng.max_pos(), params.get('min_buy_amt', 5000)):
                    break
                if any(t.code == code and (d - t.entry_date).days <= cooldown for t in eng.trades):
                    continue
                if eng.buy(d, code, px):
                    day_info['bought'].append({
                        'code': code, 'name': stock_names.get(code, '') if stock_names else '',
                        'price': round(float(px), 2),
                    })
                    if code not in prev_snap and d in closes and code in closes[d]:
                        prev_snap[code] = {'close': closes[d][code]}
        n_pos = eng.pos_n()
        pos_counts.append(n_pos)
        if n_pos > max_positions_held:
            max_positions_held = n_pos
        eng.record(d, snap)
        if day_info['bought'] or day_info['sold']:
            daily_trades[str(d)] = day_info

    if stop_event and stop_event.is_set(): return {'status': 'stopped'}
    if progress_cb: progress_cb(3, 4, "汇总结果...")

    eq = pd.DataFrame(eng.equity)
    if eq.empty or not eng.trades:
        return {'status': 'ok', 'trades': [], 'equity': [], 'summary': {}}
    fe = eq['equity'].iloc[-1]
    total_ret = (fe / params['initial_capital'] - 1) * 100
    eq['cmax'] = eq['equity'].cummax()
    eq['dd'] = (eq['equity'] - eq['cmax']) / eq['cmax'] * 100
    max_dd = float(eq['dd'].min())
    n = len(eng.trades)
    wins = [t for t in eng.trades if t.ret > 0]
    loses = [t for t in eng.trades if t.ret <= 0]
    nw, nl = len(wins), len(loses)
    wr = nw / n * 100 if n > 0 else 0
    aw = np.mean([t.ret for t in wins]) if wins else 0
    al = np.mean([t.ret for t in loses]) if loses else 0
    profit_wins = [t.profit for t in wins]
    profit_loses = [t.profit for t in loses]
    total_wins_profit = sum(profit_wins) if profit_wins else 0
    total_loses_loss = abs(sum(profit_loses)) if profit_loses else 1
    pf = total_wins_profit / total_loses_loss if total_loses_loss != 0 else 0
    best_trade = max(eng.trades, key=lambda t: t.ret) if eng.trades else None
    worst_trade = min(eng.trades, key=lambda t: t.ret) if eng.trades else None
    avg_hold_win = np.mean([t.hold for t in wins]) if wins else 0
    avg_hold_loss = np.mean([t.hold for t in loses]) if loses else 0
    profit_ratio = aw / abs(al) if al != 0 else 0
    initial = params['initial_capital']
    eq['norm'] = eq['equity'] / initial
    eq['daily_ret'] = eq['equity'].pct_change()
    daily_rets = eq['daily_ret'].dropna()
    rf_daily = 0.02 / 252
    excess = daily_rets - rf_daily
    sharpe = float(np.sqrt(252) * excess.mean() / excess.std()) if excess.std() > 0 else 0
    trading_days_span = (td_list[-1] - td_list[0]).days
    ann_ret = (1 + total_ret/100) ** (365/max(trading_days_span, 1)) - 1
    calmar = ann_ret / abs(max_dd/100) if max_dd != 0 else 0
    downside = daily_rets[daily_rets < 0]
    downside_std = downside.std() if len(downside) > 0 else 0
    sortino = float(np.sqrt(252) * excess.mean() / downside_std) if downside_std > 0 else 0
    rc = Counter(t.reason for t in eng.trades)
    eq['month'] = eq['date'].apply(lambda d: d[:7])
    monthly = eq.groupby('month').agg(s=('equity', 'first'), e=('equity', 'last'))
    monthly['ret'] = (monthly['e'] / monthly['s'] - 1) * 100
    pos_months = int((monthly['ret'] > 0).sum())
    trades_json = []
    for t in eng.trades:
        trades_json.append({
            'code': t.code, 'name': stock_names.get(t.code, '') if stock_names else '',
            'entry_date': str(t.entry_date), 'entry_time': getattr(t, 'entry_time', '09:30'),
            'exit_date': str(t.exit_date), 'exit_time': getattr(t, 'exit_time', '15:00'),
            'entry_px': round(float(t.entry_px), 2), 'exit_px': round(float(t.exit_px), 2),
            'shares': int(t.shares), 'ret_pct': round(float(t.ret), 2),
            'profit': round(float(t.profit), 0), 'entry_total': round(float(t.shares * t.entry_px), 0),
            'exit_total': round(float(t.shares * t.exit_px), 0), 'reason': t.reason,
            'hold_days': int(t.hold),
        })
    equity_json = [
        {'date': r['date'], 'equity': r['equity'], 'norm': round(r['norm'], 4),
         'cash': r['cash'], 'pos': r['pos'], 'dd': round(float(r.get('dd', 0)), 2) if 'dd' in r else None}
        for _, r in eq.iterrows()
    ]
    for i, item in enumerate(equity_json):
        item['dd'] = round(float(eq['dd'].iloc[i]), 2)
    if progress_cb: progress_cb(4, 4, "加载指数对比数据...")
    indices = load_index_data(start_date=start)
    summary = {
        'total_return': round(total_ret, 2), 'max_drawdown': round(max_dd, 2),
        'win_rate': round(wr, 1), 'initial_capital': params['initial_capital'],
        'final_equity': round(float(fe), 0), 'trading_days': len(td_list),
        'total_calendar_days': trading_days_span, 'start_date': str(start), 'end_date': str(end),
        'sharpe': round(sharpe, 2), 'calmar': round(calmar, 2), 'sortino': round(sortino, 2),
        'profit_ratio': round(float(profit_ratio), 2), 'ann_return': round(float(ann_ret * 100), 2),
        'signals': len(sig), 'buy_signals': sum(1 for _ in eng.trades if _.ret != 0 or _.profit != 0),
        'sell_signals': n, 'trades': n, 'wins': nw, 'losses': nl,
        'profit_factor': round(float(pf), 2), 'best_trade': round(float(best_trade.ret), 2) if best_trade else 0,
        'worst_trade': round(float(worst_trade.ret), 2) if worst_trade else 0,
        'avg_win': round(float(aw), 2), 'avg_loss': round(float(al), 2),
        'avg_hold_win': round(float(avg_hold_win), 1), 'avg_hold_loss': round(float(avg_hold_loss), 1),
        'positive_months': f"{pos_months}/{len(monthly)}", 'max_positions_held': max_positions_held,
        'avg_positions_held': round(float(np.mean(pos_counts)), 1) if pos_counts else 0,
        'exit_reasons': dict(rc.most_common()),
    }
    if progress_cb: progress_cb(5, 5, "完成")
    return {
        'status': 'ok', 'summary': summary, 'equity': equity_json,
        'trades': trades_json, 'daily_trades': daily_trades,
        'indices': indices,
        'params': {k: v for k, v in params.items() if not callable(v) and k != 'signal_params'},
    }
