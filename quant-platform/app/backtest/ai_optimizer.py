"""
AI 参数优化引擎 (ai_optimizer.py)
完整的端到端优化流水线：
  冷启动(LLM) → 拉丁超立方探索 → LLM分析 → 贝叶斯精化(Optuna) → WFO验证 → LLM报告

核心性能优化：信号只生成一次，所有 trial 复用缓存，大幅降低运行时间。
"""

import sys
import os
import time
import threading
import traceback
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db
from app.screener.engine import load_strategy
from app.backtest.simulate_one_trade import simulate_one_trade
from app.backtest.regime_detector import regime_detector
from app.backtest.llm_advisor import LLMAdvisor

log = get_logger("AIOptimizer")

# ────────────────────────────────────────────────────────
# 全局任务状态管理（供 API 轮询）
# ────────────────────────────────────────────────────────
_task_state = {
    "running": False,
    "phase": "idle",           # idle/loading/exploring/refining/wfo/reporting/done/error
    "phase_detail": "",
    "trial_current": 0,
    "trial_total": 0,
    "results": [],             # 所有 trial 结果（实时更新）
    "top10": [],               # 最终 Top-10
    "wfo_results": [],
    "llm_report": "",
    "best_params": None,
    "error": None,
}
_stop_flag = False


def get_task_state() -> dict:
    return dict(_task_state)


def stop_optimization():
    global _stop_flag
    _stop_flag = True
    log.info("AIOptimizer | 收到停止指令")


def _update_state(**kwargs):
    _task_state.update(kwargs)


# ────────────────────────────────────────────────────────
# 工具函数
# ────────────────────────────────────────────────────────

def _calmar_score(trades: list, min_trades: int = 8, dd_penalty: float = 0.1) -> float:
    """风险调整收益评分（§3.3 增强）。

    = mean(pnl) - 0.5*std(pnl) - dd_penalty*max_drawdown
    - 波动率惩罚(0.5*std): 避免"高均值高方差"过拟合
    - 回撤惩罚(dd_penalty*max_dd): 避免选出"高收益但深回撤"的脆弱参数
    - 最小交易数门槛: n<min_trades 重罚(-999), 防小样本偶然高分过拟合
    系数为经验值, 可后续提到 config。
    """
    if len(trades) < min_trades:
        return -999.0
    pnls = np.array([t["pnl_pct"] for t in trades], dtype=float)
    if len(pnls) < 2:
        return float(np.mean(pnls))
    mean = np.mean(pnls)
    std = np.std(pnls, ddof=1)
    max_dd = _compute_max_drawdown(trades)
    vol_term = 0.5 * std if std >= 1e-6 else 0.0
    return float(mean - vol_term - dd_penalty * max_dd)


def _profit_factor(trades: list) -> float:
    """盈利因子：总盈利 / |总亏损|"""
    if not trades:
        return 0.0
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    if not losses or sum(losses) == 0:
        return 99.0 if wins else 0.0  # §3.3: 全胜无亏损用大常数代替 inf(JSON安全/前端友好)
    return round(abs(sum(wins) / sum(losses)), 4)


def _compute_max_drawdown(trades: list) -> float:
    """真实净值回撤率：模拟复利净值曲线，最大峰谷跌幅百分比"""
    if not trades:
        return 0.0
    portfolio = 1.0; nav_peak = 1.0; max_dd = 0.0
    for t in trades:
        portfolio *= (1 + t["pnl_pct"] / 100)
        if portfolio > nav_peak:
            nav_peak = portfolio
        dd = (nav_peak - portfolio) / nav_peak * 100
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _summarize_result(params: dict, trades: list) -> dict:
    """将一次回测结果打包成标准摘要 dict"""
    if not trades:
        return {"params": params, "score": -999, "avg_pnl": 0, "win_rate": 0,
                "max_dd": 0, "n_trades": 0, "profit_factor": 0, "total_return": 0}
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gain_sum = float(sum(wins)) if wins else 0
    loss_sum = float(sum(losses)) if losses else 0
    return {
        "params": params,
        "score":   round(_calmar_score(trades), 4),
        "avg_pnl": round(float(np.mean(pnls)), 3),
        "win_rate":round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
        "max_dd":  round(_compute_max_drawdown(trades), 3),
        "n_trades":len(trades),
        "pnl_std": round(float(np.std(pnls)), 3),
        "profit_factor": round(abs(gain_sum / loss_sum), 2) if loss_sum != 0 else 99.0,
        "total_return": round(float(sum(pnls)), 2),
    }


def _safe_float(v, default=0.0):
    """容错版 float：字符串/None/NaN 都不会崩，TDX 模式下 score/wfe 可能缺失"""
    try:
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def _ai_params_to_tdx_params(ai_params: dict, base_params: dict) -> dict:
    """AI 优化参数(百分比格式) → run_tdx_backtest 的 params(小数 + tiers 结构)

    AI 优化器搜索空间用百分比命名 (hard_stop_loss_pct=-7.0)，
    但 run_tdx_backtest 内部 exit_rule_engine 读小数命名 (hard_stop=-0.07)。
    两套量纲/命名不一致，不转换会导致所有 trial fallback 默认值 → AI 优化空转。
    转换逻辑参照 api/backtest.py 的 ai/apply mapping (反向)。
    """
    p = dict(base_params)  # 含 start_date/end_date/strategy_name/intraday_freq/资金仓位
    if "hard_stop_loss_pct" in ai_params:
        p["hard_stop"] = ai_params["hard_stop_loss_pct"] / 100.0
    if "trailing_activate_pct" in ai_params:
        p["trail_activate"] = ai_params["trailing_activate_pct"] / 100.0
    if "trailing_drawdown_pct" in ai_params:
        p["trail_dd"] = ai_params["trailing_drawdown_pct"] / 100.0
    if "time_exit_days" in ai_params:
        p["time_exit_days"] = int(ai_params["time_exit_days"])
    # breakeven_* exit_rules 内部 _pct() 自动转，直接透传
    for k in ("breakeven_threshold_pct", "breakeven_stop_pnl_pct"):
        if k in ai_params:
            p[k] = ai_params[k]
    # 多档止盈：tp1/tp2/tp3 → take_profit_tiers(小数)
    if "tp1_profit" in ai_params and "tp2_profit" in ai_params:
        tiers = [
            {"profit_pct": ai_params["tp1_profit"] / 100.0,
             "sell_ratio": ai_params.get("tp1_ratio", 0.33)},
            {"profit_pct": ai_params["tp2_profit"] / 100.0,
             "sell_ratio": ai_params.get("tp2_ratio", 0.33)},
        ]
        if "tp3_profit" in ai_params:
            tiers.append({"profit_pct": ai_params["tp3_profit"] / 100.0,
                          "sell_ratio": ai_params.get("tp3_ratio", 0.34)})
        p["take_profit_tiers"] = tiers
    return p


def _lhs_sample(search_space: dict, n: int, seed: int = None) -> List[dict]:
    """
    拉丁超立方采样：确保 n 组参数在每个维度上都均匀分布，
    避免聚集在某一角落。

    §3.3(寻优-M1): 用局部 default_rng(seed) 替代 np.random.seed(42)。
    - 不再污染进程全局 numpy 随机状态(原 seed(42) 影响同进程其它模块)
    - seed=None 时每次探索点不同(真探索); 传固定 seed 可复现
    """
    rng = np.random.default_rng(seed)
    from app.backtest.llm_advisor import FALLBACK_SEARCH_SPACE
    keys = list(search_space.keys())
    samples = []
    for _ in range(n):
        samples.append({})

    for key in keys:
        bounds = search_space[key]
        if not isinstance(bounds, dict):
            bounds = FALLBACK_SEARCH_SPACE.get(key, {"min": 0, "max": 1})
        lo = float(bounds.get("min", bounds.get("low", 0)))
        hi = float(bounds.get("max", bounds.get("high", 1)))
        if lo >= hi:
            lo, hi = hi - 1e-6, hi
        is_int = isinstance(bounds.get("min"), int) and isinstance(bounds.get("max"), int)
        intervals = np.linspace(lo, hi, n + 1)
        vals = [rng.uniform(intervals[i], intervals[i + 1]) for i in range(n)]
        rng.shuffle(vals)
        # §3.3: step 量化(若提供且合法)。step >= 区间宽度时忽略(防退化成单点)。
        step = bounds.get("step")
        valid_step = isinstance(step, (int, float)) and step > 0 and step < (hi - lo)
        for i, v in enumerate(vals):
            if is_int:
                samples[i][key] = int(round(v))
            elif valid_step:
                samples[i][key] = round(lo + round((v - lo) / step) * step, 4)
            else:
                samples[i][key] = round(v, 4)

    return samples


# ────────────────────────────────────────────────────────
# 主优化器
# ────────────────────────────────────────────────────────

class AIBacktestOptimizer:
    """
    AI 参数优化引擎主类。
    调用 run() 后完整执行以下阶段：
      Phase 1: 数据准备 + 信号缓存
      Phase 2: LLM 冷启动搜索空间设计
      Phase 3: 拉丁超立方探索（12 组）
      Phase 4: LLM 分析精化搜索空间
      Phase 5: Optuna 贝叶斯精化
      Phase 6: Walk-Forward 验证
      Phase 7: LLM 最终报告
    """

    def __init__(self, use_llm: bool = True, n_exploration: int = 12, n_bayesian: int = 50,
                 strategy_type: str = "python"):
        self.use_llm = use_llm
        self.n_exploration = n_exploration
        self.n_bayesian = n_bayesian
        self.strategy_type = strategy_type   # "python" | "tdx"
        self._formula_name = None            # TDX 模式下的通达信公式名
        self.llm = LLMAdvisor(use_llm=use_llm, timeout=60)

        # 信号缓存（跨所有 trial 复用）
        self._cached_signals: Optional[pd.DataFrame] = None
        self._cached_bars: Optional[pd.DataFrame] = None
        self._code_to_name: dict = {}

    # ── Phase 1: 数据准备 ──────────────────────────────────
    def _prepare_data(
        self,
        strategy_name: str,
        strategy_params: dict,
        start: date,
        end: date,
        exchanges: list,
        sectors: list,
        index_filter: list,
        min_mv: float,
        max_mv: float,
        log_cb: Callable,
        use_hot_concept: bool = False,
        hot_concept_top_n: int = 5,
    ) -> bool:
        # ── TDX 模式：跳过 parquet/load_strategy，信号与数据由 run_tdx_backtest 内部处理 ──
        if self.strategy_type == "tdx":
            log_cb(f"🔄 [Phase 1] TDX 模式：公式 {self._formula_name} 预跑校验（此步骤只需执行一次）...")
            self._code_to_name = {}
            try:
                probe = self._run_trial_tdx({}, start, end)  # 用默认参数预跑
            except Exception as e:
                log_cb(f"❌ [Phase 1] TDX 预跑失败: {e}")
                log.error(traceback.format_exc())
                return False
            if not probe:
                log_cb(f"⚠️ 公式 {self._formula_name} 在 {start} ~ {end} 区间内无信号/交易")
                return False
            log_cb(f"✅ [Phase 1] TDX 预跑成功：{len(probe)} 笔交易（公式 {self._formula_name}）")
            return True

        log_cb("🔄 [Phase 1] 加载日线数据 + 生成策略信号（此步骤只需执行一次）...")
        try:
            use_filtered = bool(index_filter or min_mv is not None or max_mv is not None)
            if use_filtered:
                stocks = db.get_stocks_filtered(index_filter=index_filter, min_mv=min_mv, max_mv=max_mv)
            else:
                stocks = db.get_all_stocks()
            if exchanges:
                stocks = stocks[stocks["exchange"].isin(exchanges)]
            if sectors:
                def _match(v):
                    v = str(v)
                    return any(s in v for s in sectors)
                stocks = stocks[stocks["sector"].apply(_match)]

            codes = stocks["code"].tolist()
            self._code_to_name = dict(zip(stocks["code"], stocks["name"]))
            if not codes:
                log_cb("❌ 无符合条件的股票")
                return False

            # ── 直接读 Parquet 文件，绕过 DuckDB 锁竞争 ─────────────
            PARQUET_DIR = ROOT_DIR / "data" / "parquet" / "daily"
            load_start = start - timedelta(days=365)

            frames = []
            missing = 0
            for code in codes:
                p = PARQUET_DIR / f"{code}.parquet"
                if not p.exists():
                    missing += 1
                    continue
                try:
                    df = pd.read_parquet(p)
                    df["code"] = code
                    # 日期过滤
                    date_col = "date"
                    df[date_col] = pd.to_datetime(df[date_col]).dt.date
                    df = df[(df[date_col] >= load_start) & (df[date_col] <= end)]
                    if not df.empty:
                        frames.append(df)
                except Exception:
                    missing += 1
                    continue

            if not frames:
                log_cb(f"❌ 无日线数据（检查 {PARQUET_DIR} 下是否有 Parquet 文件）")
                return False

            bars = pd.concat(frames, ignore_index=True)
            log_cb(f"  读取 Parquet 完成：{len(frames)} 只股票 / {missing} 只缺失 / {len(bars):,} 条记录")

            for col in ["open", "high", "low", "close", "volume"]:
                if col in bars.columns:
                    bars[col] = pd.to_numeric(bars[col], errors="coerce")
            bars = bars.dropna(subset=["close"])
            bars["date"] = pd.to_datetime(bars["date"]).dt.date
            bars = bars.sort_values(["code", "date"])
            self._cached_bars = bars

            strategy = load_strategy(strategy_name, params=strategy_params)
            signals = strategy.generate_signals(bars)
            if signals is not None and not signals.empty and "buy_signal" in signals.columns:
                signals = signals[signals["buy_signal"] == True]
            if signals is None or signals.empty:
                log_cb("⚠️ 策略在整个历史期间未产生任何信号")
                return False

            signals["date"] = pd.to_datetime(signals["date"]).dt.date
            signals = signals[(signals["date"] >= start) & (signals["date"] <= end)]
            if signals.empty:
                log_cb("⚠️ 回测期间（精确截取后）无信号")
                return False

            self._cached_signals = signals
            log_cb(f"✅ [Phase 1] 完成：{len(frames)} 只日线 / {len(signals)} 信号已缓存")

            # 热门概念过滤
            if use_hot_concept and not signals.empty:
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
                        self._cached_signals = signals
                        log_cb(f"🔥 热门概念过滤 (Top{hot_concept_top_n}): {n_before} → {len(signals)} 条信号")
                except Exception as e:
                    log_cb(f"⚠️ 热门概念过滤失败: {e}，保留全部信号")

            # ── 加载 1 分钟线（先于信号索引，索引依赖 intraday 数据）─────
            self._load_intraday_data(start, end, codes, log_cb)

            # 预建信号索引（过滤无日内数据的信号）
            log_cb("🔧 预建信号索引...")
            self._build_numpy_index()
            log_cb(f"  索引就绪：{len(self._numpy_signal_tuples)} 有效信号")
            return True

        except Exception as e:
            log_cb(f"❌ [Phase 1] 数据准备失败: {e}")
            log.error(traceback.format_exc())
            return False

    # ── 单次 Trial 执行（1分钟线仿真，复用信号缓存）─────
    def _run_trial(self, params: dict, start: date, end: date) -> list:
        """
        用给定参数执行一次回测（仅使用 [start, end] 区间内的信号）。
        v4.0: 使用 1 分钟线逐 bar 仿真。
        TDX 模式: 提前分流到 _run_trial_tdx，复用 run_tdx_backtest 全套仿真。
        """
        if self.strategy_type == "tdx":
            return self._run_trial_tdx(params, start, end)

        if self._cached_signals is None:
            return []

        if not hasattr(self, '_numpy_signal_tuples') or not self._numpy_signal_tuples:
            self._build_numpy_index()

        all_trades = []
        for code, entry, sig_date in self._numpy_signal_tuples:
            if _stop_flag:
                break
            sd = sig_date.date() if hasattr(sig_date, 'date') else sig_date
            if sd < start or sd > end:
                continue

            trade = self._fast_simulate(code, entry, sd, params, end_date=end)
            if trade:
                all_trades.append(trade)

        return all_trades

    def _run_trial_tdx(self, params: dict, start: date, end: date) -> list:
        """TDX 模式单 trial：调 run_tdx_backtest，从 trades_json 抽 pnl。

        信号源、数据加载、止盈止损仿真(5m/日线降级、T+1、涨跌停)全部复用
        run_tdx_backtest 的现有逻辑，保证与 TDX 单次回测结果一致。
        """
        from app.backtest.tdx_runner import run_tdx_backtest

        base = {
            "start_date": start, "end_date": end,
            "strategy_name": self._formula_name,       # 公式名（关键，传给 TdxBridge）
            "intraday_freq": self._intraday_freq or "daily",
            "initial_capital": self._initial_capital or 1000000,
            "position_size": self._position_size or 50000,
        }
        tdx_params = _ai_params_to_tdx_params(params, base)
        try:
            result = run_tdx_backtest(tdx_params, stop_event=None,
                                      stock_names=self._code_to_name)
        except Exception as e:
            log.warning(f"TDX trial 执行失败: {e}")
            return []

        if not result or result.get("status") not in ("ok", None):
            return []

        trades = []
        for t in result.get("trades", []):
            try:
                trades.append({
                    "code": t["code"], "name": t.get("name", ""),
                    "entry_price": t["entry_px"], "exit_price": t["exit_px"],
                    "hold_days": t.get("hold_days", 0),
                    "pnl_pct": t["ret_pct"],          # tdx_runner 已算好，直接用
                    "buy_date": t["entry_date"],
                })
            except (KeyError, TypeError):
                continue
        return trades

    def _build_numpy_index(self):
        """从 _cached_bars 构建信号列表，一次构建所有 trial 复用。
        仅构建信号索引，不再构建日线价格数组（仿真用 1 分钟线）。"""
        signals = self._cached_signals

        # signal tuples: (code, entry_price, signal_date)
        dates_arr = signals["date"].values
        codes_arr = signals["code"].values
        closes_arr = signals["close"].values.astype(np.float64)
        self._numpy_signal_tuples = []
        for i in range(len(signals)):
            d = dates_arr[i]
            code = codes_arr[i]
            # 信号必须在日内数据中可用才加入
            if code in self._intraday_pos and d in self._intraday_pos[code]:
                self._numpy_signal_tuples.append((code, float(closes_arr[i]), d))

    def _load_intraday_data(self, start, end, codes, log_cb):
        """加载 1 分钟线数据用于精确日内仿真。
        构建 OHLC intraday 数组，按 signal_date 索引。"""
        PARQUET_DIR = ROOT_DIR / "data" / "parquet" / "min1"
        self._intraday_closes = {}   # code -> np.array of 1-min close
        self._intraday_highs = {}    # code -> np.array of 1-min high
        self._intraday_lows = {}     # code -> np.array of 1-min low
        self._intraday_dates = {}    # code -> np.array of date for each bar
        self._intraday_pos = {}      # code -> {date: first_bar_index}

        loaded = 0
        for code in codes:
            p = PARQUET_DIR / f"{code}.parquet"
            if not p.exists():
                continue
            try:
                df = pd.read_parquet(p)
                df["datetime"] = pd.to_datetime(df["datetime"])
                df = df[(df["datetime"] >= pd.Timestamp(start)) &
                        (df["datetime"] <= pd.Timestamp(end) + pd.Timedelta(days=60))]
                if df.empty:
                    continue
                df = df.sort_values("datetime")
                df["date"] = df["datetime"].dt.date
                self._intraday_closes[code] = df["close"].values.astype(np.float64)
                self._intraday_highs[code] = df["high"].values.astype(np.float64)
                self._intraday_lows[code] = df["low"].values.astype(np.float64)
                self._intraday_dates[code] = df["date"].values
                date_to_idx = {}
                for i, d in enumerate(df["date"].values):
                    if d not in date_to_idx:
                        date_to_idx[d] = i
                self._intraday_pos[code] = date_to_idx
                loaded += 1
            except Exception:
                continue

        log_cb(f"  1分钟线加载：{loaded} 只股票（覆盖 {start} ~ {end}）")

    # ── 核心仿真（1分钟线逐bar迭代，OHLC感知）───
    def _fast_simulate(self, code: str, entry: float, sig_date,
                       params: dict, end_date=None) -> Optional[dict]:
        """[v4.1 委托] 1 分钟线聚为日线 OHLC → 调 simulate_one_trade kernel。

        旧影子(假默认 -7/15、TP 按真实档位成交、fake cost、缺 trailing_first/stack)
        → 忠实 kernel。intraday 分钟归并为日,丢 1 天内分钟级触发时机(对参数寻优无影响)。
        返回 dict 仍带 pnl_pct(由 kernel return_pct 映射)以兼容 optimizer 读 trade["pnl_pct"]。
        """
        closes = self._intraday_closes.get(code)
        highs = self._intraday_highs.get(code)
        lows = self._intraday_lows.get(code)
        dates = self._intraday_dates.get(code)
        date_pos = self._intraday_pos.get(code)
        if closes is None or date_pos is None:
            return None
        start_idx = date_pos.get(sig_date)
        if start_idx is None:
            for d in sorted(date_pos.keys()):
                if d >= sig_date:
                    start_idx = date_pos[d]
                    break
        if start_idx is None:
            return None
        end_idx = len(closes)
        if end_date is not None:
            for i in range(start_idx, len(dates)):
                if dates[i] >= end_date:
                    end_idx = i
                    break
        # 聚合 1 分钟 → 日线(high=max, low=min, open=first, close=last)
        rows = [{"date": dates[i], "open": closes[i], "high": highs[i],
                 "low": lows[i], "close": closes[i]}
                for i in range(start_idx, end_idx)]
        if not rows:
            return None
        import pandas as pd
        df = pd.DataFrame(rows)
        daily = (df.assign(_d=pd.to_datetime(df["date"]).dt.date)
                  .groupby("_d")
                  .agg(open=("open", "first"), high=("high", "max"),
                       low=("low", "min"), close=("close", "last"))
                  .reset_index()
                  .rename(columns={"_d": "date"}))
        # 委托 kernel;映射 pnl_pct 兼容 optimizer(读 trade["pnl_pct"])
        from app.backtest.simulate_one_trade import simulate_one_trade
        result = simulate_one_trade(
            code=code, stock_name=code, entry_price=entry,
            signal_date=sig_date, bars_daily=daily, params_override=params,
        )
        if result is None:
            return None
        return {
            "code": code,
            "name": self._code_to_name.get(code, code),
            "entry_price": entry,
            "exit_price": result["exit_price"],
            "hold_days": result["hold_days"],
            "pnl_pct": result["return_pct"],
            "buy_date": str(sig_date),
        }

    def _default_search_space() -> dict:
        # 以系统配置的 risk 参数为基线，范围 ≤ 2×
        tp1 = settings.get("risk", "take_profit_tiers", default=[])[0]["profit_pct"] if settings.get("risk", "take_profit_tiers", default=[]) else 3.0
        tp2 = settings.get("risk", "take_profit_tiers", default=[])[1]["profit_pct"] if len(settings.get("risk", "take_profit_tiers", default=[])) >= 2 else 5.0
        return {
            "tp1_profit":              {"min": max(0.5, tp1 * 0.3), "max": tp1 * 2.0, "step": 0.5},
            "tp2_profit":              {"min": max(1.0, tp2 * 0.5), "max": tp2 * 2.0, "step": 0.5},
            "tp1_ratio":               {"min": 0.10, "max": 0.50, "step": 0.05},
            "tp2_ratio":               {"min": 0.10, "max": 0.50, "step": 0.05},
            "hard_stop_loss_pct":      {"min": -12.0, "max": -3.0, "step": 0.5},
            "trailing_activate_pct":   {"min": 3.0, "max": 12.0, "step": 0.5},
            "trailing_drawdown_pct":   {"min": 1.0, "max": 6.0, "step": 0.5},
            "breakeven_threshold_pct": {"min": 2.0, "max": 10.0, "step": 0.5},
            "breakeven_stop_pnl_pct":  {"min": 0.0, "max": 3.0, "step": 0.5},
            "time_exit_days":          {"min": 3, "max": 10, "step": 1},
        }

    # ── 降级报告（无 LLM 时）──────────
    @staticmethod
    def _generate_basic_report(top10: list, wfo_results: list, regime_summary: str, n_total: int) -> str:
        lines = [
            "# AI 参数优化报告",
            "",
            f"## 概览",
            f"- 总探索次数: {n_total}",
            f"- 最优平盈率: {_safe_float(top10[0].get('avg_pnl'),0):+.2f}%" if top10 else "- 无有效结果",
            f"- 最优胜率: {_safe_float(top10[0].get('win_rate'),0):.1f}%" if top10 else "",
            f"- 最优 PF: {top10[0].get('profit_factor', 'N/A')}" if top10 else "",
            "",
            "## Top-5 参数组合",
            "",
        ]
        for i, r in enumerate(top10[:5]):
            wfe = r.get("wfe", "N/A")
            status = r.get("wfe_status", "")
            lines.append(f"### #{i+1} | 平盈率: {_safe_float(r.get('avg_pnl'),0):+.2f}% | 胜率: {_safe_float(r.get('win_rate'),0):.1f}% | WFE: {wfe} {status}")
            lines.append(f"```")
            for k, v in r.get("params", {}).items():
                lines.append(f"  {k}: {v}")
            lines.append(f"```")
            lines.append("")

        lines.append("## WFO 验证")
        for w in wfo_results:
            lines.append(f"- #{w.get('rank')} WFE={w.get('wfe')} {w.get('wfe_status','')}")
        lines.append("")
        lines.append(f"## 市场状态\n{regime_summary}")
        return "\n".join(lines)

    # ── Phase 3: 拉丁超立方探索 ───────────────────────────
    def _run_exploration(
        self,
        search_space: dict,
        start: date,
        end: date,
        log_cb: Callable,
    ) -> List[dict]:
        log_cb(f"🔍 [Phase 3] 探索期：{self.n_exploration} 组拉丁超立方采样...")
        candidates = _lhs_sample(search_space, self.n_exploration)
        results = []
        for i, params in enumerate(candidates):
            if _stop_flag:
                break
            trades = self._run_trial(params, start, end)
            summary = _summarize_result(params, trades)
            results.append(summary)
            _task_state["results"].append(summary)
            _task_state["trial_current"] = i + 1
            pnl = summary["avg_pnl"]
            score = summary["score"]
            log_cb(
                f"  探索 {i+1:02d}/{self.n_exploration} | "
                f"score={_safe_float(score,0):.3f} | 平盈率={_safe_float(pnl,0):+.2f}% | 胜率={_safe_float(summary['win_rate'],0):.1f}% | "
                f"PF={summary.get('profit_factor', 0):.2f}"
            )
        return results

    # ── Phase 5: Optuna 贝叶斯优化 ────────────────────────
    def _run_bayesian(
        self,
        search_space: dict,
        start: date,
        end: date,
        log_cb: Callable,
        train_end: date = None,
        valid_end: date = None,
    ) -> List[dict]:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)

        log_cb(f"🤖 [Phase 5] 贝叶斯精化：{self.n_bayesian} 次 Optuna 探索...")
        results = []
        trial_count = [0]

        has_split = train_end is not None and valid_end is not None and valid_end > train_end
        if has_split:
            log_cb(f"📅 样本分割: train(start~{train_end}) valid({train_end + timedelta(days=1)}~{valid_end}) test({valid_end + timedelta(days=1)}~{end})")

        def objective(trial):
            if _stop_flag:
                raise optuna.exceptions.TrialPruned()

            from app.backtest.llm_advisor import FALLBACK_SEARCH_SPACE
            params = {}
            for key, bounds in search_space.items():
                if not isinstance(bounds, dict):
                    bounds = FALLBACK_SEARCH_SPACE.get(key, {"min": 0, "max": 1})
                lo = float(bounds.get("min", bounds.get("low", 0)))
                hi = float(bounds.get("max", bounds.get("high", 1)))
                if lo >= hi:
                    hi = lo + 1e-6
                is_int = isinstance(bounds.get("min"), int) and isinstance(bounds.get("max"), int)
                if is_int:
                    params[key] = trial.suggest_int(key, int(lo), int(hi))
                else:
                    # §3.3: step 量化(合法时)。step>=区间宽度则忽略(防退化)。
                    step = bounds.get("step")
                    if isinstance(step, (int, float)) and 0 < step < (hi - lo):
                        params[key] = trial.suggest_float(key, lo, hi, step=step)
                    else:
                        params[key] = trial.suggest_float(key, lo, hi)

            # 约束优化：如果 LLM 给出的空间范围异常，进行强制修正/剪枝
            tp1 = params.get("tp1_profit", 5.0)
            tp2 = params.get("tp2_profit", 10.0)
            tp3 = params.get("tp3_profit", 20.0)
            if tp2 <= tp1:
                params["tp2_profit"] = tp1 + 2.0
            if tp3 <= tp2:
                params["tp3_profit"] = tp2 + 3.0

            trades = self._run_trial(params, start, end)
            summary = _summarize_result(params, trades)

            # J3/C3-3: 计算 valid_score(验证集 Calmar) 和 test_score
            if has_split:
                valid_trades = self._run_trial(params, train_end + timedelta(days=1), valid_end)
                summary["valid_score"] = round(_calmar_score(valid_trades), 4)
                test_trades = self._run_trial(params, valid_end + timedelta(days=1), end)
                summary["test_score"] = round(_calmar_score(test_trades), 4)
            else:
                summary["valid_score"] = summary["score"]
                summary["test_score"] = summary["score"]

            results.append(summary)
            _task_state["results"].append(summary)

            trial_count[0] += 1
            _task_state["trial_current"] = self.n_exploration + trial_count[0]

            if trial_count[0] % 10 == 0 or trial_count[0] == 1:
                log_cb(
                    f"  Optuna {trial_count[0]:02d}/{self.n_bayesian} | "
                    f"当前最优平盈率: {max((r['avg_pnl'] for r in results), default=0):+.2f}%"
                )

            # 任务二(寻优-C1): Optuna 在验证集上爬山，而非全样本 IS score。
            # 防止贝叶斯优化器对全样本(含valid+test)过拟合。无分割时退回 score。
            return summary["valid_score"] if has_split else summary["score"]

        study = optuna.create_study(direction="maximize")
        study.optimize(objective, n_trials=self.n_bayesian, n_jobs=1)
        return results

    # ── Phase 6: Walk-Forward 验证 ────────────────────────
    def _run_wfo(
        self,
        top_params_list: list,
        start: date,
        end: date,
        log_cb: Callable,
        n_splits: int = 5,
    ) -> List[dict]:
        """
        Anchored Expanding Window WFO:
        - IS 从起点开始逐渐扩展
        - OOS 为不重叠的连续窗口
        - WFE = avg(OOS PnL) / avg(IS PnL)，取多折均值
        """
        total_days = (end - start).days
        min_days_needed = 180
        if total_days < min_days_needed:
            log_cb(f"⚠️ WFO | 回测区间仅 {total_days} 天（需≥{min_days_needed}天），跳过验证")
            return [{"rank": i+1, "wfe": "N/A（数据不足）", "oos_pnl": None}
                    for i in range(len(top_params_list))]

        # 每折 OOS 窗口 = total / (n_splits + 2)，确保初始 IS ≥ 3×OOS
        oos_days = total_days // (n_splits + 2)
        initial_is_days = oos_days * 3
        min_oos_days = 30  # OOS 窗口至少 30 天才有统计意义

        log_cb(f"📊 [Phase 6] Walk-Forward 验证（anchored expanding window, {n_splits} 折, "
               f"OOS≈{oos_days}d, 初始IS≈{initial_is_days}d）...")

        wfo_results = []

        for rank, params in enumerate(top_params_list[:5]):
            is_pnls, oos_pnls = [], []
            valid_splits = 0

            for split in range(n_splits):
                is_start = start
                is_end = start + timedelta(days=initial_is_days + split * oos_days - 1)
                oos_start = is_end + timedelta(days=1)
                oos_end = min(oos_start + timedelta(days=oos_days - 1), end)

                oos_actual_days = (oos_end - oos_start).days
                if oos_start >= end or oos_actual_days < min_oos_days:
                    break

                is_trades  = self._run_trial(params, is_start, is_end)
                oos_trades = self._run_trial(params, oos_start, oos_end)

                if is_trades and oos_trades:
                    is_avg  = np.mean([t["pnl_pct"] for t in is_trades])
                    oos_avg = np.mean([t["pnl_pct"] for t in oos_trades])
                    is_pnls.append(is_avg)
                    oos_pnls.append(oos_avg)
                    valid_splits += 1
                    log_cb(f"  WFO #{rank+1}.{split+1}: IS={is_avg:+.2f}% OOS={oos_avg:+.2f}% "
                           f"({oos_start}~{oos_end}, {oos_actual_days}d, {len(oos_trades)}笔)")

            if valid_splits >= 2 and is_pnls and oos_pnls:
                avg_is  = float(np.mean(is_pnls))
                avg_oos = float(np.mean(oos_pnls))
                wfe = round(avg_oos / avg_is, 3) if avg_is != 0 else 0

                # 计算 WFE 稳定性（各折 WFE 的标准差）
                fold_wfes = [oos_pnls[i] / is_pnls[i] if is_pnls[i] != 0 else 0
                            for i in range(valid_splits)]
                wfe_std = float(np.std(fold_wfes)) if len(fold_wfes) > 1 else 0

                if wfe >= 0.7 and wfe_std < 0.3:
                    status = "✅ 稳健"
                elif wfe >= 0.5 and wfe_std < 0.5:
                    status = "⚠️ 一般"
                elif wfe >= 0.3:
                    status = "⚠️ 衰减"
                else:
                    status = "❌ 过拟合风险"

                wfo_results.append({
                    "rank": rank + 1,
                    "wfe": round(wfe, 3),
                    "wfe_std": round(wfe_std, 3),
                    "wfe_status": status,
                    "is_pnl": round(avg_is, 3),
                    "oos_pnl": round(avg_oos, 3),
                    "n_splits": valid_splits,
                })
                log_cb(f"  WFO #{rank+1}: WFE={_safe_float(wfe,0):.2f}±{_safe_float(wfe_std,0):.2f} {status} "
                       f"({valid_splits}折 | IS={_safe_float(avg_is,0):+.2f}% OOS={_safe_float(avg_oos,0):+.2f}%)")
            elif valid_splits == 1:
                # 只有 1 折也记录，但标记为参考
                wfe = round(oos_pnls[0] / is_pnls[0], 3) if is_pnls[0] != 0 else 0
                wfo_results.append({
                    "rank": rank + 1,
                    "wfe": round(wfe, 3),
                    "wfe_std": None,
                    "wfe_status": "⚠️ 仅1折（参考）",
                    "is_pnl": round(float(is_pnls[0]), 3),
                    "oos_pnl": round(float(oos_pnls[0]), 3),
                    "n_splits": 1,
                })
                log_cb(f"  WFO #{rank+1}: WFE={_safe_float(wfe,0):.2f} ⚠️ 仅1折（参考）")
            else:
                wfo_results.append({"rank": rank + 1, "wfe": "N/A", "wfe_status": "无有效折",
                                    "oos_pnl": None})

        return wfo_results

    # ── 主入口 ─────────────────────────────────────────────
    def run(
        self,
        strategy_name: str,
        strategy_params: dict,
        start: date,
        end: date,
        exchanges: list = None,
        sectors: list = None,
        index_filter: list = None,    # 指数成分过滤 e.g. ['HS300','ZZ500']
        min_mv: float = None,          # 流通市值下限（亿元）
        max_mv: float = None,          # 流通市值上限（亿元）
        log_callback: Callable = None,
        stop_event: threading.Event = None,
        initial_capital: float = None,
        position_size: float = None,
        use_portfolio: bool = None,
        streak_pause: int = None,
        pause_days: int = None,
        intraday_freq: str = None,
        params_override: dict = None,
        use_atr_stop: bool = False,
        atr_stop_multiplier: float = 2.5,
        use_hot_concept: bool = False,
        hot_concept_top_n: int = 5,
        strategy_type: str = None,
        formula_name: str = None,
    ):
        global _stop_flag
        _stop_flag = False
        _update_state(
            running=True, phase="loading", phase_detail="",
            trial_current=0, trial_total=self.n_exploration + self.n_bayesian,
            results=[], top10=[], wfo_results=[], llm_report="",
            best_params=None, error=None,
        )

        # 存储资金参数（供后续 Top-3 组合级验证使用）
        self._initial_capital = initial_capital
        self._position_size = position_size
        self._use_portfolio = use_portfolio
        self._streak_pause = streak_pause
        self._pause_days = pause_days
        self._intraday_freq = intraday_freq

        # TDX 模式：覆盖 strategy_type 并确定公式名（缺省回退到 __init__ 的值）
        if strategy_type:
            self.strategy_type = strategy_type
        if self.strategy_type == "tdx":
            self._formula_name = formula_name or strategy_name

        def log_cb(msg: str):
            log.info(msg)
            if log_callback:
                log_callback(msg)

        try:
            # ─── Phase 1: 数据准备 ───────────────────────
            _update_state(phase="loading")
            ok = self._prepare_data(
                strategy_name, strategy_params, start, end,
                exchanges, sectors, index_filter, min_mv, max_mv, log_cb,
                use_hot_concept=use_hot_concept,
                hot_concept_top_n=hot_concept_top_n,
            )
            if not ok or _stop_flag:
                _update_state(running=False, phase="error", error="数据加载失败")
                return

            # 计算市场 Regime
            log_cb("🌐 计算历史市场状态标签 (Regime)...")
            regime_map = regime_detector.compute_regime_map(start, end)

            # ─── Phase 2: 搜索空间设计 ─────────────────────
            _update_state(phase="search_space")
            search_space = settings.optimizer_search_space
            if search_space and len(search_space) >= 2:
                log_cb(f"📋 [Phase 2] 搜索空间从系统配置加载（{len(search_space)} 个参数）")
            else:
                # 降级：使用内置默认搜索空间
                log_cb("⚠️ [Phase 2] 系统配置中无搜索空间，使用内置默认值")
                search_space = self._default_search_space()
            log_cb(f"  搜索范围预览: " + ", ".join(
                f"{k}=[{v.get('min',0)},{v.get('max',1)}]" for k, v in list(search_space.items())[:5]))

            # ─── Phase 3: 拉丁超立方探索 ─────────────────
            _update_state(phase="exploring")
            exploration_results = self._run_exploration(search_space, start, end, log_cb)
            if _stop_flag:
                _update_state(running=False, phase="stopped")
                return

            # 计算 Regime 分布
            # TDX 模式：每次 _run_trial 都调 TdxBridge worker(开销大)，
            # 且信号源固定、regime 分析意义有限 → 跳过这轮重复回测，避免数十次额外 worker 调用
            all_trades_so_far = []
            if self.strategy_type != "tdx":
                for r in exploration_results:
                    trades = self._run_trial(r["params"], start, end)
                    all_trades_so_far.extend(trades)
            regime_summary = regime_detector.summarize_trades_by_regime(all_trades_so_far, regime_map)
            log_cb(f"📊 市场状态分析：{regime_summary}")

            # 任务二(寻优-C1): train/valid/test 按【自然交易日跨度】70/20/10 切分。
            # 原按信号日期序号(int(n*0.7))切分——信号时间分布不均时 valid/test 区间畸短、噪声大。
            # 改按 start~end 日历跨度线性切，保证三段时间长度稳定。test 严格隔离只做最终展示。
            train_end_d = end
            valid_end_d = end
            total_days = (end - start).days
            if total_days >= 30:
                train_end_d = start + timedelta(days=int(total_days * 0.7))
                valid_end_d = start + timedelta(days=int(total_days * 0.9))
                log_cb(f"📅 样本分割(自然日 {total_days}天): train(start~{train_end_d}) "
                       f"valid({train_end_d + timedelta(days=1)}~{valid_end_d}) "
                       f"test({valid_end_d + timedelta(days=1)}~{end}) [test严格隔离]")

            # 为探索结果补齐 valid_score / test_score
            if train_end_d < end:
                for r in exploration_results:
                    if "valid_score" not in r:
                        vt = self._run_trial(r["params"], train_end_d + timedelta(days=1), valid_end_d)
                        r["valid_score"] = round(_calmar_score(vt), 4)
                        tt = self._run_trial(r["params"], valid_end_d + timedelta(days=1), end)
                        r["test_score"] = round(_calmar_score(tt), 4)

            # ─── Phase 4: 精化搜索空间 ───────────────
            _update_state(phase="refine_space")

            if self.use_llm:
                log_cb("🤖 [Phase 4] LLM 分析探索结果，收窄搜索空间...")
                refined_space = self.llm.analyze_exploration(
                    exploration_results, regime_summary, search_space
                )
                log_cb("✅ [Phase 4] LLM 精化完成")
            else:
                log_cb("📋 [Phase 4] 跳过 LLM 精化，直接使用配置搜索空间")
                refined_space = search_space

            # ─── Phase 5: Optuna 贝叶斯精化 ──────────────
            _update_state(phase="refining")
            bayesian_results = self._run_bayesian(refined_space, start, end, log_cb, train_end_d, valid_end_d)
            if _stop_flag:
                _update_state(running=False, phase="stopped")
                return

            # 合并排序，取 Top-10
            # J3/C3-3: Top-10 排序使用 valid_score 替代全样本 score，防止 IS 过拟合
            all_results = exploration_results + bayesian_results
            all_results = sorted(
                all_results,
                key=lambda x: (x.get("valid_score", -999), x.get("wfe", 0) if isinstance(x.get("wfe"), (int, float)) else 0),
                reverse=True,
            )
            top10 = all_results[:10]
            # 容错：score/avg_pnl/wfe 字段在 TDX 模式或 WFO 跳过时可能缺失或为字符串
            _t0 = top10[0]
            _avg = _safe_float(_t0.get('avg_pnl'), 0)
            _sc = _safe_float(_t0.get('score'), -999)
            _vs = _t0.get('valid_score', 'N/A')
            if not isinstance(_vs, (int, float)):
                _vs = 'N/A'
            log_cb(f"🏆 Top-10 IS 平盈率: {_avg:+.2f}% "
                   f"(score={_sc:.3f} valid_score={_vs})")

            # ─── Phase 6: WFO 验证 ───────────────────────
            _update_state(phase="wfo")
            top_params = [r["params"] for r in top10[:5]]
            wfo_results = self._run_wfo(top_params, start, end, log_cb)
            # 将 WFO 结果合并回 top10
            for i, r in enumerate(top10[:5]):
                wfo = next((w for w in wfo_results if w["rank"] == i + 1), {})
                top10[i]["wfe"] = wfo.get("wfe", "N/A")
                top10[i]["wfe_status"] = wfo.get("wfe_status", "")
                top10[i]["oos_pnl"] = wfo.get("oos_pnl")

            # 任务二(寻优-C1): 用 (valid_score, wfe) 选 best_params，与 Top-10 排序口径一致。
            # 原用全样本 score 排序会抵消 valid_score 防过拟合的努力。
            # WFE 越接近 1 越好;缺失 WFE 的项(未做 WFO)按 0 处理
            def _sort_key(r):
                valid_score = r.get("valid_score", r.get("score", -999))
                wfe_raw = r.get("wfe", "N/A")
                wfe_val = float(wfe_raw) if isinstance(wfe_raw, (int, float)) else 0.0
                return (valid_score, wfe_val)

            top10 = sorted(top10, key=_sort_key, reverse=True)
            best_params = top10[0]["params"] if top10 else None
            _update_state(top10=top10, best_params=best_params)
            log_cb(f"🎯 WFE 选优后: 平盈率={_safe_float(top10[0].get('avg_pnl'),0):+.2f}% valid_score={_safe_float(top10[0].get('valid_score'),-999):.3f} score={_safe_float(top10[0].get('score'),-999):.3f} WFE={top10[0].get('wfe', 'N/A')}")
            _update_state(top10=top10, wfo_results=wfo_results)

            # 计算最终 Regime 摘要（基于全部回测）
            all_final_trades = self._run_trial(top10[0]["params"], start, end)
            final_regime = regime_detector.summarize_trades_by_regime(all_final_trades, regime_map)
            _update_state(regime_summary=final_regime)

            # ─── Phase 7: 最终报告 ───────────────────
            _update_state(phase="reporting")
            n_total = self.n_exploration + self.n_bayesian
            if self.use_llm:
                log_cb("🤖 [Phase 7] 生成 LLM 最终分析报告...")
                report = self.llm.generate_final_report(top10, wfo_results, final_regime, n_total)
            else:
                log_cb("📋 [Phase 7] 生成降级报告...")
                report = self._generate_basic_report(top10, wfo_results, final_regime, n_total)
            _update_state(llm_report=report)
            log_cb("✅ 分析报告已生成")
            log_cb(f"🎉 AI 回测优化完成！共 {n_total} 次探索，最优平盈率 {_safe_float(top10[0].get('avg_pnl'),0):+.2f}%")
            _update_state(running=False, phase="done")

        except Exception as e:
            err_msg = f"AI 优化引擎崩溃: {str(e)}"
            log.error(err_msg)
            log.error(traceback.format_exc())
            _update_state(running=False, phase="error", error=err_msg)


# 全局单例
_optimizer_instance: Optional[AIBacktestOptimizer] = None


def get_optimizer() -> AIBacktestOptimizer:
    global _optimizer_instance
    if _optimizer_instance is None:
        _optimizer_instance = AIBacktestOptimizer()
    return _optimizer_instance
