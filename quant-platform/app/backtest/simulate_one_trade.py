"""单笔交易仿真 深 module(候选②)。

从 app.backtest.engine._simulate_trade_daily_fallback 抽出为独立函数(已重构);
engine + ai_optimizer._fast_simulate 都委托它 → 影子变忠实,日线路径单一真相。

对比旧 _fast_simulate 影子的关键修正:
- TP1 固定 entry*(1+tp1_fill_pct=0.03),不按真实档位 target 成交
  (旧影子按 entry*(1+tp_pct) 成交,tp1=10% 时差 7%)
- trailing_first / stack_mode 生效(走 exit_rule_engine,而非固定 TP→HS→TR)
- 无假默认:风控参数走 params_override → schema 两级兜底(缺键 RuntimeError);TP档位走 params_override(有tp1时连带tp2/tp3,缺档兜底10/20/30%)或 settings.staged_take_profit
- 成本走 execution.get_cost_cfg()(单一真相源;commission+slippage/stamp 比例与旧 engine 一致)

intraday 逐 bar 仿真(simple_runner/tdx_runner)逻辑不同,不在本 kernel 范围。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from app.backtest.execution import get_cost_cfg


def _build_trade_result(code, name, entry, exit_p, b_date, e_date, hold_days, pnl, events):
    """构造交易结果 dict(原 engine._wrap_result 逻辑)。"""
    reasons = [e["reason"].split("(")[0] for e in events if e["type"] == "sell"]
    b_str = str(b_date)
    e_str = str(e_date)
    if " " in b_str:
        b_str = b_str.split(" ")[0]
    if " " in e_str:
        e_str = e_str.split(" ")[0]
    return {
        "code": code,
        "name": name,
        "entry_date": b_str,
        "exit_date": e_str,
        "entry_price": entry,
        "exit_price": exit_p,
        "shares": 0,
        "return_pct": round(pnl, 4),
        "profit_amount": 0.0,
        "exit_reason": "+".join(reasons) if reasons else "",
        "hold_days": hold_days,
        "sell_events": events,
        "remaining_ratio": 0.0,
    }


def simulate_one_trade(
    code: str,
    stock_name: str,
    entry_price: float,
    signal_date,
    bars_daily: pd.DataFrame,
    params_override: dict = None,
    time_exit_min_pnl: float = None,
    apply_costs: bool = None,
) -> Optional[dict]:
    """日线单笔交易仿真。

    返回 trade dict(同 engine._simulate_trade_daily_fallback + _wrap_result 的产出结构)。
    """
    if bars_daily.empty:
        return None

    # 成本走 execution 单一真相源(commission+slippage / +stamp;与旧 engine 0.125/0.175 一致)
    cost_cfg = get_cost_cfg()
    buy_pct = cost_cfg["commission_rate"] + cost_cfg["slippage_rate"]
    sell_pct = cost_cfg["commission_rate"] + cost_cfg["stamp_tax_rate"] + cost_cfg["slippage_rate"]

    from app.backtest.exit_rules import exit_rule_engine, _pct
    from core.settings import settings
    if apply_costs is None:
        apply_costs = settings.backtest_apply_costs

    # 风控参数:params_override 优先 → 否则 schema(无假默认,缺键报错)
    from app.config.risk_params import load_risk_params
    _risk = load_risk_params()
    _SCHEMA_PCT_FIELDS = {
        "hard_stop_loss_pct": "hard_stop",
        "breakeven_threshold_pct": "breakeven_threshold_pct",
        "breakeven_stop_pnl_pct": "breakeven_stop_pnl_pct",
        "trailing_activate_pct": "trail_activate",
        "trailing_drawdown_pct": "trail_dd",
        "first_day_exit_min_profit": "first_day_exit_min_profit",
    }
    _SCHEMA_INT_FIELDS = {
        "time_exit_days": "time_exit_days",
        "time_exit_force_days": "time_force_days",
        "first_day_exit_days": "first_day_exit_days",
    }

    def _p(key):
        if params_override and key in params_override:
            return params_override[key]
        if key in _SCHEMA_PCT_FIELDS:
            return getattr(_risk, _SCHEMA_PCT_FIELDS[key], 0.0) * 100
        if key in _SCHEMA_INT_FIELDS:
            return getattr(_risk, _SCHEMA_INT_FIELDS[key])
        raise RuntimeError(f"simulate_one_trade 缺风控参数: {key},无假默认,需 schema 或 params_override")

    hard_sl      = _p("hard_stop_loss_pct")
    be_thresh    = _p("breakeven_threshold_pct")
    be_stop      = _p("breakeven_stop_pnl_pct")
    trail_act    = _p("trailing_activate_pct")
    trail_dd     = _p("trailing_drawdown_pct")
    max_hold     = _p("time_exit_days")
    force_hold   = _p("time_exit_force_days")
    fd_min_profit = _p("first_day_exit_min_profit")
    fd_days = _p("first_day_exit_days")

    # TP plan(优先 params_override → settings.staged_take_profit)
    if params_override and "tp1_profit" in params_override:
        active_tp_plan = [
            {"profit_pct": params_override.get("tp1_profit", 10.0) / 100.0,
             "sell_ratio": params_override.get("tp1_ratio", 0.33),
             "label": "分阶止盈1"},
            {"profit_pct": params_override.get("tp2_profit", 20.0) / 100.0,
             "sell_ratio": params_override.get("tp2_ratio", 0.33),
             "label": "分阶止盈2"},
        ]
        if "tp3_profit" in params_override:
            active_tp_plan.append({
                "profit_pct": params_override.get("tp3_profit", 30.0) / 100.0,
                "sell_ratio": params_override.get("tp3_ratio", 0.34),
                "label": "分阶止盈3", "sell_all": True,
            })
    else:
        active_tp_plan = settings.staged_take_profit or []

    remaining_ratio = 1.0
    highest = entry_price
    staged_done: set = set()
    realized_pnl = 0.0
    exit_price, exit_date, hold_days = None, None, 0
    sell_events = [{"type": "buy", "date": str(signal_date), "price": entry_price,
                    "ratio": 1.0, "reason": "买入(日线)"}]

    # buy 成本摊入 entry
    cost_entry = entry_price * (1 + buy_pct) if apply_costs else entry_price

    def _cost_pnl(raw_sell_price, ratio):
        if apply_costs:
            cost_sell = raw_sell_price * (1 - sell_pct)
            return ((cost_sell / cost_entry) - 1) * 100 * ratio
        return ((raw_sell_price / entry_price) - 1) * 100 * ratio

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
            use_high_for_tp=True, first_day_hold_value=1,
        )
        signal = exit_rule_engine.check(ctx)
        if signal is None:
            continue

        reason = signal.reason
        if reason.startswith("TP"):
            idx = int(reason[2]) - 1
            staged_done.add(idx)
            for si, stage in enumerate(active_tp_plan):
                if si != idx:
                    continue
                sell_ratio = remaining_ratio if stage.get("sell_all") else stage.get("sell_ratio", 0.0)
                actual_sell = min(sell_ratio, remaining_ratio)
                if actual_sell > 0:
                    realized_pnl += _cost_pnl(signal.sell_price, actual_sell)
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
            realized_pnl += _cost_pnl(signal.sell_price, remaining_ratio)
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
        realized_pnl += _cost_pnl(exit_price, remaining_ratio)
        sell_events.append({"type": "sell", "date": str(exit_date), "price": exit_price,
                            "ratio": remaining_ratio, "reason": "清仓"})

    return _build_trade_result(code, stock_name, entry_price, exit_price,
                               signal_date, exit_date, hold_days, realized_pnl, sell_events)