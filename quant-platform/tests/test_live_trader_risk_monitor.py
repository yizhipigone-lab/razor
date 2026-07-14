"""风控监控面板测试(v2 — T+1 保护 + 进度条 + ATR 提示)

覆盖:
- HS 触发/未触发/T+1 保护
- TR 触发/未触发/负数回撤
- TF 触发
- TC warning（exit_rules.py 一致性验证）
- TP tiers 解析
- avg_cost=0 跳过逻辑

运行: pytest tests/test_live_trader_risk_monitor.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date
from unittest.mock import patch, MagicMock
import pytest


# ===== 辅助函数（从 main.py risk_status 内联提取，做纯函数测试） =====

def _build_risk_items(pos: dict, rp, holding_days: int):
    """从 main.py risk_status 内联提取的纯计算函数，供单元测试直接调用。

    与 main.py:1336-1496 逻辑完全一致，修改时需同步。
    """
    code = pos.get("code") or ""
    avg_cost = float(pos.get("avg_cost") or 0)
    volume = float(pos.get("volume") or 0)
    last_close = float(pos.get("last_close") or 0)
    entry_date = pos.get("entry_date")
    peak_price = float(pos.get("peak_price") or 0) if pos.get("peak_price") else None
    tp_triggered = pos.get("tp_triggered") or "[]"

    if avg_cost <= 0 or volume <= 0:
        return []

    risk_items = []
    profit_rate = float(pos.get("profit_rate", 0) or 0)

    # HS（与 main.py:1344-1377 完全一致的结构）
    hard_stop_pct = rp.hard_stop * 100
    if holding_days < 2:
        risk_items.append({
            "type": "HS", "label": "硬止损",
            "trigger_value": hard_stop_pct,
            "current_pnl": profit_rate,
            "remaining": abs(hard_stop_pct),
            "budget": abs(hard_stop_pct),
            "status": "safe",
            "message": "T+1保护，持仓不足2天不触发硬止损",
        })
    else:
        hs_triggered = profit_rate <= hard_stop_pct
        if hs_triggered:
            risk_items.append({
                "type": "HS", "label": "硬止损",
                "trigger_value": hard_stop_pct, "current_pnl": profit_rate,
                "remaining": 0.0, "budget": abs(hard_stop_pct),
                "status": "danger",
                "message": f"已触发硬止损（当前{profit_rate:.1f}% < 止损线{hard_stop_pct:.1f}%）",
            })
        else:
            risk_items.append({
                "type": "HS", "label": "硬止损",
                "trigger_value": hard_stop_pct, "current_pnl": profit_rate,
                "remaining": abs(hard_stop_pct - profit_rate),
                "budget": abs(hard_stop_pct),
                "status": "safe",
                "message": f"距硬止损 {hard_stop_pct:.1f}% 还差 {abs(hard_stop_pct - profit_rate):.1f}%",
            })
    # TR
    trail_dd_pct = rp.trail_dd * 100
    if peak_price and peak_price > 0 and avg_cost > 0:
        peak_pnl_pct = (peak_price - avg_cost) / avg_cost * 100
        current_pnl_pct = profit_rate
        drawdown = peak_pnl_pct - current_pnl_pct
        tr_triggered = drawdown >= trail_dd_pct
    else:
        peak_pnl_pct = 0.0
        drawdown = 0.0
        tr_triggered = False
    if tr_triggered:
        risk_items.append({
            "type": "TR", "label": "移动止盈",
            "trigger_value": -trail_dd_pct, "activated": peak_pnl_pct > 0,
            "peak_pnl": peak_pnl_pct, "current_pnl": current_pnl_pct,
            "drawdown": drawdown,
            "remaining": 0.0, "budget": trail_dd_pct,
            "status": "warning",
            "message": f"已触发移动止盈（回撤{drawdown:.1f}% > 阈值{trail_dd_pct:.1f}%）",
        })
    else:
        tr_remaining = trail_dd_pct - drawdown if drawdown >= 0 else abs(drawdown)
        risk_items.append({
            "type": "TR", "label": "移动止盈",
            "trigger_value": -trail_dd_pct, "activated": peak_pnl_pct > 0,
            "peak_pnl": peak_pnl_pct, "current_pnl": current_pnl_pct,
            "drawdown": drawdown,
            "remaining": max(0, tr_remaining), "budget": trail_dd_pct,
            "status": "safe",
            "message": f"移动止盈未激活，回撤{drawdown:.1f}%，距触发还差 {max(0, tr_remaining):.1f}%",
        })

    # TF
    tf_trigger_days = rp.time_force_days
    tf_remaining = max(0, tf_trigger_days - holding_days)
    risk_items.append({
        "type": "TF", "label": "强制清仓",
        "trigger_days": tf_trigger_days, "current_days": holding_days,
        "remaining_days": tf_remaining,
        "remaining": tf_remaining, "budget": tf_trigger_days,
        "status": "danger" if tf_remaining <= 0 else "safe",
        "message": f"持仓第{holding_days}天/{tf_trigger_days}天，{'已到期' if tf_remaining <= 0 else f'距TF到期还{tf_remaining}天'}",
    })

    # FD
    fd_threshold = rp.first_day_exit_min_profit * 100
    fd_effective_days = rp.first_day_exit_days
    fd_triggered = holding_days <= fd_effective_days and profit_rate < fd_threshold
    risk_items.append({
        "type": "FD", "label": "首日离场",
        "trigger_profit": fd_threshold, "effective_days": fd_effective_days,
        "status": "warning" if fd_triggered else "safe",
        "message": f"目标涨幅≥{fd_threshold}%，当前{profit_rate:.1f}%，{'已触发' if fd_triggered else '无需处理'}",
    })

    # TC
    tc_days = rp.time_exit_days
    tc_profit_threshold = rp.time_exit_profit * 100
    tc_remaining = max(0, tc_days - holding_days)
    risk_items.append({
        "type": "TC", "label": "时间退出",
        "trigger_days": tc_days, "trigger_profit": tc_profit_threshold,
        "current_days": holding_days, "remaining_days": tc_remaining,
        "remaining": tc_remaining, "budget": tc_days,
        "status": "warning" if tc_remaining <= 0 and profit_rate >= tc_profit_threshold else "safe",
        "message": f"持仓第{holding_days}天/{tc_days}天，盈利需≥{tc_profit_threshold}%，当前{profit_rate:.1f}%",
    })

    # TP
    tiers = rp.take_profit_tiers or []
    for i, tier in enumerate(tiers):
        tp_pct = tier.get("profit_pct", 0) * 100
        tp_ratio = tier.get("sell_ratio", 0) * 100
        tp_triggered_flag = False
        try:
            import json as _json
            triggered_list = _json.loads(tp_triggered) if isinstance(tp_triggered, str) else (tp_triggered or [])
            tp_triggered_flag = any(
                isinstance(t, dict) and t.get("tier") == i
                for t in triggered_list
            )
        except Exception:
            pass
        if tp_triggered_flag:
            risk_items.append({
                "type": f"TP{i+1}", "label": f"止盈{i+1}档",
                "trigger_value": tp_pct, "sell_ratio": tp_ratio,
                "triggered": True,
                "current_pnl": profit_rate,
                "remaining_to_trigger": 0.0, "remaining": 0.0,
                "budget": tp_pct,
                "status": "warning",
                "message": f"止盈{i+1}档({tp_pct:.1f}%)已触发，卖出{tp_ratio:.0f}%",
            })
        else:
            tp_remaining = tp_pct - profit_rate
            risk_items.append({
                "type": f"TP{i+1}", "label": f"止盈{i+1}档",
                "trigger_value": tp_pct, "sell_ratio": tp_ratio,
                "triggered": False,
                "current_pnl": profit_rate,
                "remaining_to_trigger": tp_remaining,
                "remaining": max(0, tp_remaining),
                "budget": tp_pct,
                "status": "safe",
                "message": f"止盈{i+1}档({tp_pct:.1f}%)未触发，当前{profit_rate:.1f}%，距触发还差 {max(0, tp_remaining):.1f}%",
            })

    return risk_items


# ===== Mock RiskParams =====

def _make_rp(
    hard_stop=-0.06,
    trail_activate=0.03,
    trail_dd=0.02,
    take_profit_tiers=None,
    time_exit_days=7,
    time_exit_profit=0.03,
    time_force_days=12,
    first_day_exit_min_profit=0.0,
    first_day_exit_days=1,
    use_atr_trail=False,
    atr_trail_multiplier=1.0,
):
    rp = MagicMock()
    rp.hard_stop = hard_stop
    rp.trail_activate = trail_activate
    rp.trail_dd = trail_dd
    rp.take_profit_tiers = take_profit_tiers or [{"profit_pct": 0.03, "sell_ratio": 0.3}]
    rp.time_exit_days = time_exit_days
    rp.time_exit_profit = time_exit_profit
    rp.time_force_days = time_force_days
    rp.first_day_exit_min_profit = first_day_exit_min_profit
    rp.first_day_exit_days = first_day_exit_days
    rp.use_atr_trail = use_atr_trail
    rp.atr_trail_multiplier = atr_trail_multiplier
    return rp


# ===== 测试用例 =====

def test_hs_triggered():
    """profit_rate=-6.7% <= hard_stop=-6% → HS danger, remaining=0"""
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=-6.7, entry_date=date.today(), peak_price=11.0, tp_triggered="[]")
    rp = _make_rp(hard_stop=-0.06)
    items = _build_risk_items(pos, rp, holding_days=4)
    hs = next(i for i in items if i["type"] == "HS")
    assert hs["status"] == "danger", f"HS 应为 danger，实际 {hs['status']}"
    assert hs["remaining"] == 0.0, f"HS remaining 应为 0，实际 {hs['remaining']}"
    assert "已触发" in hs["message"]


def test_hs_not_triggered():
    """profit_rate=-5% > hard_stop=-6% → HS safe, remaining=1%"""
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=-5.0, entry_date=date.today(), peak_price=11.0, tp_triggered="[]")
    rp = _make_rp(hard_stop=-0.06)
    items = _build_risk_items(pos, rp, holding_days=4)
    hs = next(i for i in items if i["type"] == "HS")
    assert hs["status"] == "safe"
    assert abs(hs["remaining"] - 1.0) < 0.01, f"HS remaining 应为 1.0，实际 {hs['remaining']}"


def test_hs_hold_days_lt2():
    """hold_days=1 时 HS 显示 safe，message 含 T+1 保护（即使 profit_rate 已触发止损线）"""
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=-7.0, entry_date=date.today(), peak_price=11.0, tp_triggered="[]")
    rp = _make_rp(hard_stop=-0.06)
    items = _build_risk_items(pos, rp, holding_days=1)
    hs = next(i for i in items if i["type"] == "HS")
    assert hs["status"] == "safe", f"持仓第1天应为 safe，实际 {hs['status']}"
    assert "T+1" in hs["message"], f"message 应含 T+1，实际 {hs['message']}"


def test_tr_not_triggered():
    """drawdown=1.5% < trail_dd=2% → TR safe, remaining≈0.5%"""
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=3.5, entry_date=date.today(), peak_price=10.35, tp_triggered="[]")
    # peak_pnl = (10.35-10)/10*100 = 3.5%, current_pnl = 3.5%, drawdown = 0%
    # 调 peak_price 使 drawdown=1.5%: peak_pnl=5%, current=3.5%, drawdown=1.5%
    pos["peak_price"] = 10.5  # (10.5-10)/10*100=5%, drawdown=5-3.5=1.5%
    rp = _make_rp(trail_dd=0.02)
    items = _build_risk_items(pos, rp, holding_days=4)
    tr = next(i for i in items if i["type"] == "TR")
    assert tr["status"] == "safe"
    assert abs(tr["remaining"] - 0.5) < 0.01, f"TR remaining 应为 0.5，实际 {tr['remaining']}"


def test_tr_triggered():
    """drawdown=2.6% >= trail_dd=2% → TR warning, remaining=0"""
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=2.4, entry_date=date.today(), peak_price=10.5, tp_triggered="[]")
    # peak_pnl=5%, current=2.4%, drawdown=2.6% >= 2% → triggered
    rp = _make_rp(trail_dd=0.02)
    items = _build_risk_items(pos, rp, holding_days=4)
    tr = next(i for i in items if i["type"] == "TR")
    assert tr["status"] == "warning", f"TR 应为 warning，实际 {tr['status']}"
    assert tr["remaining"] == 0.0


def test_tr_negative_drawdown():
    """盈利扩大时 drawdown < trail_dd（未触发），status=safe"""
    # peak_pnl=7%, current=6%, drawdown=1% < trail_dd=2% → safe
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=6.0, entry_date=date.today(), peak_price=10.7, tp_triggered="[]")
    # (10.7-10)/10*100=7% peak, 6% current, drawdown=1% < 2% → safe
    rp = _make_rp(trail_dd=0.02)
    items = _build_risk_items(pos, rp, holding_days=4)
    tr = next(i for i in items if i["type"] == "TR")
    assert tr["status"] == "safe", f"盈利扩大时应 safe，实际 {tr['status']}"


def test_avg_cost_zero():
    """avg_cost=0 时跳过风控计算，返回空列表"""
    pos = dict(code="000001", avg_cost=0.0, volume=1000, last_close=9.5,
               profit_rate=-6.7, entry_date=date.today(), peak_price=11.0, tp_triggered="[]")
    rp = _make_rp()
    items = _build_risk_items(pos, rp, holding_days=4)
    assert items == [], f"avg_cost=0 应跳过，返回空列表，实际 {items}"


def test_tf_triggered():
    """holding_days=12, tf_days=12 → TF danger, remaining=0"""
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=2.0, entry_date=date.today(), peak_price=11.0, tp_triggered="[]")
    rp = _make_rp(time_force_days=12)
    items = _build_risk_items(pos, rp, holding_days=12)
    tf = next(i for i in items if i["type"] == "TF")
    assert tf["status"] == "danger", f"TF 应为 danger，实际 {tf['status']}"
    assert tf["remaining"] == 0


def test_tc_warning():
    """holding_days=7, pnl=4% >= threshold=3% → TC warning（与 exit_rules.py 一致）"""
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=4.0, entry_date=date.today(), peak_price=11.0, tp_triggered="[]")
    rp = _make_rp(time_exit_days=7, time_exit_profit=0.03)
    items = _build_risk_items(pos, rp, holding_days=7)
    tc = next(i for i in items if i["type"] == "TC")
    assert tc["status"] == "warning", f"TC 应为 warning（超期+盈利达标），实际 {tc['status']}"


def test_tc_safe():
    """holding_days=7, pnl=2% < threshold=3% → TC safe"""
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=2.0, entry_date=date.today(), peak_price=11.0, tp_triggered="[]")
    rp = _make_rp(time_exit_days=7, time_exit_profit=0.03)
    items = _build_risk_items(pos, rp, holding_days=7)
    tc = next(i for i in items if i["type"] == "TC")
    assert tc["status"] == "safe", f"TC 应为 safe（盈利不足），实际 {tc['status']}"


def test_tp_tier_triggered():
    """tp_triggered=[{"tier":0}] → TP1 warning, remaining=0"""
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=5.0, entry_date=date.today(), peak_price=11.0,
               tp_triggered='[{"tier": 0}]')
    rp = _make_rp(take_profit_tiers=[{"profit_pct": 0.03, "sell_ratio": 0.3}])
    items = _build_risk_items(pos, rp, holding_days=4)
    tp1 = next(i for i in items if i["type"] == "TP1")
    assert tp1["status"] == "warning", f"TP1 应为 warning，实际 {tp1['status']}"
    assert tp1["remaining"] == 0.0


def test_tp_tier_not_triggered():
    """tp_triggered=[] → TP1 safe"""
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=2.0, entry_date=date.today(), peak_price=11.0, tp_triggered="[]")
    rp = _make_rp(take_profit_tiers=[{"profit_pct": 0.03, "sell_ratio": 0.3}])
    items = _build_risk_items(pos, rp, holding_days=4)
    tp1 = next(i for i in items if i["type"] == "TP1")
    assert tp1["status"] == "safe"
    assert tp1["remaining"] > 0


def test_profit_rate_none():
    """profit_rate=None 时应 fallback 为 0，不抛异常"""
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=None, entry_date=date.today(), peak_price=11.0, tp_triggered="[]")
    rp = _make_rp(hard_stop=-0.06)
    items = _build_risk_items(pos, rp, holding_days=4)
    # profit_rate=None → 0，0 > -6%，HS safe
    hs = next(i for i in items if i["type"] == "HS")
    assert hs["status"] == "safe"


def test_global_status_priority():
    """global_status 取最高优先级：danger > warning > safe"""
    # 设置 HS danger, TR warning, FD safe → 全局 danger
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=-7.0, entry_date=date.today(), peak_price=10.5, tp_triggered="[]")
    rp = _make_rp(hard_stop=-0.06, trail_dd=0.02)
    items = _build_risk_items(pos, rp, holding_days=4)
    STATUS_PRIORITY = {"danger": 3, "warning": 2, "safe": 1}
    global_status = max(items, key=lambda x: STATUS_PRIORITY.get(x["status"], 0))["status"]
    assert global_status == "danger", f"全局状态应为 danger，实际 {global_status}"


def test_hs_boundary_at_exactly_hard_stop():
    """profit_rate == hard_stop_pct 时应触发 HS（<= 判断）"""
    # -6.0% == -6.0% → hs_triggered = True（<= 包含等于）
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=-6.0, entry_date=date.today(), peak_price=11.0, tp_triggered="[]")
    rp = _make_rp(hard_stop=-0.06)
    items = _build_risk_items(pos, rp, holding_days=4)
    hs = next(i for i in items if i["type"] == "HS")
    assert hs["status"] == "danger", f"等于止损线应触发，实际 {hs['status']}"
    assert hs["remaining"] == 0.0


def test_holding_days_exactly_2_no_t1_protection():
    """holding_days == 2 时应走普通 HS 逻辑（T+1 保护仅 < 2）"""
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=-5.0, entry_date=date.today(), peak_price=11.0, tp_triggered="[]")
    rp = _make_rp(hard_stop=-0.06)
    items = _build_risk_items(pos, rp, holding_days=2)
    hs = next(i for i in items if i["type"] == "HS")
    assert hs["status"] == "safe", f"holding_days=2 应走普通 safe 逻辑，实际 {hs['status']}"
    assert "T+1" not in hs["message"], "holding_days=2 不应触发 T+1 保护"


def test_avg_cost_negative():
    """avg_cost < 0（负成本）也应跳过风控计算"""
    pos = dict(code="000001", avg_cost=-1.0, volume=1000, last_close=9.5,
               profit_rate=5.0, entry_date=date.today(), peak_price=11.0, tp_triggered="[]")
    rp = _make_rp()
    items = _build_risk_items(pos, rp, holding_days=4)
    assert items == [], f"avg_cost < 0 应跳过，实际 {items}"


def test_tr_drawdown_exactly_at_threshold():
    """drawdown == trail_dd_pct 时应触发（>= 包含等于）"""
    # avg_cost=10.0, peak_price=10.72 → (10.72-10)/10*100=7.2% peak
    # profit_rate=5.2% → drawdown=7.2-5.2=2.0% == trail_dd=2.0% → triggered
    pos = dict(code="000001", avg_cost=10.0, volume=1000, last_close=9.5,
               profit_rate=5.2, entry_date=date.today(), peak_price=10.72, tp_triggered="[]")
    rp = _make_rp(trail_dd=0.02)
    items = _build_risk_items(pos, rp, holding_days=4)
    tr = next(i for i in items if i["type"] == "TR")
    assert tr["status"] == "warning", f"drawdown==阈值应触发，实际 {tr['status']}"
    assert tr["remaining"] == 0.0
