"""
轻量回测引擎 — 日线收盘价，纯 parquet 数据源，无 DuckDB 依赖
直接复刻 scripts/ 中已验证的回测逻辑
"""
import pandas as pd
import numpy as np
from datetime import date, timedelta
from collections import defaultdict, Counter
from pathlib import Path
from typing import Callable, Optional
import json

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
    __slots__ = ('code','entry_date','exit_date','entry_px','exit_px',
                 'shares','ret','profit','reason','hold')
    def __init__(self, c, ed, xd, ep, xp, sh, ret, profit, reason, hold):
        self.code=c; self.entry_date=ed; self.exit_date=xd
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
        # 向后兼容：旧的 tp1/tp2 参数自动转为 take_profit_tiers
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
        ma = min(self.max_pos(), self.cash)
        if ma < self.min_buy: return None
        sh = int(ma / px / 100) * 100
        if sh < 100: return None
        cost = sh * px
        if cost > self.cash: return None
        p = Position(code, d, px, sh, cost, STRATEGY_NAME)
        self.cash -= cost
        self.positions[code] = p
        return p

    def check_stops(self, d, snap, prev_snap=None):
        sells = []
        hs = self.p['hard_stop']
        tp_tiers = self.p.get('take_profit_tiers', [])
        trail_act = self.p['trail_activate']
        trail_dd = self.p['trail_dd']
        time_exit = self.p['time_exit_days']
        time_exit_profit = self.p['time_exit_profit']
        time_force = self.p['time_force_days']
        cooldown = self.p.get('same_stock_cooldown', 20)

        for code, p in list(self.positions.items()):
            if not p.active or p.remaining <= 0: continue
            bar = snap.get(code)
            if bar is None: continue
            cp = bar['close']; hp = bar.get('high', cp)
            if hp > p.peak_price: p.peak_price = hp
            pp = p.peak_price / p.entry_price - 1
            cur = cp / p.entry_price - 1
            hd = self._td(p.entry_date, d)

            # 除权跳空保护
            if prev_snap:
                prev_bar = prev_snap.get(code)
                if prev_bar and prev_bar.get('close', 0) > 0:
                    overnight_gap = cp / prev_bar['close'] - 1
                    prefix = code[:3] if len(code) >= 3 else code
                    if prefix in ('300', '301', '688'): gap_limit = -0.20
                    elif prefix[0] == '8': gap_limit = -0.30
                    else: gap_limit = -0.10
                    if overnight_gap <= gap_limit:
                        ratio = cp / prev_bar['close']
                        p.entry_price *= ratio
                        p.peak_price *= ratio
                        cur = cp / p.entry_price - 1

            if cur <= hs:
                sells.append((p, cp, "HS", None)); continue
            if hd > time_force:
                sells.append((p, cp, "TF", None)); continue

            # 多档阶梯止盈：按顺序检查，每档只触发一次
            for idx, tier in enumerate(tp_tiers):
                if idx not in p.tp_triggered and cur >= tier['profit_pct']:
                    ss = int(p.remaining * tier['sell_ratio'] / 100) * 100
                    if ss >= 100:
                        p.tp_triggered.add(idx)
                        label = f"TP{idx+1}"
                        sells.append((p, cp, label, ss))
                        break  # 每轮只触发一档

            if pp >= trail_act:
                dd = cp / p.peak_price - 1
                # ATR 动态回撤: max(固定%, ATR倍数*ATR/入场价)
                eff_trail_dd = trail_dd
                if self.p.get('use_atr_trail') and bar.get('atr', 0) > 0:
                    atr_mul = self.p.get('atr_trail_multiplier', 1.0)
                    atr_pct = atr_mul * bar['atr'] / p.entry_price
                    eff_trail_dd = max(trail_dd, atr_pct)
                if dd <= -eff_trail_dd:
                    sells.append((p, cp, "TR", None)); continue
            if hd > time_exit and cur > time_exit_profit:
                sells.append((p, cp, "TC", None)); continue
        return sells

    def sell(self, p, px, reason, partial=None, xd=None):
        ss = partial if partial else p.remaining
        ss = int(ss // 100 * 100)
        if ss <= 0: return None
        ret = (px / p.entry_price - 1) * 100
        profit = ss * (px - p.entry_price)
        p.remaining -= ss
        if p.remaining <= 0: p.active = False; p.remaining = 0
        self.cash += ss * px
        return Trade(p.code, p.entry_date, xd or date.today(),
                     p.entry_price, px, ss, ret, profit, reason, 0)

    def sell_phase(self, d, snap, prev_snap=None):
        streak_pause = self.p.get('loss_streak_pause', 5)
        pause_days = self.p.get('pause_days', 3)
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
    """从 parquet 加载全市场日线"""
    files = [f for f in DAILY_DIR.glob("*.parquet")
             if not f.stem.startswith('index_') and len(f.stem) == 6 and f.stem.isdigit()]
    dfs = []
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
        df = df.loc[:, ~df.columns.duplicated()].copy()
        keep = [c for c in ['date', 'open', 'high', 'low', 'close', 'volume'] if c in df.columns]
        if 'date' not in keep or 'close' not in keep:
            continue
        df = df[keep].copy()
        df['code'] = f.stem
        df['date'] = pd.to_datetime(df['date']).dt.date
        dfs.append(df)
    bars = pd.concat(dfs, ignore_index=True)
    for c in ['open', 'high', 'low', 'close', 'volume']:
        if c in bars.columns:
            bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=['close'])
    bars = bars[(bars['date'] >= start_buffer) & (bars['date'] <= end)]
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
                # 基准：优先用 start_date 最近交易日的收盘价
                if start_date:
                    base_rows = df[df['date'] >= start_date]
                    if not base_rows.empty:
                        base = float(base_rows['close'].iloc[0])
                    else:
                        base = float(df['close'].iloc[-1])
                else:
                    base = float(df['close'].iloc[0])
                df['norm'] = df['close'] / base
                # 只保留回测起点之后的数据
                if start_date:
                    df = df[df['date'] >= start_date]
                result[name] = [
                    {'date': str(d), 'close': float(c), 'norm': round(float(n), 4)}
                    for d, c, n in zip(df['date'], df['close'], df['norm'])
                ]
        except Exception:
            pass
    return result


def run_backtest(params: dict, progress_cb: Optional[Callable] = None,
                 stop_event=None, stock_names: Optional[dict] = None) -> dict:
    """
    执行回测，返回完整结果字典
    params 包含所有回测参数
    stock_names: {code: name} 可选的股票名称映射
    """
    start = params.get('start_date', date(2023, 1, 1))
    end = params.get('end_date', date.today())
    if isinstance(start, str): start = date.fromisoformat(start)
    if isinstance(end, str): end = date.fromisoformat(end)

    if progress_cb: progress_cb(0, 4, "加载日线数据...")

    # 加载
    # buffer: 回测起点前推180天确保MA60有足够历史
    buffer_start = start - timedelta(days=180)
    bars = load_daily_bars(buffer_start, end)
    if stop_event and stop_event.is_set(): return {'status': 'stopped'}

    if progress_cb: progress_cb(1, 4, "生成交易信号...")

    # 信号
    from app.screener.strategies.ma5_angle import generate_signals
    signal_params = params.get('signal_params', {})
    sig = generate_signals(bars, **signal_params)
    sig = sig[(sig['date'] >= start) & (sig['date'] <= end)].copy()
    sig['date'] = pd.to_datetime(sig['date']).dt.date

    bt = bars[(bars['date'] >= start) & (bars['date'] <= end)]
    closes, highs, atrs = {}, {}, {}
    # 预计算 ATR(14) 映射
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
        if use_atr and 'atr14' in g.columns:
            atrs[d] = dict(zip(g['code'], g['atr14']))
    td = sorted(closes.keys())
    sbd = defaultdict(list)
    for _, r in sig.iterrows():
        sbd[r['date']].append((r['code'], float(r['close'])))

    if stop_event and stop_event.is_set(): return {'status': 'stopped'}
    if progress_cb: progress_cb(2, 4, f"逐日回测 ({len(td)}个交易日)...")

    # 引擎
    eng = FastEngine(td, params)
    cooldown = params.get('same_stock_cooldown', 20)

    daily_trades = {}  # date -> {bought: [{code, price}], sold: [{code, price, reason, ret}]}
    total_buy_signals = 0
    total_sell_signals = 0
    prev_snap = {}  # 前一日快照，用于除权跳空检测
    max_positions_held = 0  # 最大同时持仓数
    pos_counts = []  # 每日持仓数列表

    for i, d in enumerate(td):
        if stop_event and stop_event.is_set(): return {'status': 'stopped'}

        day_info = {'bought': [], 'sold': []}
        prev_trade_count = len(eng.trades)

        snap = {}
        for code in eng.positions:
            if d in closes and code in closes[d]:
                snap[code] = {
                    'open': closes[d].get(code, 0),
                    'high': highs[d].get(code, closes[d].get(code, 0)),
                    'low': closes[d].get(code, 0),
                    'close': closes[d].get(code, 0),
                    'atr': atrs.get(d, {}).get(code, 0) if atrs else 0,
                }
        eng.sell_phase(d, snap, prev_snap)
        prev_snap = {k: dict(v) for k, v in snap.items()}

        # 记录当天卖出的
        new_trades = eng.trades[prev_trade_count:]
        for t in new_trades:
            day_info['sold'].append({
                'code': t.code,
                'name': stock_names.get(t.code, '') if stock_names else '',
                'price': round(float(t.exit_px), 2),
                'shares': int(t.shares),
                'reason': t.reason, 'ret': round(float(t.ret), 2),
            })
        total_sell_signals += len(new_trades)

        paused = eng.pause is not None and d <= eng.pause
        buys_today = 0
        if d in sbd and not paused:
            for code, px in sbd[d]:
                if eng.cash < min(eng.max_pos(), params.get('min_buy_amt', 5000)):
                    break
                if any(t.code == code and (d - t.entry_date).days <= cooldown
                       for t in eng.trades):
                    continue
                if eng.buy(d, code, px):
                    day_info['bought'].append({
                        'code': code,
                        'name': stock_names.get(code, '') if stock_names else '',
                        'price': round(float(px), 2),
                    })
                    buys_today += 1
                    # 将新买入股票的当日收盘价补入 prev_snap，确保次日除权检测生效
                    if code not in prev_snap and d in closes and code in closes[d]:
                        prev_snap[code] = {'close': closes[d][code]}
        total_buy_signals += buys_today

        n_pos = eng.pos_n()
        pos_counts.append(n_pos)
        if n_pos > max_positions_held:
            max_positions_held = n_pos

        eng.record(d, snap)

        if day_info['bought'] or day_info['sold']:
            daily_trades[str(d)] = day_info

    if stop_event and stop_event.is_set(): return {'status': 'stopped'}
    if progress_cb: progress_cb(3, 4, "汇总结果...")

    # 汇总
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

    # 最佳/最差交易
    best_trade = max(eng.trades, key=lambda t: t.ret) if eng.trades else None
    worst_trade = min(eng.trades, key=lambda t: t.ret) if eng.trades else None

    # 平均持仓时长
    avg_hold_win = np.mean([t.hold for t in wins]) if wins else 0
    avg_hold_loss = np.mean([t.hold for t in loses]) if loses else 0

    # 盈亏比率
    profit_ratio = aw / abs(al) if al != 0 else 0

    # 归一化净值曲线
    initial = params['initial_capital']
    eq['norm'] = eq['equity'] / initial

    # 日收益率（用于风险指标计算）
    eq['daily_ret'] = eq['equity'].pct_change()
    daily_rets = eq['daily_ret'].dropna()

    # 夏普比率（无风险利率假设为0.02/252）
    rf_daily = 0.02 / 252
    excess = daily_rets - rf_daily
    sharpe = float(np.sqrt(252) * excess.mean() / excess.std()) if excess.std() > 0 else 0

    # 年化收益率
    trading_days_span = (td[-1] - td[0]).days
    ann_ret = (1 + total_ret/100) ** (365/max(trading_days_span, 1)) - 1

    # 卡玛比率
    calmar = ann_ret / abs(max_dd/100) if max_dd != 0 else 0

    # 索提诺比率（下行标准差）
    downside = daily_rets[daily_rets < 0]
    downside_std = downside.std() if len(downside) > 0 else 0
    sortino = float(np.sqrt(252) * excess.mean() / downside_std) if downside_std > 0 else 0

    # 退出原因
    rc = Counter(t.reason for t in eng.trades)

    # 月度统计
    eq['month'] = eq['date'].apply(lambda d: d[:7])
    monthly = eq.groupby('month').agg(s=('equity', 'first'), e=('equity', 'last'))
    monthly['ret'] = (monthly['e'] / monthly['s'] - 1) * 100
    pos_months = int((monthly['ret'] > 0).sum())

    trades_json = []
    for t in eng.trades:
        trades_json.append({
            'code': t.code,
            'name': stock_names.get(t.code, '') if stock_names else '',
            'entry_date': str(t.entry_date),
            'exit_date': str(t.exit_date),
            'entry_px': round(float(t.entry_px), 2),
            'exit_px': round(float(t.exit_px), 2),
            'shares': int(t.shares),
            'ret_pct': round(float(t.ret), 2),
            'profit': round(float(t.profit), 0),
            'entry_total': round(float(t.shares * t.entry_px), 0),
            'exit_total': round(float(t.shares * t.exit_px), 0),
            'reason': t.reason,
            'hold_days': int(t.hold),
        })

    equity_json = [
        {'date': r['date'], 'equity': r['equity'], 'norm': round(r['norm'], 4),
         'cash': r['cash'], 'pos': r['pos'], 'dd': round(float(r.get('dd', 0)), 2) if 'dd' in r else None}
        for _, r in eq.iterrows()
    ]
    # 补充回撤
    for i, item in enumerate(equity_json):
        item['dd'] = round(float(eq['dd'].iloc[i]), 2)

    # 指数数据
    if progress_cb: progress_cb(4, 4, "加载指数对比数据...")
    indices = load_index_data(start_date=start)

    summary = {
        # 核心结果
        'total_return': round(total_ret, 2),
        'max_drawdown': round(max_dd, 2),
        'win_rate': round(wr, 1),
        'initial_capital': params['initial_capital'],
        'final_equity': round(float(fe), 0),
        'trading_days': len(td),
        'total_calendar_days': trading_days_span,
        'start_date': str(start),
        'end_date': str(end),
        # 风险收益指标
        'sharpe': round(sharpe, 2),
        'calmar': round(calmar, 2),
        'sortino': round(sortino, 2),
        'profit_ratio': round(float(profit_ratio), 2),
        'ann_return': round(float(ann_ret * 100), 2),
        # 交易统计
        'signals': len(sig),
        'buy_signals': total_buy_signals,
        'sell_signals': total_sell_signals,
        'trades': n,
        'wins': nw,
        'losses': nl,
        'profit_factor': round(float(pf), 2),
        # 收益分析
        'best_trade': round(float(best_trade.ret), 2) if best_trade else 0,
        'worst_trade': round(float(worst_trade.ret), 2) if worst_trade else 0,
        'avg_win': round(float(aw), 2),
        'avg_loss': round(float(al), 2),
        'avg_hold_win': round(float(avg_hold_win), 1),
        'avg_hold_loss': round(float(avg_hold_loss), 1),
        'positive_months': f"{pos_months}/{len(monthly)}",
        'max_positions_held': max_positions_held,
        'avg_positions_held': round(float(np.mean(pos_counts)), 1) if pos_counts else 0,
        'exit_reasons': dict(rc.most_common()),
    }

    if progress_cb: progress_cb(5, 5, "完成")

    return {
        'status': 'ok',
        'summary': summary,
        'equity': equity_json,
        'trades': trades_json,
        'daily_trades': daily_trades,
        'indices': indices,
        'params': {k: v for k, v in params.items()
                   if not callable(v) and k != 'signal_params'},
    }
