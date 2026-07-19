"""实盘交易 - 交易动作类路由(风险最高的一组)。

阶段 1 第 4 步(2026-07-19) 从 main.py 抽离,共 10 个路由:
  POST /live/kill-switch/activate     (原 main.py:513)
  POST /live/kill-switch/deactivate   (原 main.py:525)
  POST /live/reconcile                (原 main.py:535)
  POST /live/exit-scan                (原 main.py:544)
  GET  /live/audit/replay/{order_id}  (原 main.py:554)
  POST /live/order                    (原 main.py:563, 调 place_order_service)
  POST /live/order/cancel             (原 main.py:674)
  POST /live/buy-signal               (原 main.py:881, 调 process_buy_signals + _verify_token)
  POST /live/cancel-by-source         (原 main.py:923)
  GET  /live/buy-signal/pending       (原 main.py:964)

依赖策略(经 architect + code-reviewer 双审计修订):
  - 顶部 import: time/date(place_order 用 date.today()/time.time())、_state、
    auth(_require_admin + _verify_token)、fastapi、logger。不 import asyncio(并发在 service 内)。
  - 函数内 import: place_order_service / process_buy_signals(留 main,阶段 3 搬 services)
  - 审计 R5:相对 import 双点(from ..schemas / from ..price_type,搬子目录后多一层)
  - 审计 W1.1:place_order 路由的 dry-run 403 检查必须保留在 place_order_service 调用前,顺序不可改
  - 审计 W4.1:place_order_service(intent, source="WEB", lock_wait_sec=30) 调用签名一字不动
  - 审计 W1.3:buy-signal 路由整段含 HTTP 层 4 步(开关/鉴权/校验/kill_switch)
  - 审计 W4.2:cancel-by-source 的 seq>0 守卫必须保留
"""
import time
from datetime import date

from fastapi import APIRouter, HTTPException, Request

from core.logger import get_logger

from .._state import state as _state
from ..auth import _require_admin, _verify_token

logger = get_logger("live_trader.routers.trade")

router = APIRouter()


@router.post("/live/kill-switch/activate")
async def activate_killswitch(reason: str = "manual_web"):
    """激活 kill switch(Web 人工)。主开关禁用时拒绝激活。"""
    ks = _state.get("kill_switch")
    if not ks:
        raise HTTPException(503, "未初始化")
    if not ks.is_enabled():
        raise HTTPException(409, "kill switch 主开关已禁用,无法激活;先在控制面板开启急停功能")
    ks.activate(reason=reason, source="web")
    return {"activated": True, "reason": reason}


@router.post("/live/kill-switch/deactivate")
async def deactivate_killswitch():
    """解除 kill switch(需人工确认)"""
    ks = _state.get("kill_switch")
    if not ks:
        raise HTTPException(503, "未初始化")
    ok = ks.deactivate()
    return {"deactivated": ok}


@router.post("/live/reconcile")
async def reconcile():
    """手动触发对账"""
    r = _state.get("reconciler")
    if not r:
        raise HTTPException(503, "未初始化")
    return r.reconcile()


@router.post("/live/exit-scan")
async def exit_scan():
    """手动触发离场扫描"""
    em = _state.get("exit_monitor")
    if not em:
        raise HTTPException(503, "未初始化")
    actions = em.scan_once()
    return {"executed": len(actions), "actions": [{"code": a["code"], "trigger": a["trigger"]} for a in actions]}


@router.get("/live/audit/replay/{order_id}")
async def audit_replay(order_id: int):
    """审计回放(5分钟内可回放)"""
    audit = _state.get("audit")
    if not audit:
        raise HTTPException(503, "未初始化")
    return audit.replay(order_id)


@router.post("/live/order")
async def place_order(req: dict):
    """手动下单(薄壳,调用 place_order_service)

    2026-07-18 变更:
    - dry-run 模式硬拒 403(原为 mock 回报;buy-signal 链路走独立端点不受影响)
    - 支持 price_type_key 市价键(price_type.py 市场感知映射,warning 透传)
    - 市价单(price=0)先用 QMT 实时价回填估算基准,取不到行情 fail-closed 拒绝
      (防闸门1/2/3/4 按 volume×100 低估金额,风控形同虚设)
    """
    from ..schemas import OrderIntent, OrderRequest  # 审计 R5:双点(原 main 为单点)

    config = _state.get("config")
    if not config:
        raise HTTPException(503, "未初始化")

    # dry-run 硬拒(2026-07-18 用户确认:防模拟模式误下真单)
    # 审计 W1.1:此检查必须在 place_order_service 调用之前,顺序不可改
    runtime_state = _state.get("runtime_state")
    if runtime_state and not runtime_state.is_live():
        raise HTTPException(403, "dry-run 模式手工下单禁用(防误下真单)")

    # 参数校验
    try:
        order_req = OrderRequest(**req)
    except Exception as e:
        raise HTTPException(400, f"参数错误: {e}")

    # kill switch 检查
    kill_switch = _state.get("kill_switch")
    if kill_switch and kill_switch.is_active():
        raise HTTPException(403, "kill switch 已激活,禁止下单")

    # 市价键映射(2026-07-18 M1):有 key 时优先,市场感知降级 + warning
    price_type = order_req.price_type
    price_type_warning = None
    if order_req.price_type_key:
        from ..price_type import map_price_type  # 审计 R5:双点
        try:
            price_type, price_type_warning = map_price_type(
                order_req.price_type_key, order_req.code)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # 市价单金额估算(HIGH-1 修复):price=0 时用 QMT 实时价回填估算基准
    price = order_req.price
    if price_type == 11 and price <= 0:
        # 限价单必须给价(前端有校验,这里是后端兜底)
        raise HTTPException(400, "限价单必须填写价格(price > 0)")
    if price_type != 11 and price <= 0:  # 非限价且未给价
        qmt = _state.get("qmt")
        from app.utils.xtquant_compat import format_code
        code_fmt = format_code(order_req.code.split(".")[0])
        last_price = 0.0
        if qmt and qmt.connected:
            try:
                quotes = qmt.get_realtime_quotes([code_fmt])
                last_price = float(quotes.get(code_fmt, {}).get("lastPrice", 0) or 0)
            except Exception:
                last_price = 0.0
        if last_price <= 0:
            raise HTTPException(
                503, f"市价单无法取得 {code_fmt} 实时行情,fail-closed 拒绝下单")
        price = last_price
        logger.info(f"市价单金额估算基准: {code_fmt} QMT实时价 {price}")

    # 构造 OrderIntent
    import hashlib
    client_order_id = hashlib.md5(
        f"{order_req.strategy_name or 'manual'}|{order_req.code}|"
        f"{date.today()}|{order_req.direction}|{int(time.time() * 1000)}".encode()
    ).hexdigest()[:16]

    intent = OrderIntent(
        code=order_req.code,
        direction=order_req.direction,
        volume=order_req.volume,
        price=price,
        price_type=price_type,
        strategy_name=order_req.strategy_name or "manual",
        terminal=order_req.terminal,
        client_order_id=client_order_id,
        reason=f"手动下单({order_req.terminal})",
    )

    # 调用 service(审计 W4.1:source="WEB"/lock_wait_sec=30 一字不动,与 _process_one_signal 的 TDX/5s 不同)
    from ..main import place_order_service  # 留 main,阶段 3 搬 services/order_service.py
    result = place_order_service(intent, source="WEB", lock_wait_sec=30)
    if price_type_warning:
        result["price_type_warning"] = price_type_warning
    return result


@router.post("/live/order/cancel")
async def cancel_order(request: Request, body: dict):
    """撤单(2026-07-18 手工下单功能 M3)

    - dry-run → 403(与手工单一致)
    - kill_switch 激活 → 放行:撤单是减风险操作,不在"停新单"语义内
    - qmt.cancel_order → 0 成功 / -1 断开 / -3 未找到
    """
    _require_admin(request)
    runtime_state = _state.get("runtime_state")
    if runtime_state and not runtime_state.is_live():
        raise HTTPException(403, "dry-run 模式撤单禁用")
    qmt = _state.get("qmt")
    if not qmt or not qmt.connected:
        raise HTTPException(503, "QMT 未连接,无法撤单")

    order_id = body.get("order_id")
    if not order_id:
        raise HTTPException(400, "缺少 order_id")
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        raise HTTPException(400, f"order_id 非法: {order_id}")

    try:
        ret = qmt.cancel_order(order_id)
    except Exception as e:
        logger.error(f"撤单异常 oid={order_id}: {e}")
        raise HTTPException(500, f"撤单异常: {e}")

    ok = (ret == 0)
    reason = {0: "撤单成功", -1: "QMT 连接断开", -3: "未找到订单"}.get(ret, f"QMT 返回 {ret}")
    audit = _state.get("audit")
    if audit:
        audit.log("order_cancel", order_id=order_id, reason=reason)
    logger.info(f"撤单 oid={order_id} ret={ret} {reason}")
    return {"ok": ok, "order_id": order_id, "reason": reason}


@router.post("/live/buy-signal")
async def buy_signal(req: dict, authorization: str = ""):
    """买入信号批量接收端点(API 服务→实盘服务)

    v1.2.2:鉴权 + 去重 + 时点策略 + 并发信号量3 + 心跳
    """
    from ..schemas import BuySignalRequest  # 审计 R5:双点

    config = _state.get("config")

    if not config:
        raise HTTPException(503, "未初始化")

    # 信号开关检查(v2 A9: 运行时开关,从 runtime_state 读) — HTTP 层 4 步之一
    runtime_state = _state.get("runtime_state")
    _buy_enabled = runtime_state.buy_enabled if runtime_state else config.buy_signal_enabled
    if not _buy_enabled:
        raise HTTPException(403, "buy_signal 未启用")

    # 鉴权(从 Header 读取 Authorization) — HTTP 层 4 步之二
    if not _verify_token(authorization, config):
        raise HTTPException(401, "鉴权失败:token 不匹配")

    # 参数校验 — HTTP 层 4 步之三
    try:
        signal_req = BuySignalRequest(**req)
    except Exception as e:
        raise HTTPException(400, f"参数错误: {e}")

    # kill switch 检查 — HTTP 层 4 步之四
    kill_switch = _state.get("kill_switch")
    if kill_switch and kill_switch.is_active():
        raise HTTPException(403, "kill switch 已激活,禁止接收信号")

    # 业务内核委托共享函数(去重/cutoff/并发/心跳/幂等 全在 process_buy_signals)
    # HTTP 端点与 scheduler 自给自足同源,保证副作用一致(尤其心跳,防 14:55 看门狗误报)
    from ..main import process_buy_signals  # 留 main,阶段 3 搬 services/signal_service.py
    _strategy = getattr(signal_req, 'strategy', 'QUANTQQ') or 'QUANTQQ'
    return await process_buy_signals(
        signal_req.signals, strategy=_strategy, source=signal_req.source,
    )


@router.post("/live/cancel-by-source")
async def cancel_by_source(terminal: str = "TDX"):
    """按来源批量撤单(v1.2.2 §4 缺陷15)

    撤销指定 terminal 的所有在途委托。
    """
    store = _state.get("store")
    qmt = _state.get("qmt")
    if not store or not qmt:
        raise HTTPException(503, "未初始化")

    from app.utils.xtquant_compat import ORDER_STATUS_INFLIGHT, ORDER_TYPE_SELL
    inflight = store.get_inflight_orders()
    cancelled = []
    skipped = []

    for order in inflight:
        if order.get("terminal") != terminal:
            continue
        # 跳过已成交的单子
        if order.get("status") in (56, 53):  # 已成/部撤
            skipped.append(order.get("code", ""))
            continue
        try:
            seq = order.get("seq", 0)
            if seq > 0 and qmt.connected:  # 审计 W4.2:seq>0 守卫必须保留,漏 = cancel_order(0) 未定义行为
                ret = qmt.cancel_order(seq)
                # 0=成功, -1=断开, -3=未找到, 其他=失败
                if ret == 0:
                    cancelled.append(order.get("code", ""))
                    logger.info(f"撤单成功: {order.get('code')} seq={seq} terminal={terminal}")
                else:
                    skipped.append(order.get("code", ""))
                    logger.warning(f"撤单未成功: {order.get('code')} seq={seq} ret={ret}")
        except Exception as e:
            skipped.append(order.get("code", ""))
            logger.error(f"撤单失败: {order.get('code')} seq={seq}: {e}")

    return {"cancelled": cancelled, "skipped": skipped, "terminal": terminal}


@router.get("/live/buy-signal/pending")
async def pending_signals():
    """拉取待处理信号(Windows 端主动拉取模式,备用)"""
    # 当前采用信号推送模式(API 服务定时选股后推送),pending 端点预留
    return {"pending": [], "note": "当前为推送模式,无待拉取信号"}
