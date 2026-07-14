"""QUANTQQ 策略独立回测 v2 — 用系统实际参数 + 完整 9 规则

设计:
- 入口信号:QUANTQQ via TDX BRIDGE cache(已 commit)
- OHLCV:系统 parquet(2015-2026 完整)
- 退出规则:全部 9 个,从 app/backtest/exit_rules.py 复刻(不 import DAG)
- 参数:从 config/app_setting.json 读 risk+backtest 段(系统实际用)
- 优先级模式:trailing_first(TP > trail > 硬止损;同 bar TP 卖部分后再评估剩余)

设计依据 — 这是用系统实际"出场 + 进道"参数,公平对比:
- 硬止损 hard_stop 改 -4.6%(原假设 -6% 太紧)
- 多档止盈 [{2.7%, 20%}, {13%, 60%}](两档,叠加模式)
- 移动止盈激活 +3.9%,回撤 -1.7%
- 保本止损 breakeven:+2.23% 激活后,+0.98% 锁利(关键!上版本完全漏了)
- 时间 7 天(盈 2.5%) / 强制 12 天
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ════════════════════════════════════════════════════════════════
# 默认参数 — 从 config/app_setting.json 加载
# ════════════════════════════════════════════════════════════════

CONFIG_FILE = ROOT / "config" / "app_setting.json"


def load_default_params() -> dict:
    """从 app_setting.json 加载 risk + backtest 段参数。"""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    risk = cfg.get("risk", {}) or {}
    bt = cfg.get("backtest", {}) or {}
    # 三源(risk百分比 / backtest小数)统一为 RuleContext 用的小数
    return {
        "hard_stop": float(bt.get("hard_stop", -0.046)),
        "take_profit_tiers": list(bt.get("take_profit_tiers", [
            {"profit_pct": 0.027, "sell_ratio": 0.20},
            {"profit_pct": 0.13, "sell_ratio": 0.60},
        ])),
        "trail_activate": float(bt.get("trail_activate", 0.039)),
        "trail_dd": float(bt.get("trail_dd", 0.017)),
        "time_exit_days": int(bt.get("time_exit_days", 7)),
        "time_exit_profit": float(bt.get("time_exit_profit", 0.025)),
        "time_force_days": int(bt.get("time_force_days", 12)),
        "first_day_exit_min_profit": float(risk.get("first_day_exit_min_profit", 0.0)),
        "first_day_exit_days": int(risk.get("first_day_exit_days", 1)),
        "use_atr_trail": bool(risk.get("use_atr_stop", False)),
        "atr_trail_multiplier": float(risk.get("atr_stop_multiplier", 2.5)),
        "breakeven_threshold": float(risk.get("breakeven_threshold_pct", 2.23)) / 100.0,
        "breakeven_stop": float(risk.get("breakeven_stop_pnl_pct", 0.98)) / 100.0,
        "tp_stack_mode": True,
        "tp1_fill_pct": 0.03,  # VERA 对齐
        "priority_mode": "trailing_first",  # TP > trail > hard_stop
        "realistic_stop_fill": "stop",
        "commission_pct": 0.00025,  # 双边佣金(实际 A 股 ~万 2.5)
        "slippage_pct": 0.0005,        # 每笔滑点 0.05%/side(中小盘常见)
        "stamp_tax_pct": 0.001,         # 印花税 仅卖出 0.1%
        "max_position_cash": None,       # 单笔持仓上限($);None=不限制(随 cash 复利)
    }


# ════════════════════════════════════════════════════════════════
# 退出规则 — 全部 9 个,内嵌自 app/backtest/exit_rules.py
# priority_mode = trailing_first:TP > trail > stop
# ════════════════════════════════════════════════════════════════

@dataclasses.dataclass
class RuleCtx:
    """精简版 RuleContext(只 standalone 用得到的字段)"""
    entry_price: float
    peak_price: float
    triggered_tiers: set  # 已触发的 TP 档位(idx)
    open: float
    high: float
    low: float
    close: float
    atr: float
    hold_days: int
    first_day_hold_value: int = 2
    P: dict = None  # 参数


@dataclasses.dataclass
class ExitSig:
    reason: str
    sell_price: float
    sell_ratio: float = 1.0


def rule_hard_stop(ctx: RuleCtx) -> Optional[ExitSig]:
    """硬止损 -4.6%(原 -6% 太紧)"""
    if ctx.hold_days < ctx.first_day_hold_value:
        return None
    stop_price = ctx.entry_price * (1 + ctx.P["hard_stop"])
    if ctx.low > 0 and ctx.low <= stop_price:
        return ExitSig("HS", stop_price)
    if ctx.close / ctx.entry_price - 1 <= ctx.P["hard_stop"]:
        return ExitSig("HS", ctx.close)
    return None


def rule_breakeven_stop(ctx: RuleCtx) -> Optional[ExitSig]:
    """保本止损 — v1 完全漏了,这次补上。"""
    if ctx.hold_days < ctx.first_day_hold_value:
        return None
    if ctx.P["breakeven_threshold"] <= 0:
        return None
    peak_pct = ctx.peak_price / ctx.entry_price - 1
    if peak_pct < ctx.P["breakeven_threshold"]:
        return None
    stop_price = ctx.entry_price * (1 + ctx.P["breakeven_stop"])
    if ctx.low > 0 and ctx.low <= stop_price:
        return ExitSig("BE", max(stop_price, ctx.open))
    if ctx.close <= stop_price:
        return ExitSig("BE", ctx.close)
    return None


def rule_first_day_exit(ctx: RuleCtx) -> Optional[ExitSig]:
    """首日弱势离场(默认 min_profit=0 禁用)"""
    fd = ctx.P["first_day_exit_min_profit"]
    if fd <= 0:
        return None
    if not (ctx.first_day_hold_value <= ctx.hold_days <= ctx.first_day_hold_value + ctx.P["first_day_exit_days"] - 1):
        return None
    day_high_pct = ctx.high / ctx.entry_price - 1
    if day_high_pct < fd:
        return ExitSig(f"FD({day_high_pct*100:.1f}%)", ctx.close)
    return None


def rule_time_force(ctx: RuleCtx) -> Optional[ExitSig]:
    """强制时间退出 12 天"""
    if ctx.hold_days > ctx.P["time_force_days"]:
        return ExitSig("TF", ctx.close)
    return None


def rule_take_profit(ctx: RuleCtx) -> Optional[ExitSig]:
    """多档止盈(叠加模式 VERA 对齐) — 两档:[+2.7%卖20%, +13%卖60%]"""
    if ctx.hold_days < ctx.first_day_hold_value:
        return None
    if not ctx.P["take_profit_tiers"]:
        return None
    # 叠加模式:同 bar 多个档可同时触发,ratio 累加钳 1.0
    new_hits = []
    for idx, t in enumerate(ctx.P["take_profit_tiers"]):
        if idx in ctx.triggered_tiers:
            continue
        target = ctx.entry_price * (1 + t["profit_pct"])
        if ctx.close >= target:  # 用 close 检测(回测,非 5m)
            new_hits.append((idx, t, target))
    if not new_hits:
        return None
    # 最高档成交价 + ratio 累加
    new_hits.sort(key=lambda x: x[0], reverse=True)
    highest_idx, _, highest_target = new_hits[0]
    # TP1 按 tp1_fill_pct 成交
    if highest_idx == 0 and ctx.P.get("tp1_fill_pct", 0) > 0:
        highest_target = ctx.entry_price * (1 + ctx.P["tp1_fill_pct"])
    total_ratio = min(sum(h[1].get("sell_ratio", 1.0) for h in new_hits), 1.0)
    for idx, _, _ in new_hits:
        ctx.triggered_tiers.add(idx)
    return ExitSig(f"TP{highest_idx+1}", highest_target, total_ratio)


def rule_trailing_stop(ctx: RuleCtx) -> Optional[ExitSig]:
    """移动止盈 — 激活 +3.9%,回撤 -1.7%"""
    if ctx.hold_days < ctx.first_day_hold_value:
        return None
    if ctx.peak_price / ctx.entry_price - 1 < ctx.P["trail_activate"]:
        return None
    eff_dd = ctx.P["trail_dd"]
    if ctx.P["use_atr_trail"] and ctx.atr > 0:
        atr_pct = ctx.P["atr_trail_multiplier"] * ctx.atr / ctx.entry_price
        eff_dd = max(ctx.P["trail_dd"], atr_pct)
    if ctx.low > 0:
        if ctx.low / ctx.peak_price - 1 <= -eff_dd:
            return ExitSig("TR", ctx.peak_price * (1 - eff_dd))
    if ctx.close / ctx.peak_price - 1 <= -eff_dd:
        return ExitSig("TR", ctx.close)
    return None


def rule_time_condition(ctx: RuleCtx) -> Optional[ExitSig]:
    """时间条件退出 7 天且盈 > 2.5%"""
    if ctx.hold_days > ctx.P["time_exit_days"]:
        if ctx.close / ctx.entry_price - 1 > ctx.P["time_exit_profit"]:
            return ExitSig("TC", ctx.close)
    return None


# 规则评估顺序(trailing_first 模式 — TP > trail > hard_stop > BE > FD > TF > TC)
RULES_TRAILING_FIRST = [
    (110, rule_take_profit,    "TP"),
    (105, rule_trailing_stop,  "TR"),
    (100, rule_hard_stop,      "HS"),
    ( 95, rule_breakeven_stop, "BE"),
    ( 90, rule_first_day_exit, "FD"),
    ( 80, rule_time_force,     "TF"),
    ( 20, rule_time_condition, "TC"),
]


def evaluate_exit(pos, today_row: pd.Series, today: date, params: dict,
                   partial_remaining: float = None) -> Optional[ExitSig]:
    """评估退出。partial_remaining != None 时为 TP 部分卖后 check remaining。

    Returns:
        ExitSig 或 None。sell_ratio 已钳到 partial_remaining(若传入)。
    """
    hold_days = (today - pos.entry_date).days
    triggered = set(pos.tp_triggered if hasattr(pos, "tp_triggered") else [])
    ctx = RuleCtx(
        entry_price=pos.entry_price,
        peak_price=max(pos.peak_price, float(today_row["high"])),
        triggered_tiers=triggered,
        open=float(today_row.get("open", pos.entry_price)),
        high=float(today_row.get("high", pos.entry_price)),
        low=float(today_row.get("low", pos.entry_price)),
        close=float(today_row.get("close", pos.entry_price)),
        atr=0.0,
        hold_days=hold_days,
        P=params,
    )
    for priority, rule_fn, _name in RULES_TRAILING_FIRST:
        sig = rule_fn(ctx)
        if sig is None:
            continue
        # 部分卖:若已有剩余仓位,clip
        if partial_remaining is not None and sig.sell_ratio > partial_remaining:
            sig.sell_ratio = partial_remaining
        # 把新增触发的 tier 写回 pos
        if rule_fn is rule_take_profit and pos.tp_triggered is not None:
            for t_idx in triggered:
                pos.tp_triggered.add(t_idx)
        return sig
    return None


def evaluate_exit_full(pos, today_row: pd.Series, today: date, params: dict) -> list:
    """trailing_first 多档交易 — 返回 list(可能多个部分卖+全卖)。"""
    hold_days = (today - pos.entry_date).days
    triggered = set() if not hasattr(pos, "tp_triggered") or pos.tp_triggered is None else set(pos.tp_triggered)
    ctx = RuleCtx(
        entry_price=pos.entry_price,
        peak_price=max(pos.peak_price, float(today_row["high"])),
        triggered_tiers=triggered,
        open=float(today_row.get("open", pos.entry_price)),
        high=float(today_row.get("high", pos.entry_price)),
        low=float(today_row.get("low", pos.entry_price)),
        close=float(today_row.get("close", pos.entry_price)),
        atr=0.0,
        hold_days=hold_days,
        P=params,
    )
    signals = []
    remaining = 1.0
    for priority, rule_fn, _name in RULES_TRAILING_FIRST:
        sig = rule_fn(ctx)
        if sig is None:
            continue
        # 钳 sell_ratio 到 remaining
        if sig.sell_ratio > remaining:
            sig.sell_ratio = remaining
        if sig.sell_ratio <= 0:
            break
        signals.append(sig)
        remaining -= sig.sell_ratio
        if pos.tp_triggered is not None:
            pos.tp_triggered.update(triggered - pos.tp_triggered)
        if remaining <= 0.001 or sig.sell_ratio >= 1.0:
            break
    return signals


# ════════════════════════════════════════════════════════════════
# Data layer
# ════════════════════════════════════════════════════════════════

PARQUET_DIR = ROOT / "data" / "parquet" / "daily"
TDX_CACHE_DIR = ROOT / "output" / "tdx_cache"


def fetch_ohlcv_parquet(code: str) -> Optional[pd.DataFrame]:
    candidates = [PARQUET_DIR / f"{code}.parquet"]
    if "." in code:
        candidates.append(PARQUET_DIR / f"{code.split('.')[0]}.parquet")
    p = next((c for c in candidates if c.exists()), None)
    if not p:
        return None
    try:
        df = pd.read_parquet(p)
    except Exception:
        return None
    if df is None or df.empty or "date" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)
    keep = [c for c in ("date", "open", "high", "low", "close", "volume", "amount") if c in df.columns]
    return df[keep]


def load_tdx_quanqq_cache(start: date, end: date) -> tuple:
    if not TDX_CACHE_DIR.exists():
        return {}, []
    cache_files = list(TDX_CACHE_DIR.glob("*.parquet"))
    if not cache_files:
        return {}, []
    # 优先选含 QUANTQQ 信号 (signal_var=ZP) 的 cache,其次选最新
    def _is_quanqq(p):
        try:
            d = pd.read_parquet(p, columns=["signal_var"])
            return (d["signal_var"] == "ZP").any()
        except Exception:
            return False
    quanqq_caches = [p for p in cache_files if _is_quanqq(p)]
    if not quanqq_caches:
        quanqq_caches = cache_files  # fallback
    fp = max(quanqq_caches, key=lambda p: p.stat().st_mtime)
    print(f"      读 TDX cache: {fp.name} ({fp.stat().st_size/1e6:.1f}MB) — ZP-aware pick ...")
    df = pd.read_parquet(fp)
    sv = df["signal_value"]
    if sv.dtype == object:
        sv = sv.astype(str).map(lambda x: 1 if x in ("1", "True", "true") else 0).astype(np.int8)
    else:
        sv = sv.astype(np.int8)
    df = df.assign(sig=sv)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df[(df["date"] >= start) & (df["date"] <= end)]
    zp = df[df["signal_var"] == "ZP"].copy()
    print(f"      ZP signal rows: {len(zp)}, codes: {zp['code'].nunique()}, dates: {zp['date'].nunique()}")
    dates = sorted(df["date"].unique().tolist())
    date_to_idx = {d: i for i, d in enumerate(dates)}
    signals: Dict[str, np.ndarray] = {}
    for code, grp in zp.groupby("code"):
        arr = np.zeros(len(dates), dtype=np.int8)
        for d, v in zip(grp["date"].tolist(), grp["sig"].astype(int).tolist()):
            i = date_to_idx.get(d)
            if i is not None:
                arr[i] = v
        signals[str(code)] = arr
    return signals, dates


# ════════════════════════════════════════════════════════════════
# Engine
# ════════════════════════════════════════════════════════════════

@dataclasses.dataclass
class Position:
    code: str
    entry_date: date = None
    entry_price: float = 0.0
    shares: int = 0  # 剩余股数(部分卖后会减)
    entry_shares: int = 0  # 入场时股数(记录总仓位)
    peak_price: float = 0.0
    cost_per_share: float = 0.0
    tp_triggered: set = None  # 已触发的 TP 档位 idx set


@dataclasses.dataclass
class Trade:
    code: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    shares: int  # 本次卖出股数
    entry_shares: int  # 入场总股数(便于算总收益)
    return_pct: float  # 这次卖出的收益率
    hold_days: int
    exit_rule: str
    cost_total: float


def run_backtest(
    start: date = date(2020, 1, 1),
    end: date = date.today(),
    capital: float = 1_000_000,
    max_positions: int = 5,
    params: Optional[dict] = None,
    params_overrides: Optional[dict] = None,
    progress_cb: Optional[Callable] = None,
) -> dict:
    if params is None:
        params = load_default_params()
    if params_overrides:
        params.update(params_overrides)
    print(f"═══ 独立回测 v2 (系统实际参数) ═══")
    print(f"区间: {start} → {end} (~{(end-start).days} 日)")
    print(f"资金: CNY{capital:,.0f} | 同时持仓: {max_positions}")
    _tp_str = ",".join(
        f"{t['profit_pct']*100:.1f}%x{t['sell_ratio']*100:.0f}%"
        for t in params["take_profit_tiers"]
    )
    print(f"参数: hard_stop={params['hard_stop']*100:.2f}%, "
          f"tp=[{_tp_str}], "
          f"trail=({params['trail_activate']*100:.1f}%, {params['trail_dd']*100:.1f}%), "
          f"BE=({params['breakeven_threshold']*100:.2f}%->{params['breakeven_stop']*100:.2f}%), "
          f"TE={params['time_exit_days']}d(TF={params['time_force_days']}d)")
    print()

    # ── 1. 加载 signals ──
    print("[1/2] 从 TDX cache 读 QUANTQQ signals ...")
    signals_full, tdx_dates = load_tdx_quanqq_cache(start, end)
    if not signals_full or not tdx_dates:
        raise RuntimeError("TDX cache 不可用;需先跑 TDX bridge 一次")
    print(f"      signals: {len(signals_full)} 只 | TDX 交易日: {len(tdx_dates)}")

    # ── 1.5 加载 OHLCV ──
    print("[1.5] 从系统 parquet 读每只 code 的 OHLCV ...")
    prices_full: Dict[str, pd.DataFrame] = {}
    for code in signals_full.keys():
        df = fetch_ohlcv_parquet(code)
        if df is not None and not df.empty:
            df = df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)
            if not df.empty:
                prices_full[str(code)] = df
    print(f"      实际有 OHLCV: {len(prices_full)} 只")

    # ── 构造 universe + 索引加速 ──
    universe = set(signals_full.keys())
    universe = sorted(universe)[:300]
    print(f"      实际交易股票池: {len(universe)} 只")
    date_index: Dict[str, Dict[date, int]] = {}
    for code, df in prices_full.items():
        date_index[code] = {d: i for i, d in enumerate(df["date"].tolist())}

    trade_dates = tdx_dates

    # ── 2. 主循环 ──
    print("[2/2] 主循环 ...")
    cash = capital
    positions: Dict[str, Position] = {}
    closed_trades: List[Trade] = []
    equity_curve: List[dict] = []
    portfolio_entry_value = {}  # position_code → 入场时投入总现金

    for date_idx, today in enumerate(trade_dates):
        # 估值
        equity_today = cash
        for pos in positions.values():
            df = prices_full.get(pos.code)
            if df is not None and date_index[pos.code].get(today) is not None:
                i = date_index[pos.code][today]
                close = float(df.iloc[i]["close"]) if pd.notna(df.iloc[i]["close"]) else pos.entry_price
                equity_today += pos.shares * close
            else:
                equity_today += pos.shares * pos.entry_price
        equity_curve.append({"date": today.isoformat(), "equity": equity_today})

        # ── 2a. 评估退出 ──
        for code in list(positions.keys()):
            pos = positions[code]
            df = prices_full.get(code)
            if df is None or date_index[code].get(today) is None:
                continue
            i = date_index[code][today]
            row = df.iloc[i]
            if pd.isna(row["close"]):
                continue
            row["high"] = row["high"] if pd.notna(row["high"]) else row["close"]
            row["low"] = row["low"] if pd.notna(row["low"]) else row["close"]
            row["open"] = row["open"] if pd.notna(row["open"]) else row["close"]

            # trailing_first:可能产生多个信号(TP 部分卖 + 剩余 trail/stop 全卖)
            signals = evaluate_exit_full(pos, row, today, params)
            if not signals:
                # 用 peak 维持(可能同日 high 创新高)
                pos.peak_price = max(pos.peak_price, float(row["high"]))
                continue

            for sig_idx, sig in enumerate(signals):
                if sig.sell_ratio <= 0:
                    continue
                sold_shares = int(pos.shares * sig.sell_ratio)
                if sold_shares <= 0:
                    continue
                exit_price = sig.sell_price
                proceeds = sold_shares * exit_price
                # 卖出侧成本:佣金 + 滑点 + 印花税(仅卖出)
                sell_cost_ratio = (params["commission_pct"] + params["slippage_pct"]
                                   + params["stamp_tax_pct"])
                cost = proceeds * sell_cost_ratio
                cash += proceeds - cost
                # 算这次卖出的收益(相对入场价)
                entry_invested = pos.entry_shares * pos.entry_price
                avg_cost = pos.cost_per_share
                this_return_pct = (exit_price / avg_cost - 1) * 100
                closed_trades.append(Trade(
                    code=code,
                    entry_date=pos.entry_date,
                    exit_date=today,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    shares=sold_shares,
                    entry_shares=pos.entry_shares,
                    return_pct=this_return_pct,
                    hold_days=(today - pos.entry_date).days,
                    exit_rule=sig.reason,
                    cost_total=cost * 2,
                ))
                pos.shares -= sold_shares
                if pos.shares <= 0:
                    del positions[code]
                    break
            # 更新 peak
            if code in positions:
                positions[code].peak_price = max(positions[code].peak_price, float(row["high"]))

        # ── 2b. 今日 buy signals ──
        for code in universe:
            if code in positions:
                continue
            if len(positions) >= max_positions:
                break
            zp_arr = signals_full.get(code)
            if zp_arr is None or date_idx >= len(zp_arr) or zp_arr[date_idx] != 1:
                continue
            df = prices_full.get(code)
            if df is None or date_index[code].get(today) is None:
                continue
            i = date_index[code][today]
            entry_price = float(df.iloc[i]["close"]) if pd.notna(df.iloc[i]["close"]) else 0
            if entry_price <= 0:
                continue
            # 单笔上限 — 防止复利把单笔推到不切实际
            position_size = cash / max_positions
            if params.get("max_position_cash") is not None:
                position_size = min(position_size, params["max_position_cash"])
            entry_shares = int(position_size / entry_price / 100) * 100
            if entry_shares < 100:
                continue
            # 买入侧成本:price + 佣金 + 滑点
            buy_cost_ratio = params["commission_pct"] + params["slippage_pct"]
            cost = entry_shares * entry_price * (1 + buy_cost_ratio)
            if cost > cash:
                continue
            cash -= cost
            positions[code] = Position(
                code=code, entry_date=today, entry_price=entry_price,
                shares=entry_shares, entry_shares=entry_shares,
                peak_price=entry_price,
                cost_per_share=entry_price * (1 + buy_cost_ratio),
                tp_triggered=set(),
            )

    print()
    return _build_report(closed_trades, equity_curve, capital, params)


def _build_report(trades: List[Trade], equity_curve: List[dict], capital: float, params: dict) -> dict:
    if not trades:
        return {"stats": {}, "trades": [], "equity_curve": equity_curve}

    rets = np.array([t.return_pct for t in trades])
    hold_days_arr = np.array([t.hold_days for t in trades])
    wins = rets[rets > 0]
    losses = rets[rets <= 0]

    equity = pd.DataFrame(equity_curve)
    equity["date"] = pd.to_datetime(equity["date"])
    equity = equity.set_index("date")
    total_return = (equity["equity"].iloc[-1] / capital - 1) * 100
    dd = (equity["equity"] / equity["equity"].cummax() - 1) * 100
    max_dd = float(dd.min())

    stats = {
        "trade_count": len(trades),
        "win_count": int((rets > 0).sum()),
        "loss_count": int((rets <= 0).sum()),
        "win_rate_pct": round((rets > 0).mean() * 100, 2),
        "avg_return_pct": round(rets.mean(), 3),
        "avg_win_pct": round(wins.mean(), 3) if len(wins) else 0,
        "avg_loss_pct": round(losses.mean(), 3) if len(losses) else 0,
        "profit_factor": round(wins.sum() / abs(losses.sum()), 2) if len(losses) and abs(losses.sum()) > 0 else float("inf"),
        "max_return_pct": round(rets.max(), 2),
        "min_return_pct": round(rets.min(), 2),
        "avg_hold_days": round(hold_days_arr.mean(), 1),
        "max_hold_days": int(hold_days_arr.max()),
        "total_return_pct": round(total_return, 2),
        "max_drawdown_pct": round(max_dd, 2),
    }

    rule_stats = {}
    for t in trades:
        r = t.exit_rule.split("(")[0]  # 主标签(去除括号)
        if r not in rule_stats:
            rule_stats[r] = {"count": 0, "wins": 0, "rets": []}
        rule_stats[r]["count"] += 1
        rule_stats[r]["rets"].append(t.return_pct)
        if t.return_pct > 0:
            rule_stats[r]["wins"] += 1
    for r, s in rule_stats.items():
        s["avg_ret"] = round(np.mean(s["rets"]), 3)
        s["win_rate"] = round(s["wins"] / s["count"] * 100, 2)
        del s["rets"]
        del s["wins"]

    return {
        "stats": stats,
        "params_used": {
            "hard_stop": params["hard_stop"],
            "take_profit_tiers": params["take_profit_tiers"],
            "trail_activate": params["trail_activate"],
            "trail_dd": params["trail_dd"],
            "breakeven_threshold": params["breakeven_threshold"],
            "breakeven_stop": params["breakeven_stop"],
            "time_exit_days": params["time_exit_days"],
            "time_force_days": params["time_force_days"],
        },
        "rule_breakdown": rule_stats,
        "trades": [dataclasses.asdict(t) for t in trades],
        "equity_curve": [{"date": str(p["date"]).split(" ")[0], "equity": p["equity"]}
                         for p in equity_curve],
    }


def print_report(report: dict):
    s = report["stats"]
    print("═" * 60)
    print("       独立回测 v2 — QUANTQQ + 系统实际参数 + 9 规则")
    print("═" * 60)
    print(f"  总交易数:   {s['trade_count']}")
    print(f"  胜率:       {s['win_rate_pct']}%  ({s['win_count']} 胜 / {s['loss_count']} 负)")
    print(f"  盈亏比:     {s['profit_factor']}")
    print(f"  平均收益:   {s['avg_return_pct']}%")
    print(f"  平均盈利:   +{s['avg_win_pct']}%")
    print(f"  平均亏损:   {s['avg_loss_pct']}%")
    print(f"  持仓周期:   {s['avg_hold_days']}天 (max {s['max_hold_days']}天)")
    print()
    print(f"  ╔ 总收益:   {s['total_return_pct']}% ╗")
    print(f"  ╚ 最大回撤: {s['max_drawdown_pct']}% ╝")
    print()
    print("─" * 60)
    print("  退出规则分布:")
    for rule, st in report.get("rule_breakdown", {}).items():
        print(f"    {rule:15s}: {st['count']:>4d} 笔, 胜率 {st['win_rate']:>5.1f}%, 平均 {st['avg_ret']:>+6.2f}%")
    print("═" * 60)
    p = report.get("params_used", {})
    print(f"\n  使用参数:")
    print(f"    hard_stop        = {p.get('hard_stop', 0)*100:.2f}%")
    print(f"    take_profit_tiers= {[(t['profit_pct']*100, t['sell_ratio']) for t in p.get('take_profit_tiers', [])]}")
    print(f"    trail_activate   = {p.get('trail_activate', 0)*100:.2f}%")
    print(f"    trail_dd         = {p.get('trail_dd', 0)*100:.2f}%")
    print(f"    breakeven        = {p.get('breakeven_threshold', 0)*100:.2f}% → {p.get('breakeven_stop', 0)*100:.2f}%")
    print(f"    time_exit        = {p.get('time_exit_days', 0)}d / 强制 {p.get('time_force_days', 0)}d")

    out_dir = ROOT / "output" / "_research"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"backtest_v2_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  报告写入: {out_json}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--capital", type=float, default=1_000_000)
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--max-position-cash", type=float, default=200000,
                        help="单笔持仓现金上限($);默认 20 万防止复利")
    parser.add_argument("--slippage", type=float, default=0.0005,
                        help="每笔滑点(per side,默认 0.05%%)")
    parser.add_argument("--stamp-tax", type=float, default=0.001,
                        help="印花税(仅卖,默认 0.1%%)")
    parser.add_argument("--commission", type=float, default=0.00025,
                        help="双边佣金率(默认 0.025%%)")
    args = parser.parse_args()
    t0 = time.time()
    # 把 CLI 参数注入到 load_default_params 之上(覆盖默认)
    params_overrides = {
        "commission_pct": args.commission,
        "slippage_pct": args.slippage,
        "stamp_tax_pct": args.stamp_tax,
        "max_position_cash": args.max_position_cash if args.max_position_cash > 0 else None,
    }
    report = run_backtest(
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        capital=args.capital,
        max_positions=args.max_positions,
        params_overrides=params_overrides,
    )
    print(f"\n回测耗时: {time.time() - t0:.1f}s")
    print_report(report)


if __name__ == "__main__":
    main()
