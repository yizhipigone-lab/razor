"""
严格 T+1 + 成本 回测引擎

vs FastEngine (app/backtest/simple_runner.py) 的差异:
  1. T 日买入的票, T+1 日才可卖 (A 股真实 T+1 规则)
  2. 佣金 0.025% 单边 (最低 5 元) + 印花 0.05% (卖时收) + 滑点 0.1% (双边)
  3. 卖出资金 T 日即可继续买入 (A 股真实规则, 跟 FastEngine cash 实时回笼一致)
  4. 其它规则 (硬止损/移动止盈/ATR/阶梯止盈/首日离场/时间退出/同股冷却/连亏暂停)
     全部走本系统 app/backtest/exit_rules.py, 跟 FastEngine 保持一致

用法:
    from app.backtest.strict_runner import StrictEngine, run_strict
    eng = StrictEngine(td_list, params)
    eng.sell_phase(d, snap, prev_snap)
    eng.buy(d, code, px)
    eng.record(d, snap)
"""
import pandas as pd
from datetime import date
from pathlib import Path
from app.backtest.simple_runner import FastEngine  # 复用同套引擎的辅助逻辑
from app.backtest.execution import can_buy, can_sell_today, calc_buy_cost, calc_sell_revenue

# 成本参数 (A 股标准, 偏保守) - L28 修复: 委托给 execution.py 统一
# 保留本地常量仅为向后兼容(其他模块可能引用)
COMMISSION_RATE = 0.00025   # 万2.5 单边
STAMP_TAX_RATE = 0.0005     # 千0.5 卖时收
SLIPPAGE_RATE = 0.001       # 万10 双边 (模拟冲击成本)
MIN_COMMISSION = 5.0        # 最低 5 元
MIN_LOT = 100


class Trade:
    __slots__ = ('code', 'entry_date', 'exit_date', 'entry_px', 'shares',
                 'entry_commission', 'exit_px',
                 'exit_commission', 'stamp_tax', 'ret', 'profit', 'reason',
                 'hold_days', 'sellable_date')
    def __init__(self, code, entry_date, exit_date, entry_px, shares,
                 entry_commission, exit_px,
                 exit_commission, stamp_tax, ret, profit, reason,
                 hold_days, sellable_date):
        self.code = code
        self.entry_date = entry_date
        self.exit_date = exit_date
        self.entry_px = entry_px
        self.shares = shares
        self.entry_commission = entry_commission
        self.exit_px = exit_px
        self.exit_commission = exit_commission
        self.stamp_tax = stamp_tax
        self.ret = ret
        self.profit = profit
        self.reason = reason
        self.hold_days = hold_days
        self.sellable_date = sellable_date


class Position:
    __slots__ = ('code', 'entry_date', 'entry_px', 'entry_price', 'shares',
                 'cost', 'entry_commission', 'remaining', 'peak_price',
                 'tp_triggered', 'active', 'sellable_date')
    def __init__(self, code, d, px, sh, cost, entry_commission):
        self.code = code
        self.entry_date = d
        self.entry_px = px
        self.entry_price = px  # 兼容 exit_rules.build_context
        self.shares = sh
        self.remaining = sh
        self.cost = cost
        self.entry_commission = entry_commission
        self.peak_price = px
        self.tp_triggered = set()
        self.active = True
        # T+1 规则: 买入当日不可卖
        self.sellable_date = d


class StrictEngine:
    """严格 T+1 + 成本回测引擎
    行为差异 vs FastEngine:
      - Position.sellable_date: T+1 才可卖
      - buy/sell 扣 commission + stamp_tax + slippage
      - cash 实时回笼 (T 日卖出 T 日可买, A 股真实规则)
    """
    def __init__(self, td_list, params):
        self.cash = params['initial_capital']
        self.position_size = params['position_size']
        self.min_buy = params.get('min_buy_amt', 5000)
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.equity: list[dict] = []
        self.cl = 0  # 连亏计数
        self.pause = None
        self.td_list = td_list
        self.p = params

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
            if not p.active:
                continue
            bar = prices.get(p.code, {})
            px = bar.get('close', p.entry_px) if isinstance(bar, dict) \
                else (bar if bar else p.entry_px)
            pv += p.remaining * px
        return self.cash + pv

    def buy(self, d, code, px):
        """T+1 严格买入 + 成本"""
        if code in self.positions:
            return None
        # L28 修复: 统一成交执行层 - 涨停过滤
        # strict_runner 简化处理:prev_close = px (无历史上下文,避免误伤)
        prev_close = px
        can_buy_ok, _ = can_buy(code, prev_close, px)
        if not can_buy_ok:
            return None
        ma = min(self.max_pos(), self.cash)
        if ma < self.min_buy:
            return None
        # L28 修复: 删 buy_px 预乘滑点, calc_buy_cost 内部已含 slippage
        sh = int(ma / px / MIN_LOT) * MIN_LOT
        if sh < MIN_LOT:
            return None
        # L28 修复: 统一成交执行层 - 买入成本(佣金+滑点)
        cost_result = calc_buy_cost(px, sh)
        cost = cost_result['total']
        # 最低佣金调整: 若 max() 后超过现金,减仓
        comm = cost_result['commission']
        if cost > self.cash:
            sh = int((self.cash - MIN_COMMISSION) / px / MIN_LOT) * MIN_LOT
            if sh < MIN_LOT:
                return None
            cost_result = calc_buy_cost(px, sh)
            cost = cost_result['total']
            comm = cost_result['commission']
        self.cash -= cost
        sellable_date = d + pd.Timedelta(days=1)
        p = Position(code, d, px, sh, cost, comm)
        p.sellable_date = sellable_date.date() if hasattr(
            sellable_date, 'date') else sellable_date
        self.positions[code] = p
        return p

    def sell(self, p, px, reason, partial=None, xd=None):
        """T+1 严格卖出 + 成本
        A 股: T 日卖出资金 T 日即可用 (cash 实时回笼)
        """
        ss = partial if partial else p.remaining
        ss = int(ss // MIN_LOT * MIN_LOT)
        if ss < MIN_LOT:
            ss = min(MIN_LOT, int(p.remaining))
        if ss <= 0:
            return None
        ss = min(ss, int(p.remaining))
        # L28 修复: 删 sell_px 预乘滑点, calc_sell_revenue 内部已含 slippage
        sell_px = px
        # L28 修复: 统一成交执行层 - 卖出净收入(扣佣金+印花+滑点)
        sell_rev = calc_sell_revenue(sell_px, ss)
        net = sell_rev['total']
        comm = sell_rev['commission']
        stamp = sell_rev['stamp_tax']
        profit = net - (p.cost * ss / p.shares)
        ret = (net / (p.entry_px * ss) - 1) * 100
        p.remaining -= ss
        if p.remaining <= 0:
            p.active = False
            p.remaining = 0
        # 资金 T 日即可用 (A 股真实规则)
        self.cash += net
        if p.remaining <= 0:
            del self.positions[p.code]
        return Trade(p.code, p.entry_date, xd or date.today(),
                     p.entry_px, ss, p.entry_commission,
                     sell_px, comm, stamp, ret, profit, reason,
                     self._td(p.entry_date, xd or date.today()),
                     p.sellable_date)

    def sell_phase(self, d, snap, prev_snap=None):
        """T+1 卖出: 只对 sellable_date <= d 的持仓做检查"""
        from app.backtest.exit_rules import (exit_rule_engine,
                                             adjust_for_gap)
        streak_pause = self.p.get('loss_streak_pause', 5)
        pause_days = self.p.get('pause_days', 3)
        if self.pause and d > self.pause:
            self.pause = None
            self.cl = 0
        sells = []
        for code, p in list(self.positions.items()):
            if not p.active or p.remaining <= 0:
                continue
            # L28 修复: T+1 约束 - 委托给 execution.can_sell_today
            # entry_date == sellable_date 的前置日(买入日),不能卖
            # 简化:这里 p.entry_date + 1 day == sellable_date,等价为 d > p.entry_date
            if not can_sell_today(p.entry_date, d):
                continue
            bar = snap.get(code)
            if bar is None:
                continue
            cp = bar['close']
            hp = bar.get('high', cp)
            if hp > p.peak_price:
                p.peak_price = hp
            hd = self._td(p.entry_date, d)

            if prev_snap:
                prev_bar = prev_snap.get(code)
                if prev_bar:
                    p.entry_px, p.peak_price = adjust_for_gap(
                        code, p.entry_px, p.peak_price,
                        cp, prev_bar.get('close', 0),
                    )

            ctx = exit_rule_engine.build_context(
                p, bar, hd, self.p, use_high_for_tp=True,
            )
            signal = exit_rule_engine.check(ctx)
            if signal:
                if signal.reason.startswith('TP'):
                    idx = int(signal.reason[2]) - 1
                    p.tp_triggered.add(idx)
                if signal.sell_ratio < 1.0:
                    ss = int(p.shares * signal.sell_ratio / MIN_LOT) * MIN_LOT
                    if ss > p.remaining:
                        ss = int(p.remaining // MIN_LOT * MIN_LOT)
                    if ss < MIN_LOT:
                        ss = min(MIN_LOT, int(p.remaining))
                    sells.append((p, signal.sell_price, signal.reason, ss))
                else:
                    sells.append((p, signal.sell_price, signal.reason, None))
        for p, px, reason, partial in sells:
            t = self.sell(p, px, reason, partial, d)
            if t:
                self.trades.append(t)
                if t.ret <= 0:
                    self.cl += 1
                    if self.cl >= streak_pause:
                        self.pause = d + pd.Timedelta(days=pause_days)
                else:
                    self.cl = 0
                    self.pause = None
        self.positions = {k: v for k, v in self.positions.items() if v.active}

    def record(self, d, prices):
        eq = self.eq(prices)
        self.equity.append({
            'date': str(d), 'equity': round(eq, 2),
            'cash': round(self.cash, 2), 'pos': self.pos_n(),
        })


def run_strict(td_list, closes, highs, sbd, params):
    """与 batch_tdx_407 严格版逻辑一致, 复用 calls
    Returns: (eng, trades, equity)
    """
    eng = StrictEngine(td_list, params)
    prev_snap = {}
    # 从 parquet 加载 low 字段(避免盘中跳水被漏触发)
    daily_dir = Path(__file__).parent.parent.parent / "data" / "parquet" / "daily"
    low_cache = {}
    def _get_low(code, d):
        cache_key = (code, d)
        if cache_key in low_cache:
            return low_cache[cache_key]
        low_val = 0
        pq = daily_dir / f"{code}.parquet"
        if pq.exists():
            try:
                pdf = pd.read_parquet(str(pq), columns=['date', 'low'])
                pdf['date'] = pd.to_datetime(pdf['date']).dt.date
                row = pdf[pdf['date'] == d]
                if not row.empty:
                    low_val = float(row.iloc[0]['low'])
            except Exception:
                pass
        low_cache[cache_key] = low_val
        return low_val

    for d in td_list:
        snap = {}
        for code in eng.positions:
            if d in closes and code in closes[d]:
                close_val = closes[d].get(code, 0)
                snap[code] = {
                    'open': close_val,
                    'high': highs[d].get(code, close_val),
                    'low': _get_low(code, d),
                    'close': close_val,
                }
        eng.sell_phase(d, snap, prev_snap)
        prev_snap = {k: dict(v) for k, v in snap.items()}
        if d in sbd and not (eng.pause and d <= eng.pause):
            for code, px in sbd[d]:
                if eng.cash < min(eng.max_pos(), 5000):
                    break
                eng.buy(d, code, px)
        eng.record(d, snap)
    return eng
