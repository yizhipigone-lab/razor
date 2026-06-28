import sys
import os
import pandas as pd
import numpy as np

# 强制注入项目根目录
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from datetime import date, timedelta, datetime
import threading
import time
from typing import List, Dict, Optional, Callable

# 核心：必须严格同步 server.py 的引用路径
from database.duckdb_manager import db
from core.logger import get_logger
from core.settings import settings
from app.screener.engine import load_strategy

# L25 修复: 统一成交执行层(涨停过滤/T+1/成本)
from app.backtest.execution import can_buy, can_sell_today, calc_buy_cost, calc_sell_revenue

log = get_logger("Backtest")


class BacktestResult:
    """回测结果容器"""

    def __init__(self):
        self.trades: list = []            # 每笔交易记录
        self.stock_stats: dict = {}       # 每只股票盈亏统计
        self.total_pnl_pct: float = 0.0
        self.win_rate: float = 0.0
        self.total_trades: int = 0

    def to_summary_df(self) -> pd.DataFrame:
        if not self.stock_stats:
            return pd.DataFrame()
        return pd.DataFrame.from_dict(self.stock_stats, orient="index")

    def trades_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.trades)


class BacktestEngine:
    """
    量化回测引擎 (v3.1 OHLC-aware: High检测止盈+Low检测止损, 日内精确仿真)
    """

    def run(
        self,
        strategy_name: str,
        strategy_params: dict = None,
        start: date = None,
        end: date = None,
        exchanges: list = None,
        sectors: list = None,
        index_filter: list = None,    # 指数成分过滤 e.g. ['HS300','ZZ500']
        min_mv: float = None,          # 流通市值下限（亿元）
        max_mv: float = None,          # 流通市值上限（亿元）
        progress_callback: Optional[Callable] = None,
        stop_event: Optional[threading.Event] = None,
        params_override: dict = None,       # AI 优化器注入的止盈止损参数，覆盖全局 settings
        intraday_freq: str = None,         # None → 从 settings 读取默认值
        time_exit_min_pnl: float = None,    # 时间止盈最低盈利率(%)，None=无条件到期
        apply_costs: bool = None,           # 是否扣除交易成本(滑点+佣金+印花税)
        bj_filter: bool = True,             # 是否过滤北交所
        sh_red_filter: bool = False,        # 仅上证红盘日信号（Branch C专用）
        use_atr_stop: bool = False,          # ATR动态止损替代固定硬止损
        atr_stop_multiplier: float = 2.5,    # ATR止损倍数（N * ATR(14)）
        use_hot_concept: bool = False,       # 仅保留热门概念成分股
        hot_concept_top_n: int = 5,          # 取前N个最热概念
        use_vol_adaptive: bool = False,      # 波动率自适应止盈止损
        use_regime_filter: bool = False,     # 市场状态评分过滤（弱市减仓）
        use_kelly_sizing: bool = False,      # Kelly公式动态仓位
        use_vol_climax_exit: bool = False,   # 成交量高潮离场（出货日检测）
        use_portfolio: bool = None,         # 启用资金管理组合仿真
        initial_capital: float = None,      # 初始资金
        position_size: float = None,        # 单票仓位上限
        streak_pause: int = None,           # 连败保护：连续N笔亏损后暂停（0=禁用）
        pause_days: int = None,             # 连败暂停天数
    ) -> BacktestResult:
        # 参数解析：显式传入 > settings 默认值
        if intraday_freq is None:
            intraday_freq = settings.backtest_intraday_freq
        if apply_costs is None:
            apply_costs = settings.backtest_apply_costs
        if use_portfolio is None:
            use_portfolio = settings.backtest_use_portfolio
        if initial_capital is None:
            initial_capital = settings.backtest_initial_capital
        if position_size is None:
            position_size = settings.backtest_position_size
        if streak_pause is None:
            streak_pause = settings.backtest_streak_pause
        if pause_days is None:
            pause_days = settings.backtest_pause_days
        self._use_atr_stop = use_atr_stop
        self._atr_stop_multiplier = atr_stop_multiplier
        self._use_vol_adaptive = use_vol_adaptive
        self._use_vol_climax_exit = use_vol_climax_exit
        def _prog(step, total, msg):
            if progress_callback:
                progress_callback(step, total, msg)
            log.info(f"[回测 {step}/{total}] {msg}")

        result = BacktestResult()
        start_date = start or (date.today() - timedelta(days=365))
        end_date = end or date.today()

        _prog(1, 5, "筛选回测目标股票...")
        # 优先使用带过滤的方法（ST剔除+指数+市值）
        use_filtered = bool(index_filter or min_mv is not None or max_mv is not None)
        if use_filtered:
            stocks = db.get_stocks_filtered(index_filter=index_filter, min_mv=min_mv, max_mv=max_mv)
        else:
            stocks = db.get_all_stocks()
            # 非指数路径也统一剔除 ST
            if "name" in stocks.columns:
                stocks = stocks[~stocks["name"].str.contains(r'\*?ST', na=False)]
        if exchanges:
            stocks = stocks[stocks["exchange"].isin(exchanges)]
        # 北交所过滤（8开头代码 或 exchange=BJ）
        if bj_filter and "exchange" in stocks.columns:
            stocks = stocks[stocks["exchange"] != "BJ"]
        if bj_filter and "code" in stocks.columns:
            stocks = stocks[~stocks["code"].astype(str).str.startswith('8')]
        if sectors:
            def _match_sector(val):
                val_str = str(val)
                for s_filter in sectors:
                    if s_filter.startswith("CAT:"):
                        cat_char = s_filter[4:].upper()
                        if val_str.startswith(cat_char): return True
                    elif s_filter in val_str:
                        return True
                return False
            stocks = stocks[stocks["sector"].apply(_match_sector)]
        
        codes = stocks["code"].tolist()
        code_to_name = dict(zip(stocks["code"], stocks["name"]))
        
        if not codes:
            _prog(5, 5, "未发现符合条件的股票")
            return result

        _prog(2, 5, f"加载 {len(codes)} 只股票的历史数据进行快速扫描...")
        # 【核心修正】向前回溯 365 天以确保策略(如 MA60、RPS)有足够的历史上下文计算指标
        load_start = start_date - timedelta(days=365)
        raw_bars = db.load_all_bars(freq="daily", start=load_start, end=end_date, codes=codes)
        bars = raw_bars.to_pandas() if hasattr(raw_bars, "to_pandas") else pd.DataFrame(raw_bars)
            
        if bars.empty:
            _prog(5, 5, "数据库无 K 线数据")
            return result
            
        for col in ["open", "high", "low", "close", "volume"]:
            if col in bars.columns:
                bars[col] = pd.to_numeric(bars[col], errors='coerce')
        
        bars = bars.dropna(subset=["close"])
        bars['date'] = pd.to_datetime(bars['date']).dt.date
        bars = bars.sort_values(['code', 'date'])

        _prog(3, 5, "计算策略信号 (基于日线信号库)...")
        strategy = load_strategy(strategy_name, params=strategy_params)
        signals = strategy.generate_signals(bars)
        
        # 【核心修正】引擎自我保护：只保留被明确标记为买入信号的行
        if signals is not None and not signals.empty and 'buy_signal' in signals.columns:
            signals = signals[signals['buy_signal'] == True]
            
        if signals is None or signals.empty:
            _prog(5, 5, "当前期间未产生交易信号")
            return result
            
        # 裁剪掉为了计算指标而多加载的历史时间段里的信号
        signals['date'] = pd.to_datetime(signals['date']).dt.date
        signals = signals[(signals['date'] >= start_date) & (signals['date'] <= end_date)]

        if signals.empty:
            _prog(5, 5, "精确回测期间内未产生有效信号 (早期冗余信号已过滤)")
            return result

        # 上证红盘过滤器（Branch C 专用）
        if sh_red_filter:
            try:
                from pathlib import Path as _Path
                from database.duckdb_manager import PARQUET_DAILY_DIR
                sh_path = _Path(PARQUET_DAILY_DIR) / "index_000001.parquet"
                if sh_path.exists():
                    sh_bars = pd.read_parquet(str(sh_path))
                    sh_bars["date"] = pd.to_datetime(sh_bars["date"]).dt.date
                    # 红盘 = 收盘价 > 开盘价（当日上涨）
                    sh_bars["is_red"] = sh_bars["close"] > sh_bars["open"]
                    red_dates = set(sh_bars[sh_bars["is_red"]]["date"].tolist())
                    n_before = len(signals)
                    signals = signals[signals["date"].isin(red_dates)]
                    _prog(3, 5, f"上证红盘过滤: {n_before} → {len(signals)} 条信号")
            except Exception as e:
                log.warning(f"上证红盘过滤失败: {e}，跳过此过滤器")

        # 热门概念过滤：仅保留属于前N个最热概念的股票信号
        if use_hot_concept:
            try:
                signal_dates = sorted(signals['date'].unique())
                date_list = "', '".join(str(d) for d in signal_dates)
                eligible_sql = f"""
                WITH ranked AS (
                    SELECT trade_date, concept_name,
                           ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY hotness DESC) AS rn
                    FROM concept_heat
                    WHERE trade_date IN ('{date_list}')
                ),
                top_concepts AS (
                    SELECT trade_date, concept_name FROM ranked WHERE rn <= {hot_concept_top_n}
                )
                SELECT DISTINCT tc.trade_date, cs.stock_code
                FROM top_concepts tc
                JOIN concept_stocks cs ON tc.concept_name = cs.concept_name
                """
                eligible = db.conn.execute(eligible_sql).df()
                if not eligible.empty:
                    eligible['trade_date'] = pd.to_datetime(eligible['trade_date']).dt.date
                    eligible_set = set(
                        (row['trade_date'], row['stock_code']) for _, row in eligible.iterrows()
                    )
                    n_before = len(signals)
                    signals = signals[
                        signals.apply(lambda r: (r['date'], r['code']) in eligible_set, axis=1)
                    ]
                    _prog(3, 5, f"热门概念过滤 (Top{hot_concept_top_n}): {n_before} → {len(signals)} 条信号")
                else:
                    _prog(3, 5, "热门概念过滤: 无匹配数据，保留全部信号")
            except Exception as e:
                log.warning(f"热门概念过滤失败: {e}，跳过此过滤器")

        _prog(4, 5, f"处理 {len(signals)} 条信号，日线OHLC仿真...")

        all_trades = []
        for i, (_, row) in enumerate(signals.iterrows()):
            if stop_event and stop_event.is_set(): break
            code = row["code"]
            name = code_to_name.get(code, code)
            signal_date = row["date"]
            if i % 10 == 0:
                _prog(4, 5, f"模拟第 {i+1}/{len(signals)} 笔交易: {name}...")
            
            entry_price = float(row.get("close", 0))
            if entry_price <= 0: continue

            # ★ 实盘过滤①: 停牌检测（成交量为0）
            vol = float(row.get("volume", 1) or 1)
            if vol == 0:
                log.debug(f"过滤 [{code}] 信号日停牌，跳过")
                continue

            # ★ 实盘过滤②: 涨跌停检测(L25: 改用 execution.can_buy 统一规则)
            pre_close = float(row.get("pre_close", 0) or 0)
            if pre_close <= 0:
                # 从 bars 中找前一根日线收盘价
                prev = bars[(bars["code"] == code) & (bars["date"] < signal_date)]
                pre_close = float(prev["close"].iloc[-1]) if not prev.empty else 0
            if pre_close > 0:
                # L25: 统一通过 execution.can_buy 判断(支持主板/创业/科创/北证)
                ok, reason = can_buy(code, prev_close=pre_close, today_high=entry_price)
                if not ok:
                    log.debug(f"跳过 [{code}] 信号日{reason}，跳过")
                    continue

            # 日线OHLC仿真
            stock_daily = bars[bars["code"] == code]
            bars_daily = stock_daily[stock_daily["date"] >= signal_date]
            trade = self._simulate_trade_daily_fallback(code, name, entry_price, signal_date, bars_daily, params_override=params_override, time_exit_min_pnl=time_exit_min_pnl)

            if trade:
                # 传递quality和股票元信息给组合管理
                if 'quality' in row.index:
                    trade['quality'] = float(row['quality'])
                if 'close_pos' in row.index:
                    trade['close_pos'] = float(row['close_pos'])
                if 'x1' in row.index:
                    trade['x1'] = float(row['x1'])
                all_trades.append(trade)

        result.trades = all_trades
        result.total_trades = len(all_trades)

        # ── 投资组合资金管理仿真 ──────────────────────────
        if use_portfolio and all_trades:
            funded_trades, final_value, skipped, monthly = self._replay_portfolio(
                all_trades, initial_capital, position_size,
                streak_pause=streak_pause, pause_days=pause_days,
                use_kelly=use_kelly_sizing, use_regime=use_regime_filter
            )
            result.portfolio_initial_capital = initial_capital
            result.portfolio_final_value = final_value
            result.portfolio_total_return = (final_value - initial_capital) / initial_capital * 100
            result.portfolio_trades = funded_trades
            result.portfolio_skipped = skipped
            result.portfolio_monthly = monthly
            all_trades = funded_trades
            result.trades = funded_trades
            result.total_trades = len(funded_trades)

        if all_trades:
            # 根据用户建议：胜率分母 = 盈利笔数 + 亏损笔数 (剔除那些因数据截止而导致的 0% 平手单)
            wins = [t for t in all_trades if t["pnl_pct"] > 0]
            losses = [t for t in all_trades if t["pnl_pct"] < 0]
            
            w_cnt = len(wins)
            l_cnt = len(losses)
            total_valid = w_cnt + l_cnt
            
            if total_valid > 0:
                result.win_rate = round((w_cnt / total_valid) * 100, 2)
            else:
                result.win_rate = 0.0
                
            result.total_pnl_pct = round(sum(t["pnl_pct"] for t in all_trades) / len(all_trades), 2)
            for t in all_trades: 
                result.stock_stats[t["code"]] = t
            
        _prog(5, 5, f"回测任务完成: {len(all_trades)}笔(含{len(all_trades)-total_valid if all_trades else 0}笔平手离场)")
        return result

    # 实盘交易成本（万三佣金 + 千一滑点 + 千0.5印花税）
    TRADE_COST_BUY  = 0.125   # 买入: 0.1%滑点 + 0.025%佣金 = 0.125%
    TRADE_COST_SELL = 0.175   # 卖出: 0.1%滑点 + 0.025%佣金 + 0.05%印花税 = 0.175%

    @staticmethod
    def _compute_regime(signal_date, lookback=60):
        """市场状态评分 0~1（仅用价格结构，零指标）
        1.0=强牛市, 0.5=中性, 0.0=强熊市"""
        from pathlib import Path as _Path
        from database.duckdb_manager import PARQUET_DAILY_DIR
        sh_path = _Path(PARQUET_DAILY_DIR) / "index_000001.parquet"
        if not sh_path.exists():
            return 0.5
        try:
            sh = pd.read_parquet(str(sh_path))
            sh['date'] = pd.to_datetime(sh['date']).dt.date
            sh = sh[sh['date'] <= signal_date].tail(lookback)
            if len(sh) < 20:
                return 0.5
            c = sh['close']
            hh = c.rolling(20).max()
            ll = c.rolling(20).min()
            donchian_pos = (c.iloc[-1] - ll.iloc[-1]) / max(hh.iloc[-1] - ll.iloc[-1], 0.01)
            roc5 = c.iloc[-1] / c.iloc[-5] - 1 if len(c) >= 5 else 0
            roc20 = c.iloc[-1] / c.iloc[-20] - 1 if len(c) >= 20 else 0
            tr = sh['high'] - sh['low']
            vol_now = tr.tail(10).mean()
            vol_prev = tr.iloc[-20:-10].mean() if len(tr) >= 20 else vol_now
            vol_expanding = vol_now > vol_prev * 1.05
            above_ma5 = (c > c.rolling(5).mean()).tail(10).mean()
            score = (
                max(0, min(1, donchian_pos)) * 0.25 +
                (0.5 if roc5 > 0 else 0) * 0.20 +
                (0.5 if roc20 > 0 else 0) * 0.25 +
                above_ma5 * 0.10 +
                (0.3 if not vol_expanding else 0.1) * 0.20
            )
            return max(0.0, min(1.0, score))
        except Exception:
            return 0.5

    @staticmethod
    def _replay_portfolio(trades: list, initial_capital: float, position_size: float,
                          streak_pause: int = 0, pause_days: int = 2,
                          use_kelly: bool = False, use_regime: bool = False):
        """按时间顺序重放交易，模拟资金管理约束。

        规则：
        - 每只股票买入花费 position_size，卖出后资金回笼（含盈亏）
        - 当日先处理卖出（释放资金），再处理买入
        - 可用资金不足时跳过买入信号
        - 连败保护：连续 streak_pause 笔亏损后暂停 pause_days 天
        - 返回: (实际成交的交易列表, 最终资金, 跳过的信号数, 月度统计)
        """
        from datetime import datetime as _dt, timedelta as _td

        events = []
        for t in trades:
            buy_date = str(t.get("buy_date", ""))
            sell_date = str(t.get("sell_date", ""))[:10]
            events.append({"type": "sell", "date": sell_date, "trade": t})
            events.append({"type": "buy",  "date": buy_date,  "trade": t})

        events.sort(key=lambda e: (e["date"], 0 if e["type"] == "sell" else 1))

        cash = initial_capital
        open_positions = 0  # 当前持有的仓位数量
        # L27 修复: 用 position_value(市值) 替代 invested_capital(成本价)
        # 买入时用 entry_price*shares(=position_size)作为初始市值;
        # 卖出时 cash 已含 close*shares, 无需额外累加
        position_value = 0   # 未平仓持仓的市值(买入=成本, 卖出前每天其实无法实时标记)
        funded_ids = set()   # 已成交交易的 code+buy_date，用于过滤卖单
        funded = []
        skipped = 0
        streak_losses = 0
        pause_until = None

        # 月度统计追踪
        peak_nav = initial_capital
        month_nav = {}       # {YYYY-MM: end_of_month_nav}
        month_entries = {}   # {YYYY-MM: count}
        month_closes = {}    # {YYYY-MM: count}
        month_pnl_sum = {}   # {YYYY-MM: total_pnl_元}
        month_peak = {}      # {YYYY-MM: peak NAV during month}
        month_trough = {}    # {YYYY-MM: trough NAV after peak}

        for evt in events:
            date_str = evt["date"]
            if not date_str or len(date_str) < 7:
                continue
            month_key = date_str[:7]

            if evt["type"] == "sell":
                t = evt["trade"]
                tid = (t.get("code", ""), str(t.get("buy_date", "")))
                if tid not in funded_ids:
                    continue  # 未成交的买单对应的卖单，跳过
                funded_ids.discard(tid)
                actual_pos = t.get("_position_size", position_size)
                pnl_yuan = actual_pos * t["pnl_pct"] / 100
                # L27: 卖出时 close*shares 进 cash(act_pos+pnl), 清掉该仓位的市值
                position_value -= actual_pos
                cash += actual_pos + pnl_yuan
                open_positions -= 1

                # 月度统计
                month_closes[month_key] = month_closes.get(month_key, 0) + 1
                month_pnl_sum[month_key] = month_pnl_sum.get(month_key, 0) + pnl_yuan

                # 连败追踪
                if t["pnl_pct"] <= 0:
                    streak_losses += 1
                    if streak_pause > 0 and streak_losses >= streak_pause:
                        try:
                            pd_dt = _dt.strptime(date_str, "%Y-%m-%d") + _td(days=pause_days)
                            pause_until = pd_dt.strftime("%Y-%m-%d")
                        except ValueError:
                            pass
                else:
                    streak_losses = 0
                    pause_until = None

            else:  # buy
                # 连败暂停检查
                if pause_until and date_str <= pause_until:
                    skipped += 1
                    continue

                # 计算实际仓位金额
                actual_pos = position_size

                # 市场状态过滤：弱市减仓
                if use_regime:
                    regime = BacktestEngine._compute_regime(date_str)
                    if regime < 0.25:       # 极弱市：跳过
                        skipped += 1
                        continue
                    actual_pos *= (0.3 + regime * 0.7)  # 0.3~1.0

                # Kelly公式动态仓位
                if use_kelly and len(funded) >= 5:
                    recent = [t for t in funded[-20:] if t.get('pnl_pct', 0) != 0]
                    if len(recent) >= 5:
                        wins = [t for t in recent if t['pnl_pct'] > 0]
                        losses = [t for t in recent if t['pnl_pct'] < 0]
                        wr = len(wins) / len(recent) if recent else 0.5
                        avg_win = sum(t['pnl_pct'] for t in wins) / len(wins) if wins else 5.0
                        avg_loss = abs(sum(t['pnl_pct'] for t in losses) / len(losses)) if losses else 5.0
                        rr = avg_win / max(avg_loss, 0.1)
                        kelly = max(0.02, wr - (1 - wr) / max(rr, 0.5))
                        quality = float(evt['trade'].get('quality', 0.5))
                        quality_mult = 0.5 + quality * 0.8  # 0.5~1.3
                        kelly_adj = kelly * quality_mult * 0.5
                        actual_pos = min(actual_pos * max(0.3, min(kelly_adj * 10, 1.5)), cash * 0.15)
                    actual_pos = max(position_size * 0.3, min(actual_pos, position_size * 1.5))

                if cash >= actual_pos:
                    cash -= actual_pos
                    # L27: 买入时用车位成本作为初始市值(entry_price * shares = position_size)
                    position_value += actual_pos
                    open_positions += 1
                    tid = (evt["trade"].get("code", ""), str(evt["trade"].get("buy_date", "")))
                    funded_ids.add(tid)
                    # 记录实际仓位到交易记录中
                    evt["trade"]["_position_size"] = actual_pos
                    funded.append(evt["trade"])
                    month_entries[month_key] = month_entries.get(month_key, 0) + 1
                else:
                    skipped += 1

            # L27: 净值 = 现金 + 持仓市值(close*shares, 暂无实时价时用成本近似)
            nav = cash + position_value
            m_peak = month_peak.get(month_key, nav)
            month_peak[month_key] = max(m_peak, nav)
            m_trough = month_trough.get(month_key, nav)
            if nav < m_peak:
                month_trough[month_key] = min(m_trough, nav)
            peak_nav = max(peak_nav, nav)

            # 记录月末净值（最后一次事件覆盖）
            month_nav[month_key] = nav

        # ── 整理月度统计 ──
        monthly = []
        all_time_peak = initial_capital
        for mk in sorted(month_nav.keys()):
            nav = month_nav[mk]
            all_time_peak = max(all_time_peak, nav)
            entries = month_entries.get(mk, 0)
            closes = month_closes.get(mk, 0)
            pnl = month_pnl_sum.get(mk, 0)
            # 当月回撤
            mp = month_peak.get(mk, nav)
            mt = month_trough.get(mk, mp)
            month_dd = (mp - mt) / mp * 100 if mp > 0 else 0
            monthly.append({
                "month": mk,
                "nav": round(nav, 0),
                "entries": entries,
                "closes": closes,
                "pnl_yuan": round(pnl, 0),
                "max_dd_pct": round(month_dd, 1),
            })

        return funded, cash, skipped, monthly

    def _simulate_trade_v2(self, code: str, stock_name: str, entry_price: float, signal_date, bars_5m: pd.DataFrame, params_override: dict = None, time_exit_min_pnl: float = None, apply_costs: bool = True) -> Optional[dict]:
        """[v3.2 cost-aware] 日内精确仿真：
        优先级：分档止盈(H检测) → 硬止损(L检测) → 回落止盈 → 条件时间到期
        TP用High检测（先涨先触发），SL用Low检测（后跌后触发），TP优先，同bar止盈触发则跳过止损。
        apply_costs=True 时扣除滑点+佣金+印花税，还原实盘真实收益。
        """
        # L20 修复: 风控参数从 schema 加载(唯一真相源)。
        # config.py → schema → engine 三层单向链;settings 不再是风控参数中间层。
        # params_override 仅用于 AI 优化器临时覆盖,不影响真相源。
        from app.config.schema import load_risk_params
        _risk = load_risk_params()
        # key -> schema 字段名 映射(单位:百分比, 需要 *100)
        _SCHEMA_PCT_FIELDS = {
            'hard_stop_loss_pct': 'hard_stop',
            'breakeven_threshold_pct': 'breakeven_threshold',
            'breakeven_stop_pnl_pct': 'breakeven_stop',
            'trailing_activate_pct': 'trail_activate',
            'trailing_drawdown_pct': 'trail_dd',
            'first_day_exit_min_profit': 'first_day_exit_min_profit',
        }
        # key -> schema 字段名 映射(单位:整数天, 直接取)
        _SCHEMA_INT_FIELDS = {
            'time_exit_days': 'time_exit_days',
            'time_exit_force_days': 'time_force_days',
            'first_day_exit_days': 'first_day_exit_days',
        }
        def _p(key):
            """params_override 优先;否则从 schema 读(唯一真相源);缺键报错(无假默认)"""
            if params_override and key in params_override:
                return params_override[key]
            if key in _SCHEMA_PCT_FIELDS:
                return getattr(_risk, _SCHEMA_PCT_FIELDS[key]) * 100
            if key in _SCHEMA_INT_FIELDS:
                return getattr(_risk, _SCHEMA_INT_FIELDS[key])
            raise RuntimeError(f"engine.py 缺风控参数: {key},无假默认,需在 schema 或 params_override 配置")

        hard_sl      = _p('hard_stop_loss_pct')
        be_thresh    = _p('breakeven_threshold_pct')
        be_stop      = _p('breakeven_stop_pnl_pct')
        trail_act    = _p('trailing_activate_pct')
        trail_dd     = _p('trailing_drawdown_pct')
        max_hold     = _p('time_exit_days')
        force_hold   = _p('time_exit_force_days')
        fd_min_profit = _p('first_day_exit_min_profit')
        fd_days = _p('first_day_exit_days')

        # ATR 动态止损：用入场前14日ATR计算止损百分比
        vol_scale = 1.0  # 波动率缩放因子，默认不变
        if getattr(self, '_use_atr_stop', False) or getattr(self, '_use_vol_adaptive', False):
            daily = bars_5m.set_index('datetime' if 'datetime' in bars_5m.columns else 'date')
            daily_ohlc = daily['close'].resample('1D').ohlc() if hasattr(daily, 'resample') else None
            if daily_ohlc is not None and len(daily_ohlc) >= 15:
                prev_high = daily_ohlc['high'].shift(1)
                prev_low = daily_ohlc['low'].shift(1)
                prev_close = daily_ohlc['close'].shift(1)
                tr1 = prev_high - prev_low
                tr2 = (prev_high - prev_close).abs()
                tr3 = (prev_low - prev_close).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                atr14 = tr.tail(14).mean()
                if atr14 > 0:
                    atr_pct = atr14 / entry_price
                    # 波动率自适应：用ATR%相对基准3%的比值缩放所有阈值
                    if getattr(self, '_use_vol_adaptive', False):
                        vol_scale = max(0.5, min(2.0, atr_pct / 0.03))
                    # ATR硬止损
                    if getattr(self, '_use_atr_stop', False):
                        atr_stop_price = entry_price - self._atr_stop_multiplier * atr14
                        atr_stop_pct = (atr_stop_price / entry_price - 1) * 100
                        hard_sl = min(hard_sl, atr_stop_pct)

        # 分档止盈：优先从 params_override 扁平化构建，否则读 settings
        if params_override and 'tp1_profit' in params_override:
            # P0-1 单位约定: tp*_profit 是百分比格式(3.0=3%)，take_profit_tiers.profit_pct
            # 统一为小数约定(0.03)，与 config / exit_rules 判定口径一致 → 此处 /100
            active_tp_plan = [
                {"profit_pct": params_override.get('tp1_profit', 10.0) / 100.0,
                 "sell_ratio": params_override.get('tp1_ratio', 0.33),
                 "label": "分阶止盈1"},
                {"profit_pct": params_override.get('tp2_profit', 20.0) / 100.0,
                 "sell_ratio": params_override.get('tp2_ratio', 0.33),
                 "label": "分阶止盈2"},
            ]
            if 'tp3_profit' in params_override:
                active_tp_plan.append({
                    "profit_pct": params_override.get('tp3_profit', 30.0) / 100.0,
                    "sell_ratio": params_override.get('tp3_ratio', 0.34),
                    "label": "分阶止盈3", "sell_all": True,
                })
        else:
            active_tp_plan = settings.staged_take_profit or []

        # 波动率自适应：止损/移动止盈阈值 × vol_scale，止盈目标不变
        if vol_scale != 1.0:
            hard_sl = max(hard_sl * vol_scale, -11.0)
            trail_act = trail_act * vol_scale
            trail_dd = trail_dd * vol_scale
            # 止盈目标不缩放——高波动股需要更宽的止损但不改变获利预期

        # 成交量高潮离场：计算20日均量（基准）
        _climax_avg_vol = None
        if getattr(self, '_use_vol_climax_exit', False):
            daily_idx = bars_5m.set_index('datetime' if 'datetime' in bars_5m.columns else 'date')
            daily_v = daily_idx['volume'].resample('1D').sum() if hasattr(daily_idx, 'resample') else None
            if daily_v is not None and len(daily_v) >= 25:
                _climax_avg_vol = daily_v.iloc[-25:-1].mean()  # 入场前20日均量

        remaining_ratio = 1.0
        highest = entry_price
        trailing_active = False
        staged_done = set()
        realized_pnl = 0.0
        exit_price, exit_date, hold_days = None, None, 0
        # 成交量高潮检测：逐日累积
        _day_vol, _day_high, _day_low, _day_close = 0.0, 0.0, 1e9, 0.0
        sell_events = [{"type": "buy", "date": str(signal_date), "price": entry_price,
                        "ratio": 1.0, "reason": "策略信号买入"}]

        time_col = "datetime" if "datetime" in bars_5m.columns else "date"

        # 成本调整：买入成本摊入 entry_price，卖出时单独扣除
        cost_entry = entry_price * (1 + self.TRADE_COST_BUY / 100) if apply_costs else entry_price

        def _cost_pnl(raw_sell_price, ratio):
            """计算扣除交易成本后的实际盈亏贡献"""
            if apply_costs:
                cost_sell = raw_sell_price * (1 - self.TRADE_COST_SELL / 100)
                return ((cost_sell / cost_entry) - 1) * 100 * ratio
            return ((raw_sell_price / entry_price) - 1) * 100 * ratio

        from app.backtest.exit_rules import exit_rule_engine, _pct
        from dataclasses import dataclass

        @dataclass
        class _MirrorPos:
            entry_price: float = 0
            peak_price: float = 0
            tp_triggered: set = None
            def __post_init__(self):
                if self.tp_triggered is None:
                    self.tp_triggered = set()

        for i, (_, row) in enumerate(bars_5m.iterrows()):
            curr_dt = row[time_col]
            curr_date = curr_dt.date() if hasattr(curr_dt, 'date') else curr_dt
            if curr_date <= signal_date:
                continue

            if i > 0:
                prev_date = bars_5m.iloc[i - 1][time_col]
                prev_date = prev_date.date() if hasattr(prev_date, 'date') else prev_date
                if curr_date != prev_date:
                    hold_days += 1
                    # 日边界：用统一规则引擎检查首日弱势离场 + 成交量高潮
                    if fd_min_profit > 0 or _climax_avg_vol is not None:
                        mp = _MirrorPos(entry_price, highest, staged_done)
                        ctx_p = {
                            "hard_stop": -0.99, "take_profit_tiers": [],
                            "trail_activate": 0.99, "trail_dd": 0.99,
                            "time_exit_days": 999, "time_exit_profit": 0.99,
                            "time_force_days": 999,
                            "first_day_exit_min_profit": fd_min_profit,
                            "first_day_exit_days": fd_days,
                        }
                        ctx = exit_rule_engine.build_context(
                            mp, {"close": _day_close, "high": _day_high, "low": _day_low, "open": _day_close},
                            hold_days, ctx_p, first_day_hold_value=2
                        )
                        if _climax_avg_vol is not None and _day_vol > 0:
                            ctx.vol_climax_enabled = True
                            ctx.vol_climax_avg = _climax_avg_vol
                            ctx.vol_climax_day_vol = _day_vol
                            ctx.vol_climax_day_high = _day_high
                            ctx.vol_climax_day_low = _day_low
                            ctx.vol_climax_day_close = _day_close
                        sig = exit_rule_engine.check(ctx)
                        if sig:
                            realized_pnl += _cost_pnl(_day_close, remaining_ratio)
                            sell_events.append({"type": "sell", "date": str(prev_date),
                                                "price": sig.sell_price, "ratio": remaining_ratio,
                                                "reason": sig.reason})
                            remaining_ratio, exit_price, exit_date = 0, sig.sell_price, prev_date
                            break
                    # 重置日累积
                    _day_vol, _day_high, _day_low, _day_close = 0.0, 0.0, 1e9, 0.0

            price_h = float(row["high"])
            price_l = float(row["low"])
            price_c = float(row["close"])
            # 日累积
            _day_vol += float(row.get("volume", 0))
            _day_high = max(_day_high, price_h)
            _day_low = min(_day_low, price_l)
            _day_close = price_c
            highest = max(highest, price_h)

            # 用统一规则引擎逐bar检查
            mp = _MirrorPos(entry_price, highest, staged_done)
            ctx_p2 = {
                "hard_stop": _pct(hard_sl),
                "take_profit_tiers": active_tp_plan,
                "trail_activate": _pct(trail_act),
                "trail_dd": _pct(trail_dd),
                "time_exit_days": max_hold,
                "time_exit_profit": _pct(time_exit_min_pnl) if time_exit_min_pnl is not None else 0.01,
                "time_force_days": force_hold,
                "first_day_exit_min_profit": 0.0,
                "first_day_exit_days": 1,
                "breakeven_threshold_pct": be_thresh,
                "breakeven_stop_pnl_pct": be_stop,
            }
            ctx2 = exit_rule_engine.build_context(
                mp, {"open": float(row.get("open", price_c)), "high": price_h, "low": price_l, "close": price_c},
                hold_days, ctx_p2, use_high_for_tp=True, first_day_hold_value=1
            )
            sig2 = exit_rule_engine.check(ctx2)
            if sig2:
                reason = sig2.reason
                if reason.startswith("TP"):
                    idx = int(reason[2]) - 1
                    staged_done.add(idx)
                    for si, stage in enumerate(active_tp_plan):
                        if si == idx:
                            sell_ratio = remaining_ratio if stage.get("sell_all") else stage.get("sell_ratio", 0.0)
                            actual_sell = min(sell_ratio, remaining_ratio)
                            if actual_sell > 0:
                                tp_pct = stage.get("profit_pct", 999.0)
                                # P0-1: tp_pct 已统一为小数(0.03=3%)，成交价 = entry*(1+小数)
                                tp_price = entry_price * (1 + tp_pct)
                                realized_pnl += _cost_pnl(tp_price, actual_sell)
                                sell_events.append({"type": "sell", "date": str(curr_dt),
                                                    "price": tp_price, "ratio": actual_sell,
                                                    "reason": stage.get("label", reason)})
                                remaining_ratio -= actual_sell
                            if remaining_ratio <= 0:
                                exit_price, exit_date = tp_price, curr_dt
                                break
                    if remaining_ratio <= 0:
                        break
                else:
                    sell_px = sig2.sell_price
                    realized_pnl += _cost_pnl(sell_px, remaining_ratio)
                    sell_events.append({"type": "sell", "date": str(curr_dt),
                                        "price": sell_px, "ratio": remaining_ratio,
                                        "reason": reason})
                    remaining_ratio = 0
                    exit_price = sell_px
                    exit_date = curr_dt
                    break
                remaining_ratio, exit_price, exit_date = 0, price_c, curr_dt
                break

        # 数据耗尽 → 期末平仓
        if remaining_ratio > 0:
            last = bars_5m.iloc[-1]
            exit_price = float(last["close"])
            exit_date = last[time_col]
            realized_pnl += _cost_pnl(exit_price, remaining_ratio)
            sell_events.append({"type": "sell", "date": str(exit_date), "price": exit_price,
                                "ratio": remaining_ratio, "reason": "期末平仓"})

        return self._wrap_result(code, stock_name, entry_price, exit_price,
                                 signal_date, exit_date, hold_days, realized_pnl, sell_events)

    def _simulate_trade_daily_fallback(self, code: str, stock_name: str, entry_price: float,
                                       signal_date, bars_daily: pd.DataFrame,
                                       params_override: dict = None,
                                       time_exit_min_pnl: float = None) -> Optional[dict]:
        """[v3.1 日线降级] OHLC-aware：TP用High检测，SL用Low检测，TP优先于SL。条件时间到期。"""
        if bars_daily.empty:
            return None

        # L20 修复: 同上,风控参数从 schema 读(唯一真相源)。
        # 与 _simulate_trade_v2 保持完全一致的查表逻辑。
        from app.config.schema import load_risk_params
        _risk = load_risk_params()
        _SCHEMA_PCT_FIELDS = {
            'hard_stop_loss_pct': 'hard_stop',
            'breakeven_threshold_pct': 'breakeven_threshold',
            'breakeven_stop_pnl_pct': 'breakeven_stop',
            'trailing_activate_pct': 'trail_activate',
            'trailing_drawdown_pct': 'trail_dd',
            'first_day_exit_min_profit': 'first_day_exit_min_profit',
        }
        _SCHEMA_INT_FIELDS = {
            'time_exit_days': 'time_exit_days',
            'time_exit_force_days': 'time_force_days',
            'first_day_exit_days': 'first_day_exit_days',
        }
        def _p(key):
            """params_override 优先;否则从 schema 读(唯一真相源);缺键报错(无假默认)"""
            if params_override and key in params_override:
                return params_override[key]
            if key in _SCHEMA_PCT_FIELDS:
                return getattr(_risk, _SCHEMA_PCT_FIELDS[key]) * 100
            if key in _SCHEMA_INT_FIELDS:
                return getattr(_risk, _SCHEMA_INT_FIELDS[key])
            raise RuntimeError(f"engine.py 缺风控参数: {key},无假默认,需在 schema 或 params_override 配置")

        hard_sl      = _p('hard_stop_loss_pct')
        be_thresh    = _p('breakeven_threshold_pct')
        be_stop      = _p('breakeven_stop_pnl_pct')
        trail_act    = _p('trailing_activate_pct')
        trail_dd     = _p('trailing_drawdown_pct')
        max_hold     = _p('time_exit_days')
        force_hold   = _p('time_exit_force_days')
        fd_min_profit = _p('first_day_exit_min_profit')
        fd_days = _p('first_day_exit_days')

        if params_override and 'tp1_profit' in params_override:
            # P0-1 单位约定: tp*_profit 是百分比格式(3.0=3%)，take_profit_tiers.profit_pct
            # 统一为小数约定(0.03)，与 config / exit_rules 判定口径一致 → 此处 /100
            active_tp_plan = [
                {"profit_pct": params_override.get('tp1_profit', 10.0) / 100.0,
                 "sell_ratio": params_override.get('tp1_ratio', 0.33),
                 "label": "分阶止盈1"},
                {"profit_pct": params_override.get('tp2_profit', 20.0) / 100.0,
                 "sell_ratio": params_override.get('tp2_ratio', 0.33),
                 "label": "分阶止盈2"},
            ]
            if 'tp3_profit' in params_override:
                active_tp_plan.append({
                    "profit_pct": params_override.get('tp3_profit', 30.0) / 100.0,
                    "sell_ratio": params_override.get('tp3_ratio', 0.34),
                    "label": "分阶止盈3", "sell_all": True,
                })
        else:
            active_tp_plan = settings.staged_take_profit or []

        remaining_ratio = 1.0
        highest = entry_price
        trailing_active = False
        staged_done = set()
        realized_pnl = 0.0
        exit_price, exit_date, hold_days = None, None, 0
        sell_events = [{"type": "buy", "date": str(signal_date), "price": entry_price,
                        "ratio": 1.0, "reason": "买入(日线)"}]

        from app.backtest.exit_rules import exit_rule_engine, _pct
        from dataclasses import dataclass

        # 镜像持仓，适配 build_context
        @dataclass
        class _MirrorPos:
            entry_price: float = 0
            peak_price: float = 0
            tp_triggered: set = None
            def __post_init__(self):
                self.tp_triggered = self.tp_triggered or set()

        for _, row in bars_daily.iterrows():
            d = row["date"]
            if d <= signal_date:
                continue
            hold_days += 1
            price_h = float(row["high"])
            price_l = float(row["low"])
            price_c = float(row["close"])
            highest = max(highest, price_h)

            close_pnl = (price_c / entry_price - 1) * 100

            # 构建上下文
            ctx_params = {
                "hard_stop": _pct(hard_sl),
                "take_profit_tiers": active_tp_plan,
                "trail_activate": _pct(trail_act),
                "trail_dd": _pct(trail_dd),
                "time_exit_days": max_hold,
                "time_exit_profit": _pct(time_exit_min_pnl) if time_exit_min_pnl is not None else 0.01,
                "time_force_days": force_hold,
                "first_day_exit_min_profit": fd_min_profit,
                "first_day_exit_days": fd_days,
                "breakeven_threshold_pct": be_thresh,
                "breakeven_stop_pnl_pct": be_stop,
            }
            pct_pos = _MirrorPos(entry_price, highest, staged_done)
            bar_dict = {"open": float(row.get("open", price_c)),
                        "high": price_h, "low": price_l, "close": price_c}
            ctx = exit_rule_engine.build_context(
                pct_pos, bar_dict, hold_days, ctx_params,
                use_high_for_tp=True, first_day_hold_value=1
            )

            signal = exit_rule_engine.check(ctx)
            if signal is None:
                continue

            reason = signal.reason

            # TP → 处理部分卖出 + sell_all
            if reason.startswith("TP"):
                idx = int(reason[2]) - 1
                staged_done.add(idx)
                s_idx = idx
                for si, stage in enumerate(active_tp_plan):
                    if si == s_idx:
                        sell_ratio = remaining_ratio if stage.get("sell_all") else stage.get("sell_ratio", 0.0)
                        actual_sell = min(sell_ratio, remaining_ratio)
                        if actual_sell > 0:
                            # P0-1: 原 `realized_pnl += tp_pct * actual_sell` 单位错(tp_pct小数 vs
                            # realized_pnl百分比口径)且是名义档位收益。改为按真实成交价算百分比收益，
                            # 与下方 close_pnl(950)/pnl(965) 口径一致。
                            tp_realized_pct = (signal.sell_price / entry_price - 1) * 100
                            realized_pnl += tp_realized_pct * actual_sell
                            sell_events.append({"type": "sell", "date": str(d),
                                                "price": signal.sell_price,
                                                "ratio": actual_sell,
                                                "reason": stage.get("label", reason)})
                            remaining_ratio -= actual_sell
                        if remaining_ratio <= 0:
                            exit_price = signal.sell_price
                            exit_date = d
                            break
                if remaining_ratio <= 0:
                    break
            else:
                # 非TP → 全卖
                realized_pnl += close_pnl * remaining_ratio
                sell_events.append({"type": "sell", "date": str(d),
                                    "price": signal.sell_price,
                                    "ratio": remaining_ratio,
                                    "reason": reason})
                remaining_ratio = 0
                exit_price = signal.sell_price
                exit_date = d
                break

        if remaining_ratio > 0:
            last = bars_daily.iloc[-1]
            exit_price = float(last["close"])
            exit_date = last["date"]
            pnl = (exit_price / entry_price - 1) * 100
            realized_pnl += pnl * remaining_ratio
            sell_events.append({"type": "sell", "date": str(exit_date), "price": exit_price,
                                "ratio": remaining_ratio, "reason": "清仓"})

        return self._wrap_result(code, stock_name, entry_price, exit_price,
                                 signal_date, exit_date, hold_days, realized_pnl, sell_events)

    def _wrap_result(self, code, name, entry, exit_p, b_date, e_date, days, pnl, events):
        reasons = [e['reason'].split('(')[0] for e in events if e['type'] == 'sell']
        b_str = str(b_date)
        e_str = str(e_date)
        if ' ' in b_str:
            bd, bt = b_str.split(' ', 1)
        else:
            bd, bt = b_str, '09:30'
        if ' ' in e_str:
            ed, et = e_str.split(' ', 1)
        else:
            ed, et = e_str, '15:00'
        return {
            "code": code, "name": name, "entry_price": entry, "exit_price": exit_p,
            "buy_date": bd, "buy_time": bt, "sell_date": ed, "sell_time": et,
            "hold_days": days,
            "pnl_pct": pnl, "ret_pct": pnl, "exit_reason": "+".join(sorted(list(set(reasons)))), "sell_events": events,
        }

backtest_engine = BacktestEngine()
