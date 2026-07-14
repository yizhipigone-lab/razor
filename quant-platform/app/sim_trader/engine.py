"""
模拟盘交易 — 核心引擎
每日流程:
  14:52 — 止盈止损卖出（先回收现金）
  14:54 — 选股买入
  14:56 — 记录净值
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import copy
import threading
from functools import wraps
import pandas as pd
from datetime import date, datetime, timedelta
from typing import Optional, Dict, List, Tuple
from collections import Counter

from app.sim_trader.config import *
from app.sim_trader.models import Position, Trade, CycleResult  # 2026-07-14 抽出叶子模块; re-export 保向后兼容
from core.logger import get_logger

log = get_logger("SimEngine")


def _cycle_locked(method):
    """H3(2026-07-15 全项目审计): 守护 engine 状态变更方法, 防 cron 与手动 execute/reset 并发踩踏。

    用 RLock(sell_phase→execute_sell 可重入); 锁粒度=整个 cycle 方法。
    """
    @wraps(method)
    def _wrapper(self, *args, **kwargs):
        with self._cycle_lock:
            return method(self, *args, **kwargs)
    return _wrapper

# P0-4: 加载期一致性校验 — 检测到可疑(疑似回测污染)的 equity_curve 时置位，供 API 查询
_BAD_EQUITY_CURVE_DETECTED = False

_stock_name_cache = {}

def _get_stock_name(code: str) -> str:
    """查股票简称，带缓存"""
    if code in _stock_name_cache:
        return _stock_name_cache[code]
    try:
        from database.duckdb_manager import db
        df = db.conn.execute("SELECT name FROM stocks WHERE code = ?", [code]).fetchdf()
        name = str(df.iloc[0]['name']) if not df.empty else ''
    except Exception:
        name = ''
    _stock_name_cache[code] = name
    return name


def _safe_broadcast(data: dict):
    """安全广播到前端（非 server 环境静默跳过）"""
    try:
        from server.websocket.manager import sync_broadcast
        sync_broadcast(data)
    except Exception:
        pass
    # 同步写日志文件持久化
    _write_log(data)


def _write_log(entry: dict):
    """追加一行 JSON 到当日日志文件"""
    import json, os
    try:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'output', 'sim_trader', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f'{date.today()}.jsonl')
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
    except Exception:
        pass


class SimTraderEngine:
    """尾盘模拟交易引擎（含盘中实时监控）"""

    def __init__(self, store=None):
        # H3(2026-07-15): cycle 锁, 守护 sell_phase/execute_buy/execute_sell/record/refresh
        self._cycle_lock = threading.RLock()
        # L4 修复: 总是初始化 _store, 便于 refresh_trades_from_store 统一判断
        self._store = store
        if store is not None:
            state = self._store.load_state()
            self.cash = state['cash']
            self.consecutive_losses = state['consecutive_losses']
            self.pause_until = state['pause_until']
            self._trade_count = state['trade_count']
            self.positions = self._store.load_positions()
            self.trades = self._store.load_trades()
            self.equity_curve = self._store.load_equity_curve()
            self._validate_loaded_state()
        else:
            self.cash = INITIAL_CAPITAL
            self.positions: Dict[str, Position] = {}
            self.trades: List[Trade] = []
            self.equity_curve: List[Dict] = []
            self.consecutive_losses = 0
            self.pause_until: Optional[date] = None
            self._trade_count = 0

        # L3 修复: 当日新增 trades(供 API/cron 算"今日交易数")
        # 区别于 self.trades(全部历史),启动时为空,execute_sell 时 append,日切时清空
        self._today_trades: List[Trade] = []

        self._prev_snap: dict = {}  # 前一日快照，用于除权跳空保护
        # #9 修复: 昨日完整 OHLC(今日 sell_phase 14:52 时调用的"昨日"快照)
        # 与 _prev_snap 的区别:
        #   _prev_snap 在 sell_phase 末尾被覆盖为"今日 snapshot"(line 481+1)
        #   _prev_day_snap 始终是"昨日收盘快照",供次日除权跳空保护使用
        self._prev_day_snap: dict = {}
        # L5 修复: 冷启动时从 store 加载 _prev_day_snap(否则次日 sell_phase 跳空保护失效)
        if store is not None:
            self._prev_day_snap = self._store.load_prev_day_snap() or {}

        # 启动时补齐缺失的交易日净值快照（防止曲线断档）
        if store is not None:
            self._fill_missing_snapshots()

        # 盘中监控器（延迟初始化，避免循环导入）
        self._monitor = None

        # K4/C3-4: 启动时校验模拟盘风控参数与 schema 一致
        self._validate_params_against_schema()

    def _validate_params_against_schema(self):
        """校验 check_stops 使用的风控参数与 config.py schema 一致"""
        try:
            from app.config.schema import load_risk_params
            _sch = load_risk_params()
        except Exception as e:
            log.warning(f"[校验] 无法加载风控 schema: {e}，跳过一致性检查")
            return

        # 从 config.py 读取（check_stops 的 fallback 源）
        import app.sim_trader.config as _sc
        config_hard_stop = getattr(_sc, 'HARD_STOP', None)
        if config_hard_stop is not None and abs(config_hard_stop - _sch.hard_stop) > 0.01:
            log.warning(f"[校验] hard_stop 不一致: schema={_sch.hard_stop} config={config_hard_stop}")

        # 从 settings 读取（check_stops 的主源）
        from core.settings import settings as _sett
        settings_hs = _sett.get("risk", "hard_stop_loss_pct")
        if settings_hs is not None and abs(settings_hs - _sch.hard_stop) > 0.01:
            log.warning(f"[校验] hard_stop settings={settings_hs} vs schema={_sch.hard_stop} (app_setting.json={settings_hs})")

        log.info(f"[校验] 风控参数: hard_stop={_sch.hard_stop}%, TP tiers={_sch.take_profit_tiers}, "
                 f"trail_act={_sch.trail_activate}%, trail_dd={_sch.trail_dd}%")

    @_cycle_locked
    def refresh_trades_from_store(self):
        """从 store 重新加载 trades/positions/equity (L4 修复)
        用于回测/手动模式: store 仍持有完整数据, 入口 reporter 调此方法确保读到最新"""
        if self._store:
            self.trades = self._store.load_trades()
            self.positions = self._store.load_positions()
            self.equity_curve = self._store.load_equity_curve()

    def _validate_loaded_state(self):
        """P0-4: 加载期一致性校验 — 拦截疑似回测污染的 equity_curve。
        守规则3: 只丢弃可疑曲线 + 告警, 不崩溃、不阻断启动(保留 cash/positions/trades)。
        背景: 2026-06 TDX 回测数据(首日 equity=2.14倍本金)曾整盘覆盖运行态 state.json。"""
        global _BAD_EQUITY_CURVE_DETECTED
        try:
            ec = self.equity_curve
            if not ec:
                return
            # 校验1: 首条 equity 超过初始资金 1.10 倍 -> 疑似回测污染(实盘首日不可能)
            first_eq = float(ec[0].get('equity', 0) or 0)
            if first_eq > INITIAL_CAPITAL * 1.10:
                log.error(
                    f"⚠️ 拒绝采用可疑 equity_curve: 首条 equity={first_eq:,.0f} "
                    f"超过 INITIAL_CAPITAL({INITIAL_CAPITAL:,}) 的 1.10 倍, "
                    f"疑似被 TDX 回测数据污染。已丢弃曲线, 保留 cash/positions/trades。")
                self.equity_curve = []
                _BAD_EQUITY_CURVE_DETECTED = True
                return
            # 校验2: state.cash 与最近曲线 cash 偏差 >5% -> 仅告警(不丢弃)
            last_cash = float(ec[-1].get('cash', 0) or 0)
            if last_cash > 0 and self.cash > 0:
                dev = abs(self.cash - last_cash) / self.cash
                if dev > 0.05:
                    log.warning(
                        f"⚠️ state.cash({self.cash:,.0f}) 与最近 equity_curve.cash"
                        f"({last_cash:,.0f}) 偏差 {dev:.1%} (>5%), 请人工核对数据来源。")
        except Exception as e:
            log.warning(f"[加载校验] 跳过(异常不阻断启动): {e}")

    def _fill_missing_snapshots(self):
        """补齐 equity_curve 中缺失的交易日快照。
        从最后一个快照日到昨天，逐日查 parquet 收盘价计算净值。"""
        if not self.equity_curve:
            return
        try:
            from datetime import date as _d, timedelta
            last_date = _d.fromisoformat(str(self.equity_curve[-1]['date']))
            yesterday = _d.today() - timedelta(days=1)

            # 获取交易日列表
            trading_dates = set()
            try:
                from app.api.sim_trader import _load_trading_calendar
                trading_dates = _load_trading_calendar()
            except Exception:
                pass

            # 逐日检查到昨天
            d = last_date + timedelta(days=1)
            while d <= yesterday:
                if d.weekday() >= 5:  # 跳过周末
                    d += timedelta(days=1)
                    continue
                if trading_dates and d not in trading_dates:
                    d += timedelta(days=1)
                    continue

                # 查 parquet 收盘价，计算持仓市值
                total_mv = 0.0
                daily_dir = None
                try:
                    from pathlib import Path as _Path
                    import pandas as pd
                    root = _Path(__file__).resolve().parent.parent.parent
                    daily_dir = root / "data" / "parquet" / "daily"
                except Exception:
                    pass

                for code, pos in self.positions.items():
                    if not pos.is_active or pos.remaining_shares <= 0:
                        continue
                    close_px = pos.entry_price  # 兜底用买入价
                    if daily_dir:
                        f = daily_dir / f"{code}.parquet"
                        if f.exists():
                            try:
                                df = pd.read_parquet(str(f), columns=['date', 'close'])
                                df['date'] = pd.to_datetime(df['date']).dt.date
                                row = df[df['date'] == d]
                                if not row.empty:
                                    close_px = float(row.iloc[0]['close'])
                            except Exception:
                                pass
                    total_mv += pos.remaining_shares * close_px

                equity = self.cash + total_mv
                # P1-2: 单路径落盘(去重+统一'pos'键), source 标记为 fill_missing 便于追溯
                if self._store:
                    self._store.save_equity_point(d, equity, self.cash,
                                                  self.position_count, source='fill_missing')
                    self.equity_curve = self._store.load_equity_curve()
                else:
                    self.equity_curve.append({
                        'date': str(d), 'equity': equity,
                        'cash': self.cash, 'pos': self.position_count,
                    })
                log.info(f"[快照补录] {d} 净值={equity:,.0f}（启动时自动补齐）")
                d += timedelta(days=1)
        except Exception as e:
            log.warning(f"[快照补录] 跳过: {e}")

    @property
    def monitor(self):
        if self._monitor is None:
            try:
                from app.sim_trader.intraday_monitor import IntradayMonitor
                self._monitor = IntradayMonitor(self)
            except ImportError:
                return None
        return self._monitor

    @property
    def monitor_enabled(self) -> bool:
        if self.monitor is None:
            return False
        return getattr(self.monitor, 'enabled', False)

    @property
    def auto_sell(self) -> bool:
        return AUTO_SELL

    @property
    def auto_buy(self) -> bool:
        return AUTO_BUY

    @property
    def auto_scan(self) -> bool:
        return AUTO_SCAN

    # ── 仓位管理 ──────────────────────────────

    def max_buy_amount(self) -> float:
        if self.consecutive_losses >= LOSS_STREAK_HALVE:
            return POSITION_SIZE / 2
        return POSITION_SIZE

    def active_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.is_active]

    @property
    def position_count(self) -> int:
        return len(self.active_positions())

    def build_live_snapshot(self) -> dict:
        """用 QMT 实时行情为当前持仓构建 snapshot(全局规则: 行情优先用QMT)。
        返回 {code: {'close': 现价, 'preClose': 昨收, ...}}。
        供盘中监控器 record 用, 替代"昨日快照", 避免净值失真。
        QMT 不可用时返回空 dict, 调用方(record/total_equity)会用 current_price 兜底。"""
        codes = [p.code for p in self.active_positions()]
        if not codes:
            return {}
        try:
            from app.data_manager.quote_source import get_realtime_quotes
            qdf = get_realtime_quotes(codes)
        except Exception as e:
            log.debug(f"[实时快照] 行情获取失败: {e}")
            return {}
        if qdf.empty:
            return {}
        snap = {}
        for code in codes:
            # 兼容带/不带后缀:先精确,再按裸 code
            rows = qdf[qdf['code'] == code]
            if rows.empty and '.' in code:
                rows = qdf[qdf['code'] == code.split('.')[0]]
            if rows.empty:
                continue
            row = rows.iloc[0]
            price = float(row.get('price', 0) or 0)
            if price <= 0:
                continue
            _lc = row.get('last_close', 0)
            pre = float(_lc) if (_lc is not None and _lc == _lc and _lc > 0) else 0.0  # NaN/缺失→0(守 Q6,不用现价冒充)
            snap[code] = {
                'open': float(row.get('open', price) or price),
                'high': float(row.get('high', price) or price),
                'low': float(row.get('low', price) or price),
                'close': price,
                'preClose': pre,
            }
        return snap

    def total_equity(self, snapshot: dict) -> float:
        """snapshot: {code: {'close': float, ...}}
        净值虚高修复: 估值优先级 snapshot当前价 > pos.current_price(上次已知市价) > entry_price(兜底)。
        避免 snapshot 缺某持仓股价格时直接 fallback 买入价, 导致净值失真(2026-05盘前手动触发record虚高根因)。"""
        pv = 0.0
        for p in self.active_positions():
            px = snapshot.get(p.code, {}).get('close', 0) or 0
            if px <= 0:
                px = p.current_price if p.current_price and p.current_price > 0 else p.entry_price
            pv += p.remaining_shares * px
        return self.cash + pv

    def equity_price_coverage(self, snapshot: dict) -> tuple:
        """返回 (有效报价持仓数, 活跃持仓总数)。用于判断净值可信度。
        有效报价 = snapshot 里有该股 close>0。覆盖不全说明行情缺失, 净值可能失真。"""
        active = self.active_positions()
        covered = sum(1 for p in active
                      if (snapshot.get(p.code, {}).get('close', 0) or 0) > 0)
        return covered, len(active)

    # ── 买入 ──────────────────────────────────

    @_cycle_locked
    def execute_buy(self, today: date, code: str, price: float,
                    strategy_name: str = "") -> Optional[Position]:
        """买入一只股票，收盘价成交"""
        if code in self.positions:
            return None
        max_amt = min(self.max_buy_amount(), self.cash)
        # 全局 min/max 卡边界(与实盘同源,系统设置 trading 段;模拟盘同进程即时生效)
        from core.settings import settings
        g_min = float(settings.get("trading", "min_buy_amount", default=5000))
        g_max = float(settings.get("trading", "max_buy_amount", default=60000))
        max_amt = max(g_min, min(max_amt, g_max))
        if max_amt < MIN_BUY_AMT:
            return None
        shares = int(max_amt / price / 100) * 100
        if shares < 100:
            return None
        # 任务一: 模拟盘买入扣成本(佣金+滑点)，与回测引擎口径一致
        from app.backtest.execution import calc_buy_cost
        cost = calc_buy_cost(price, shares)['total']
        if cost > self.cash:
            return None

        pos = Position(code=code, entry_date=today, entry_price=price,
                       shares=shares, cost=cost, strategy_name=strategy_name,
                       entry_time=datetime.now().strftime('%H:%M:%S'))
        self.cash -= cost
        self.positions[code] = pos
        log.info(f"[买入] {code} 价格={price:.2f} 数量={shares} 金额={cost:.0f} 剩余现金={self.cash:.0f} 策略={strategy_name}")
        _safe_broadcast({"type":"sim_trader_log","action":"buy","code":code,"name":_get_stock_name(code),"price":round(price,2),"shares":shares,"cost":round(cost,0),"cash":round(self.cash,0),"strategy":strategy_name,"date":str(today),"time":datetime.now().strftime('%H:%M:%S')})
        if self._store:
            self._store.save_positions(self.positions)
            self._store.save_state(
                self.cash, self.consecutive_losses,
                self.pause_until, self._trade_count)
        return pos

    # ── 卖出（止盈止损） ──────────────────────

    def check_stops(self, today: date, snapshot: dict,
                    trading_dates: List[date],
                    prev_snap: dict = None,
                    readonly: bool = False) -> List[Tuple]:
        """
        按优先级检查所有持仓的止盈止损。
        readonly=True 时不修改持仓状态（用于告警模式）。
        返回: [(pos, exit_price, reason, partial_shares_or_None), ...]
        """
        from app.backtest.exit_rules import exit_rule_engine

        sells = []
        for code, pos in list(self.positions.items()):
            if not pos.is_active or pos.remaining_shares <= 0:
                continue

            bar = snapshot.get(code)
            if bar is None:
                continue

            close_p = bar['close']
            high_p  = bar.get('high', close_p)
            hold_days = sum(1 for td in trading_dates if pos.entry_date <= td <= today)

            # 0. 除权跳空保护（readonly 模式跳过）
            if not readonly and prev_snap:
                prev_bar = prev_snap.get(code)
                if prev_bar:
                    from app.backtest.exit_rules import adjust_for_gap
                    pos.entry_price, pos.peak_price = adjust_for_gap(
                        code, pos.entry_price, pos.peak_price,
                        close_p, prev_bar.get('close', 0)
                    )

            # 更新峰值
            if not readonly and high_p > pos.peak_price:
                pos.peak_price = high_p

            # 构建参数 — 优先从 settings（app_setting.json）读取，config.py 兜底
            # 与 intraday_monitor._check_position 保持完全一致的取值逻辑
            # v5.5 (2026-07-14): 统一走 risk_params.load_risk_params, 与 intraday_monitor/exit_monitor 共享, 杜绝 4 套默认值漂移
            from app.config.risk_params import load_risk_params as _load_risk_params
            import dataclasses
            sim_params = dataclasses.asdict(_load_risk_params())
            ctx = exit_rule_engine.build_context(pos, bar, hold_days, sim_params, use_high_for_tp=True)
            signal = exit_rule_engine.check(ctx)

            if signal:
                # 标记TP档位
                if signal.reason.startswith('TP') and not readonly:
                    idx = int(signal.reason[2]) - 1
                    pos.mark_tier_triggered(idx)

                if signal.sell_ratio < 1.0:
                    ss = int(pos.remaining_shares * signal.sell_ratio / 100) * 100
                    if ss < 100:
                        ss = min(100, int(pos.remaining_shares))
                    sells.append((pos, signal.sell_price, signal.reason, ss))
                else:
                    sells.append((pos, signal.sell_price, signal.reason, None))

        return sells

    @_cycle_locked
    def execute_sell(self, pos: Position, exit_price: float, reason: str,
                     partial: Optional[int] = None,
                     exit_date: Optional[date] = None,
                     exit_timing: str = "close") -> Optional[Trade]:
        ss = partial if partial is not None else pos.remaining_shares
        ss = int(ss // 100 * 100)
        if ss <= 0:
            return None

        # 任务一: 卖出扣成本(佣金+印花+滑点)，profit/ret 基于含费净额
        from app.backtest.execution import calc_sell_revenue
        sell_net = calc_sell_revenue(exit_price, ss)['total']
        # H2(2026-07-15 全项目审计): 成本基按【剩余股数】(本次卖出前)摊分, 不是原始 shares。
        # 旧版用 pos.shares(原始, 永不改)做分母, 但 pos.cost 已被前次部分卖摊减 →
        # 第二次起 cost_basis 系统性低估、利润虚高。改为 remaining_shares(本次卖出前值)。
        cost_basis = pos.cost * (ss / pos.remaining_shares) if pos.remaining_shares else 0.0
        profit = sell_net - cost_basis
        rp = (profit / cost_basis * 100) if cost_basis else 0.0
        pos.remaining_shares -= ss
        if pos.remaining_shares > 0:
            pos.cost -= cost_basis  # 摊减已卖成本基，保证后续部分卖出口径正确

        if pos.remaining_shares <= 0:
            pos.is_active = False
            pos.remaining_shares = 0

        self.cash += sell_net
        self._trade_count += 1

        log.info(f"[卖出] {pos.code} 价格={exit_price:.2f} 数量={ss} 收益={rp:.1f}% 利润={profit:.0f} 原因={reason} 剩余现金={self.cash:.0f}")
        _safe_broadcast({"type":"sim_trader_log","action":"sell","code":pos.code,"name":_get_stock_name(pos.code),"price":round(exit_price,2),"shares":ss,"ret_pct":round(rp,1),"profit":round(profit,0),"reason":reason,"cash":round(self.cash,0),"date":str(exit_date or date.today()),"time":datetime.now().strftime('%H:%M:%S')})

        # 连亏计数（盘中/尾盘卖出统一在此更新）
        if rp <= 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
            self.pause_until = None
        if self.consecutive_losses >= LOSS_STREAK_PAUSE:
            self.pause_until = (exit_date or date.today()) + timedelta(days=PAUSE_DAYS)

        # 立即持久化（盘中/尾盘卖出统一保存，防止状态丢失）
        trade = Trade(
            code=pos.code, entry_date=pos.entry_date,
            exit_date=exit_date or date.today(),
            entry_price=pos.entry_price, exit_price=exit_price,
            shares=ss, return_pct=rp, profit_amount=profit,
            exit_reason=reason, hold_days=0,
            entry_reason=pos.strategy_name, exit_timing=exit_timing,
            entry_time=getattr(pos, 'entry_time', '15:00'),
            exit_time=datetime.now().strftime('%H:%M:%S'),
        )

        if self._store:
            self._store.save_trade(trade)
            self._store.save_positions(self.positions)
            self._store.save_state(
                self.cash, self.consecutive_losses,
                self.pause_until, self._trade_count)

        # L3 修复: 维护当日 trades 列表(供 API/cron 算"今日交易数")
        # 无论有无 store 都维护(纯回测时 store=None 也需要)
        self._today_trades.append(trade)

        return trade

    @_cycle_locked
    def sell_phase(self, today: date, snapshot: dict,
                   trading_dates: List[date]):
        """卖出阶段（先卖后买，回收现金）。仅交易时段内执行。"""
        from datetime import datetime
        now = datetime.now()
        t = now.hour * 100 + now.minute
        if t < 925 or t > 1505:
            log.warning(f"[卖出阶段] 非交易时段({now.strftime('%H:%M')})，跳过执行")
            return

        sells = self.check_stops(today, snapshot, trading_dates,
                                 prev_snap=self._prev_day_snap)  # #9 修复: 传"昨日"快照

        for pos, exit_price, reason, partial in sells:
            trade = self.execute_sell(pos, exit_price, reason, partial,
                                      exit_date=today, exit_timing="close")
            if trade:
                trade.hold_days = sum(1 for td in trading_dates
                                      if pos.entry_date <= td <= today)
                # 不再 append:execute_sell 已写 DB;冷启动时 load_trades 一次性加载
                # 这样能避免盘中 sell + 尾盘 sell 时,内存与 DB 不一致
                # 注意:execute_sell 已调用 _store.save_trade(trade),这里不再重复保存

                # 真实券商委托路径已删除(2026-07-14):sim_trader 永远不真下单,真单走 live_trader
                # 见 docs/审计报告/项目质量审计_2026-07-13_全项目.md 架构决定

        if sells:
            log.info(f"[卖出阶段] 共执行 {len(sells)} 笔卖出")
        # P1-4: 先落盘现金(save_state), 再清理并落盘持仓。
        # 语义"先确保卖出所得现金落袋, 再处理持仓", 配合 P1-1 原子写, 杜绝
        # "现金已增加但持仓未删"导致的重复计算窗口。
        if self._store:
            self._store.save_state(
                self.cash, self.consecutive_losses,
                self.pause_until, self._trade_count)
        # 清理已平仓
        self.positions = {k: v for k, v in self.positions.items() if v.is_active}
        # 保存当日快照供次日除权跳空保护
        self._prev_snap = {k: dict(v) for k, v in snapshot.items()}
        # #9 修复: 同步更新 _prev_day_snap(今日尾盘 = 次日开盘的"昨日")
        # 用 deep copy 避免后续就地修改 _prev_snap 内层 dict 时污染 prev_day
        self._prev_day_snap = copy.deepcopy(self._prev_snap)
        # L5 修复: 持久化 _prev_day_snap 到 store,供下次冷启动加载
        if self._store:
            self._store.save_prev_day_snap(self._prev_day_snap)
            self._store.save_positions(self.positions)

    # ── 记录 ──────────────────────────────────

    @_cycle_locked
    def record(self, today: date, snapshot: dict):
        # #8 修复:增量更新所有持仓的 current_price,让 market_value/profit_pct property 反映实时行情
        # 边界保护: bar['close'] 缺失/停牌(为 0)时,保持原值或 fallback 到 entry_price,避免突然归零
        for code, pos in self.positions.items():
            bar = snapshot.get(code)
            if bar:
                close_raw = bar.get('close')
                close_val = float(close_raw) if close_raw else 0.0
                pos.current_price = close_val if close_val > 0 else (pos.current_price or pos.entry_price)

        # L3 修复: 日切时清空 _today_trades(避免跨日累积)
        # 条件: 已累积且最后一笔的 exit_date < today(说明跨日了)
        if self._today_trades and self._today_trades[-1].exit_date < today:
            self._today_trades = []

        eq = self.total_equity(snapshot)

        # 净值可信度修复: 检测行情覆盖率 + 单日跳变
        covered, active_n = self.equity_price_coverage(snapshot)
        eq_source = 'record'
        if active_n > 0 and covered < active_n:
            # 有持仓但部分/全部缺实时报价 -> 净值可能失真(盘前手动触发/行情通道全挂)
            eq_source = 'partial'
            log.warning(
                f"[净值可信度] {today} 行情覆盖 {covered}/{active_n} 只持仓, "
                f"缺价股按上次市价/买入价估值, 净值={eq:,.0f} 可能不准 -> 标记 source=partial")
        # 运行期跳变告警: 与上一净值点比, 突变>15% 且非因当日大额交易, 提示排查
        try:
            prev_pts = self.equity_curve
            if prev_pts:
                prev_eq = float(prev_pts[-1].get('equity', 0) or 0)
                if prev_eq > 0 and abs(eq - prev_eq) / prev_eq > 0.15:
                    log.warning(
                        f"[净值跳变] {today} 净值 {prev_eq:,.0f} -> {eq:,.0f} "
                        f"({(eq-prev_eq)/prev_eq:+.1%}, >15%), 请核对行情/交易是否异常")
        except Exception:
            pass

        log.info(f"[快照] 日期={today} 权益={eq:,.0f} 现金={self.cash:,.0f} 持仓={self.position_count}")
        _safe_broadcast({"type":"sim_trader_log","action":"snapshot","date":str(today),"time":datetime.now().strftime('%H:%M:%S'),"equity":round(eq,0),"cash":round(self.cash,0),"positions":self.position_count})
        # P1-2: 统一经 save_equity_point 单路径落盘(键名 'pos', 同日去重),
        # 消除原"内存append('positions'键) + save_equity_point('pos'键)"对同一list双写产生的重复记录。
        if self._store:
            self._store.save_equity_point(today, eq, self.cash, self.position_count,
                                          source=eq_source)
            self._store.save_state(
                self.cash, self.consecutive_losses,
                self.pause_until, self._trade_count)
            # 内存曲线与 store 同步(加载后两者为同一引用; 此处确保无 store 分支外的一致性)
            self.equity_curve = self._store.load_equity_curve()
        else:
            # 无 store(纯内存模式): 自行维护, 同日去重, 统一 'pos' 键
            entry = {'date': str(today), 'equity': eq, 'cash': self.cash, 'pos': self.position_count}
            if self.equity_curve and str(self.equity_curve[-1].get('date')) == str(today):
                self.equity_curve[-1] = entry
            else:
                self.equity_curve.append(entry)
