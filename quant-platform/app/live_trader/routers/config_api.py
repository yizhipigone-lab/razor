"""实盘交易 - 配置热加载路由。

阶段 1 第 3 步(2026-07-19) 从 main.py 抽离,共 12 个路由(7 个语义端点 × GET/PUT):
  GET/PUT /live/config/scan-interval   (扫描间隔)
  GET/PUT /live/config/auto-buy-time   (自动选股时点)
  GET/PUT /live/config/buy-ratio       (单只占本金比例)
  GET/PUT /live/config/switches        (买入/卖出/auto_buy/kill_switch 开关)
  GET/POST /live/config/mode           (dry-run/live 模式切换)
  GET     /live/config/risk-params     (风险参数展示)
  GET     /live/config/risk-status     (持仓风控实时状态,大函数)

依赖策略:
  - 顶部 import: asyncio(set_mode 用 asyncio.sleep)、_state、auth(_require_admin)、fastapi、logger
  - get_risk_status 函数内 import _resolve_instrument_name(与 positions 共用,
    _instrument_name_cache 暂留 main,所有路由搬完后统一归属)
  - 各路由的 from core.settings / from app.config.risk_params / from app.live_trader.utils
    都是绝对/本包路径,搬迁后不变
- set_mode 用 _state["mode_switching"] 做跨请求并发互斥(architect R3 标注的业务状态),
  通过 _state dict 访问,lifespan 装配后所有 router 共享同一 dict。
"""
import asyncio

from fastapi import APIRouter, HTTPException, Request

from core.logger import get_logger

from .._state import state as _state
from ..auth import _require_admin

logger = get_logger("live_trader.routers.config_api")

router = APIRouter()


@router.get("/live/config/scan-interval")
async def get_scan_interval():
    """获取当前离场扫描间隔(秒)"""
    scheduler = _state.get("scheduler")
    if not scheduler:
        raise HTTPException(503, "调度服务未启动")
    return {"interval_sec": scheduler.get_scan_interval()}


@router.put("/live/config/scan-interval")
async def set_scan_interval(request: Request, body: dict):
    """设置离场扫描间隔(秒,仅本地)。保存后立即生效,不阻塞。范围:10~300"""
    _require_admin(request)
    scheduler = _state.get("scheduler")
    if not scheduler:
        raise HTTPException(503, "调度服务未启动")
    seconds = body.get("interval_sec")
    if seconds is None:
        raise HTTPException(400, "缺少 interval_sec 参数")
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        raise HTTPException(400, "interval_sec 必须为数字")
    if seconds < 10 or seconds > 300:
        raise HTTPException(400, "interval_sec 范围:10~300 秒")
    # 立即生效(内存)
    scheduler.set_scan_interval(seconds)
    # 持久化到 settings(下次启动自动加载)
    from core.settings import settings
    settings.set("live_trader", "exit_scan_interval_sec", seconds, save=True)
    return {"interval_sec": scheduler.get_scan_interval(), "saved": True}


@router.get("/live/config/auto-buy-time")
async def get_auto_buy_time():
    """获取自动选股触发时点(HH:MM)"""
    scheduler = _state.get("scheduler")
    if not scheduler:
        raise HTTPException(503, "调度服务未启动")
    return {"auto_buy_time": scheduler.get_auto_buy_time()}


@router.put("/live/config/auto-buy-time")
async def set_auto_buy_time(request: Request, body: dict):
    """设置自动选股触发时点(仅本地)。格式 HH:MM，保存后当日已触发则次日生效。"""
    _require_admin(request)
    scheduler = _state.get("scheduler")
    if not scheduler:
        raise HTTPException(503, "调度服务未启动")
    t = body.get("auto_buy_time")
    if not t:
        raise HTTPException(400, "缺少 auto_buy_time 参数")
    try:
        scheduler.set_auto_buy_time(t)
    except ValueError as e:
        raise HTTPException(400, str(e))
    from core.settings import settings
    settings.set("live_trader", "auto_buy_time", t, save=True)
    audit = _state.get("audit")
    if audit:
        audit.log("auto_buy_time_changed", snapshot={"auto_buy_time": t})
    logger.info(f"auto_buy_time 设置: {t}")
    return {"auto_buy_time": scheduler.get_auto_buy_time(), "saved": True}


@router.get("/live/config/buy-ratio")
async def get_buy_ratio():
    """获取单只占本金比例(实盘下单 = clamp(本金×比例, 全局min, 全局max))"""
    rs = _state.get("runtime_state")
    if not rs:
        raise HTTPException(503, "未初始化")
    return {"buy_position_ratio": rs.buy_position_ratio}


@router.put("/live/config/buy-ratio")
async def set_buy_ratio(request: Request, body: dict):
    """设置单只占本金比例(仅本地)。内存立即生效 + 持久化到 live_trader.buy_position_ratio 顶层。范围:(0,1]"""
    _require_admin(request)
    rs = _state.get("runtime_state")
    if not rs:
        raise HTTPException(503, "未初始化")
    ratio = body.get("buy_position_ratio")
    if ratio is None:
        raise HTTPException(400, "缺少 buy_position_ratio 参数")
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        raise HTTPException(400, "buy_position_ratio 必须为数字")
    if not (0 < ratio <= 1):
        raise HTTPException(400, "buy_position_ratio 范围:(0,1]")
    old = rs.set_buy_position_ratio(ratio)                                  # 内存立即生效
    from core.settings import settings
    settings.set("live_trader", "buy_position_ratio", ratio, save=True)     # 落盘顶层(不进 runtime 段)
    audit = _state.get("audit")
    if audit:
        audit.log("buy_ratio_changed", snapshot={"old": old, "new": ratio})
    logger.info(f"buy_position_ratio: {old} -> {ratio}")
    return {"buy_position_ratio": rs.buy_position_ratio, "saved": True}


# ===== 运行时开关/模式切换(v2 §3.3/§3.4)=====

@router.get("/live/config/switches")
async def get_switches():
    """读取买入/卖出开关状态"""
    rs = _state.get("runtime_state")
    if not rs:
        raise HTTPException(503, "未初始化")
    return rs.get_state()


@router.put("/live/config/switches")
async def set_switches(request: Request, body: dict):
    """切换买入/卖出/auto_buy/kill_switch 开关(仅本地)"""
    _require_admin(request)
    rs = _state.get("runtime_state")
    if not rs:
        raise HTTPException(503, "未初始化")
    buy = body.get("buy_enabled")
    sell = body.get("sell_enabled")
    auto_buy = body.get("auto_buy_enabled")
    kill_switch_en = body.get("kill_switch_enabled")
    old = rs.set_switches(buy_enabled=buy, sell_enabled=sell,
                          auto_buy_enabled=auto_buy, kill_switch_enabled=kill_switch_en)
    # 关闭急停主开关时,顺带清理当前激活态(内存+文件+DB),使重新启用时是干净状态。
    # 注意:此时 is_active() 已因主开关关闭而短路返回 False,故不能用它判断,需直接 deactivate()
    # (deactivate 本身不检查 is_enabled,且对无激活态是安全 no-op)。
    if kill_switch_en is False:
        ks = _state.get("kill_switch")
        if ks:
            ks.deactivate()
            logger.warning("kill_switch 主开关已关闭,并清理残留激活态(若有)")
    audit = _state.get("audit")
    if audit:
        audit.log("switch_changed", snapshot={"old": old, "new": {
            "buy_enabled": buy, "sell_enabled": sell,
            "auto_buy_enabled": auto_buy, "kill_switch_enabled": kill_switch_en}})
    logger.info(f"开关切换: {old} -> buy={buy} sell={sell} auto_buy={auto_buy} kill_switch={kill_switch_en}")
    return rs.get_state()


@router.get("/live/config/mode")
async def get_mode():
    """读取当前模式(dry-run/live)"""
    rs = _state.get("runtime_state")
    if not rs:
        raise HTTPException(503, "未初始化")
    return {"mode": rs.mode}


@router.post("/live/config/mode")
async def set_mode(request: Request, body: dict):
    """切换模式(仅本地)。live→dry-run 撤在途单等终态;dry-run→live 清残留+QMT检查"""
    _require_admin(request)
    rs = _state.get("runtime_state")
    if not rs:
        raise HTTPException(503, "未初始化")
    new_mode = body.get("mode")
    if new_mode not in ("dry-run", "live"):
        raise HTTPException(400, "mode 必须 dry-run/live")

    # v2审计高-2: 防并发切换(标志带时间戳,超60s自动恢复防死锁)
    import time as _time
    _ms = _state.get("mode_switching")
    if _ms and (_time.time() - _ms) < 60:
        raise HTTPException(409, "模式切换中,请稍后(已有切换在进行)")
    _state["mode_switching"] = _time.time()

    ks = _state.get("kill_switch")
    store = _state.get("store")
    qmt = _state.get("qmt")
    audit = _state.get("audit")

    # kill_switch 激活时禁止切 live(§3.4)
    if new_mode == "live" and ks and ks.is_active():
        raise HTTPException(409, "kill_switch 激活中,禁止切 live")

    # live→dry-run:撤所有在途真实委托 + 轮询等终态(超时阻断,§3.4)
    if new_mode == "dry-run" and rs.is_live():
        inflight = store.get_inflight_orders(live_only=True) if store else []
        for o in inflight:
            oid = o.get("order_id")
            if oid and qmt:
                try:
                    ret = qmt.cancel_order(oid)
                    # v2审计高-1: 检查返回值(0成功,-1断开,-3未找到)
                    if ret == -1:
                        raise HTTPException(409, f"撤单 {oid} 失败(QMT 断开),阻断切换")
                    if ret not in (0, -3):
                        logger.warning(f"撤单 {oid} 返回 {ret}")
                except HTTPException:
                    raise
                except Exception as e:
                    logger.warning(f"撤单 {oid} 异常: {e}")
        # 轮询等终态(每0.5s,上限30s),只看 live 在途(中-7)
        remaining = inflight
        for _ in range(60):
            await asyncio.sleep(0.5)
            remaining = store.get_inflight_orders(live_only=True) if store else []
            if not remaining:
                break
        if remaining:
            raise HTTPException(409, f"撤单未全部终态,阻断切换(剩余 {len(remaining)} 笔,需人工处理)")

    # dry-run→live:清 mock 残留 + QMT 连接检查(§3.4)
    if new_mode == "live" and rs.is_dry_run():
        if not qmt or not qmt.connected:
            raise HTTPException(409, "QMT 未连接,禁止切 live")
        if store:
            try:
                store.clean_dryrun_residue()
            except Exception as e:
                logger.error(f"清 dry-run 残留失败: {e}")

    old = rs.set_mode(new_mode)
    _state["mode_switching"] = 0  # v2高-2: 清切换标志
    if audit:
        audit.log("mode_switched", snapshot={"old": old, "new": new_mode})
    logger.info(f"模式切换: {old} -> {new_mode}")
    return {"old": old, "new": new_mode}


@router.get("/live/config/risk-params")
async def get_risk_params():
    """返回 risk 段止盈止损参数(前端展示用,不写死)"""
    from core.settings import settings
    keys = ["hard_stop_loss_pct", "take_profit_tiers", "trailing_stop_activate_pct",
            "trailing_stop_drawdown_pct", "time_exit_days", "time_exit_min_profit_pct",
            "time_exit_force_days", "first_day_exit_min_profit", "first_day_exit_days"]
    return {"params": {k: settings.get("risk", k) for k in keys}}


@router.get("/live/config/risk-status")
async def get_risk_status():
    """返回持仓风控实时状态（展示用，不改变任何交易逻辑）。

    进度条宽度 = remaining / budget * 100（已触发 remaining<=0 则 100%）：
      HS:  budget = |hard_stop|（百分点）
      TR:  budget = trail_dd（百分点，回撤超过即触发）
      TF:  budget = trigger_days（天数）
      TP:  budget = profit_pct（百分点）
      FD:  budget = first_day_exit_min_profit（百分点）
      TC:  budget = time_exit_days（天数）
    """
    from datetime import datetime
    from app.config.risk_params import load_risk_params
    from app.live_trader.utils import calc_trading_days
    from ..main import _resolve_instrument_name  # 与 positions 共用,cache 暂留 main

    store = _state.get("store")
    if not store:
        raise HTTPException(503, "未初始化")

    rp = load_risk_params()
    risk_params = {
        "hard_stop": rp.hard_stop,
        "trail_activate": rp.trail_activate,
        "trail_dd": rp.trail_dd,
        "take_profit_tiers": rp.take_profit_tiers,
        "time_exit_days": rp.time_exit_days,
        "time_exit_profit": rp.time_exit_profit,
        "time_force_days": rp.time_force_days,
        "first_day_exit_min_profit": rp.first_day_exit_min_profit,
        "first_day_exit_days": rp.first_day_exit_days,
        "use_atr_trail": rp.use_atr_trail,
        "atr_trail_multiplier": rp.atr_trail_multiplier,
    }
    if rp.use_atr_trail:
        risk_params["atr_note"] = "移动止盈基于ATR计算，显示与实际触发可能存在偏差"

    positions = store.get_positions() or []
    result_positions = []

    qmt = _state.get("qmt")

    for pos in positions:
        code = pos.get("code") or ""
        avg_cost = float(pos.get("avg_cost") or 0)
        volume = float(pos.get("volume") or 0)
        last_close = float(pos.get("last_close") or 0)
        entry_date = pos.get("entry_date")
        peak_price = float(pos.get("peak_price") or 0) if pos.get("peak_price") else None
        tp_triggered = pos.get("tp_triggered") or "[]"

        if avg_cost <= 0 or volume <= 0:
            continue

        # 基础数据
        import dataclasses
        risk_items = []

        # 统一计算 holding_days（HS/FD/TF/TC/TP 共用，避免 NameError）
        holding_days = calc_trading_days(entry_date) if entry_date else 1
        profit_rate = float(pos.get("profit_rate", 0) or 0)  # 防御 None/"" → 0

        # ----- HS 硬止损 -----
        hard_stop_pct = rp.hard_stop * 100  # 如 -6.0
        # T+1 保护：持仓不足2天不触发硬止损
        if holding_days < 2:
            hs_status = "safe"
            hs_message = "T+1保护，持仓不足2天不触发硬止损"
            risk_items.append({
                "type": "HS", "label": "硬止损",
                "trigger_value": hard_stop_pct,
                "current_pnl": profit_rate,
                "remaining": abs(hard_stop_pct),   # safe=0% 进度条
                "budget": abs(hard_stop_pct),
                "status": hs_status,
                "message": hs_message,
            })
        else:
            hs_triggered = profit_rate <= hard_stop_pct
            if hs_triggered:
                hs_remaining = 0.0
                hs_status = "danger"
                hs_message = f"已触发硬止损（当前{profit_rate:.1f}% < 止损线{hard_stop_pct:.1f}%）"
            else:
                hs_remaining = abs(hard_stop_pct - profit_rate)  # 离触发还差多少百分点（正数）
                hs_status = "safe"
                hs_message = f"距硬止损 {hard_stop_pct:.1f}% 还差 {hs_remaining:.1f}%"
            risk_items.append({
                "type": "HS", "label": "硬止损",
                "trigger_value": hard_stop_pct,
                "current_pnl": profit_rate,
                "remaining": hs_remaining,
                "budget": abs(hard_stop_pct),
                "status": hs_status,
                "message": hs_message,
            })

        # ----- TR 移动止盈 -----
        trail_dd_pct = rp.trail_dd * 100  # 如 2.0
        if peak_price and peak_price > 0 and avg_cost > 0:
            peak_pnl_pct = (peak_price - avg_cost) / avg_cost * 100
            current_pnl_pct = profit_rate
            drawdown = peak_pnl_pct - current_pnl_pct  # 回撤百分点
            tr_triggered = drawdown >= trail_dd_pct
        else:
            peak_pnl_pct = 0.0
            drawdown = 0.0
            tr_triggered = False
        if tr_triggered:
            tr_remaining = 0.0
            tr_status = "warning"
            tr_message = f"已触发移动止盈（回撤{drawdown:.1f}% > 阈值{trail_dd_pct:.1f}%）"
        else:
            tr_remaining = trail_dd_pct - drawdown if drawdown >= 0 else abs(drawdown)
            tr_status = "safe" if drawdown < 0 else "safe"
            tr_message = f"移动止盈未激活，回撤{drawdown:.1f}%，距触发还差 {max(0, tr_remaining):.1f}%"
        risk_items.append({
            "type": "TR", "label": "移动止盈",
            "trigger_value": -trail_dd_pct,
            "activated": peak_pnl_pct > 0,
            "peak_pnl": peak_pnl_pct,
            "current_pnl": current_pnl_pct,
            "drawdown": drawdown,
            "remaining": max(0, tr_remaining),
            "budget": trail_dd_pct,   # M-V4-2
            "status": tr_status,
            "message": tr_message,
        })

        # ----- TF 强制清仓 -----
        tf_trigger_days = rp.time_force_days
        tf_remaining = max(0, tf_trigger_days - holding_days)
        tf_status = "danger" if tf_remaining <= 0 else "safe"
        tf_message = f"持仓第{holding_days}天/{tf_trigger_days}天，{'已到期' if tf_remaining <= 0 else f'距TF到期还{tf_remaining}天'}"
        risk_items.append({
            "type": "TF", "label": "强制清仓",
            "trigger_days": tf_trigger_days,
            "current_days": holding_days,
            "remaining_days": tf_remaining,
            "remaining": tf_remaining,
            "budget": tf_trigger_days,   # M-V4-2
            "status": tf_status,
            "message": tf_message,
        })

        # ----- FD 首日离场 -----
        fd_threshold = rp.first_day_exit_min_profit * 100
        fd_effective_days = rp.first_day_exit_days
        fd_triggered = holding_days <= fd_effective_days and profit_rate < fd_threshold
        fd_status = "warning" if fd_triggered else "safe"
        fd_message = f"目标涨幅≥{fd_threshold}%，当前{profit_rate:.1f}%，{'已触发' if fd_triggered else '无需处理'}"
        risk_items.append({
            "type": "FD", "label": "首日离场",
            "trigger_profit": fd_threshold,
            "effective_days": fd_effective_days,
            "status": fd_status,
            "message": fd_message,
        })

        # ----- TC 时间条件退出 -----
        tc_days = rp.time_exit_days
        tc_profit_threshold = rp.time_exit_profit * 100
        tc_remaining = max(0, tc_days - holding_days)
        tc_status = "warning" if tc_remaining <= 0 and profit_rate >= tc_profit_threshold else "safe"
        tc_message = f"持仓第{holding_days}天/{tc_days}天，盈利需≥{tc_profit_threshold}%，当前{profit_rate:.1f}%"
        risk_items.append({
            "type": "TC", "label": "时间退出",
            "trigger_days": tc_days,
            "trigger_profit": tc_profit_threshold,
            "current_days": holding_days,
            "remaining_days": tc_remaining,
            "remaining": tc_remaining,
            "budget": tc_days,   # M-V4-2
            "status": tc_status,
            "message": tc_message,
        })

        # ----- TP 多档止盈 -----
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
            except Exception as e:
                logger.warning(f"TP tiers 解析失败 code={code} tp_triggered={tp_triggered!r}: {e}")
            if tp_triggered_flag:
                tp_remaining = 0.0
                tp_status = "warning"
                tp_message = f"止盈{i+1}档({tp_pct:.1f}%)已触发，卖出{tp_ratio:.0f}%"
            else:
                tp_remaining = tp_pct - profit_rate
                tp_status = "safe"
                tp_message = f"止盈{i+1}档({tp_pct:.1f}%)未触发，当前{profit_rate:.1f}%，距触发还差 {max(0, tp_remaining):.1f}%"
            risk_items.append({
                "type": f"TP{i+1}", "label": f"止盈{i+1}档",
                "trigger_value": tp_pct,
                "sell_ratio": tp_ratio,
                "triggered": tp_triggered_flag,
                "current_pnl": profit_rate,
                "remaining_to_trigger": tp_remaining,
                "remaining": max(0, tp_remaining),
                "budget": tp_pct,   # M-V4-2
                "status": tp_status,
                "message": tp_message,
            })

        # ----- 全局状态：按 exit_monitor 优先级取最高 -----
        STATUS_PRIORITY = {"danger": 3, "warning": 2, "safe": 1}
        global_status = max(risk_items, key=lambda x: STATUS_PRIORITY.get(x["status"], 0))["status"]

        result_positions.append({
            "code": code,
            "name": _resolve_instrument_name(code, qmt),
            "current_price": float(pos.get("last_price") or 0),  # 实时价:refresh_quotes 3s 写 + WS 覆盖,严格对齐持仓卡片现价列
            "avg_cost": avg_cost,
            "last_close": last_close,
            "volume": volume,
            "float_profit": float(pos.get("float_profit") or 0),
            "profit_rate": profit_rate,
            "entry_date": str(entry_date) if entry_date else None,
            "holding_days": holding_days,
            "peak_price": peak_price,
            "tp_triggered": tp_triggered,
            "risk_items": risk_items,
            "global_status": global_status,
        })

    return {
        "risk_params": risk_params,
        "max_sell_per_scan": 3,
        "positions": result_positions,
        "updated_at": datetime.now().isoformat(),
    }
