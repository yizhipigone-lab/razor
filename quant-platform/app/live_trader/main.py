"""实盘交易模块 FastAPI 入口(v5.4 §10)

路由聚合 + 生命周期管理 + 启动互斥(§16.8)。
端口 8001,NSSM 守护。
"""
import asyncio
import os
import socket
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.logger import get_logger

logger = get_logger("live_trader.main")


# ===== 全局组件(生命周期管理)=====
_state: dict = {}


def _check_port_in_use(port: int) -> bool:
    """检查端口是否在监听(§16.8 启动互斥)"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _acquire_lock(lock_file: str) -> bool:
    """获取文件锁(§16.8)"""
    try:
        import portalocker
        os.makedirs(os.path.dirname(lock_file), exist_ok=True)
        fd = open(lock_file, "w")
        portalocker.lock(fd, portalocker.LOCK_EX | portalocker.LOCK_NB)
        _state["lock_fd"] = fd
        return True
    except ImportError:
        # portalocker 未装,用简单文件存在检查
        if os.path.exists(lock_file):
            return False
        os.makedirs(os.path.dirname(lock_file), exist_ok=True)
        with open(lock_file, "w") as f:
            f.write(str(os.getpid()))
        _state["lock_file"] = lock_file
        return True
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """生命周期:启动 + 关闭"""
    from .config import load_config
    from .store import LiveTraderStore
    from .notify import Notifier
    from .kill_switch import KillSwitch
    from .clearance_lock import ClearanceLock
    from .qmt_wrapper import QmtWrapper
    from .callback_handler import CallbackHandler
    from .connection_manager import ConnectionManager
    from .risk_gate import RiskGate
    from .pnl_engine import PnlEngine
    from .reconciler import Reconciler
    from .exit_monitor import ExitMonitor
    from .audit import AuditLogger
    from .scheduler import LiveScheduler

    # ===== 启动 =====
    logger.info("=" * 60)
    logger.info("实盘交易模块 live_trader 启动")
    logger.info("=" * 60)

    # 加载配置(fail-fast 校验)
    config = load_config()
    _state["config"] = config
    logger.info(f"模式: {config.mode} | 资金: {config.live_capital} | 账号: {config.qmt_account_id}")

    # 启动互斥(§16.8):检查 8081(qmt_proxy 已废弃)是否在跑(安全守卫)
    if _check_port_in_use(8081):
        msg = "检测到端口 8081 在监听(可能是旧的 qmt_proxy_server),请先停止(防 xtquant 资源池冲突)"
        logger.error(msg)
        raise RuntimeError(msg)

    # 文件锁
    if not _acquire_lock(config.lock_file):
        msg = f"无法获取文件锁 {config.lock_file},可能已有 live_trader 在跑"
        logger.error(msg)
        raise RuntimeError(msg)
    logger.info(f"文件锁获取: {config.lock_file}")

    # 初始化组件
    store = LiveTraderStore(config)
    notifier = Notifier(config.wework_webhook, config.multi_channel_alert)
    kill_switch = KillSwitch(config, store, notifier)
    clearance_lock = ClearanceLock(config)
    pnl_engine = PnlEngine(store)
    audit = AuditLogger(store)

    # 检查 kill switch 文件(上次崩溃可能留下)
    if kill_switch.is_active():
        logger.warning("⚠ 启动时检测到 kill switch 已激活,需人工解除后才下单")
        if notifier:
            notifier.kill_switch_activated("启动时检测到残留 kill switch", "startup_check")

    qmt = QmtWrapper(config)
    callback = CallbackHandler(config, store, kill_switch, clearance_lock, pnl_engine, notifier)
    conn = ConnectionManager(config, qmt, callback, kill_switch, store)
    risk_gate = RiskGate(config, store, kill_switch, qmt)
    reconciler = Reconciler(config, store, qmt, kill_switch, notifier, pnl_engine)
    exit_monitor = ExitMonitor(config, store, qmt, risk_gate, clearance_lock,
                                kill_switch, callback, audit, pnl_engine)

    # 注入依赖
    callback.kill_switch = kill_switch
    callback.clearance_lock = clearance_lock
    callback.pnl_engine = pnl_engine
    callback.notify = notifier

    # 连接 QMT
    try:
        conn.connect()
        # 启动恢复(§17.1)
        conn.reconcile_on_startup()
        # 持仓接管(§3.3.1)
        _takeover_positions(store, qmt, config, audit)
        # 清理 dry-run 残留(§18.3 严格保护:live 启动时校验无 dry-run 残留)
        _cleanup_dryrun_residue(store, config, audit)
        # 写入当日首条资产备份(为闸门5a日亏计算提供基准,§16.4)
        if qmt.connected:
            asset_data = qmt.query_asset()
            if asset_data:
                store.backup_asset(asset_data)
                logger.info(f"资产备份(闸门5a基准): total_asset={asset_data.get('total_asset', 0):.2f}")
    except Exception as e:
        logger.error(f"QMT 连接失败: {e}")
        kill_switch.activate(reason=f"QMT连接失败: {e}", source="startup")
        if notifier:
            notifier.kill_switch_activated(f"QMT连接失败: {e}", "startup")

    _state.update({
        "store": store, "notifier": notifier, "kill_switch": kill_switch,
        "clearance_lock": clearance_lock, "qmt": qmt, "callback": callback,
        "conn": conn, "risk_gate": risk_gate, "reconciler": reconciler,
        "exit_monitor": exit_monitor, "audit": audit, "pnl_engine": pnl_engine,
    })

    # 启动调度器(离场扫描 + 对账 + EOD 归档 + 非交易日检测)
    scheduler = LiveScheduler(
        config, store, qmt, exit_monitor, reconciler, kill_switch, notifier, audit
    )
    scheduler.start()
    _state["scheduler"] = scheduler

    logger.info("live_trader 启动完成")
    yield

    # ===== 关闭 =====
    logger.info("live_trader 关闭中...")
    try:
        scheduler.stop()
        conn.stop()
        qmt.stop()
        callback.stop()
        store.close()
        _kill_all_subprocesses()
        # 释放文件锁
        if "lock_fd" in _state:
            _state["lock_fd"].close()
        if "lock_file" in _state and os.path.exists(_state["lock_file"]):
            os.remove(_state["lock_file"])
    except Exception as e:
        logger.error(f"关闭异常: {e}")
    logger.info("live_trader 已关闭")


def _takeover_positions(store, qmt, config, audit) -> None:
    """持仓接管(§3.3.1):分类 managed=true/false"""
    if not qmt.connected:
        return
    qmt_positions = qmt.query_positions()
    if not qmt_positions:
        logger.info("持仓接管:QMT 无持仓")
        return

    preserved = set(config.preserved_codes)
    for pos in qmt_positions:
        code = pos.get("code", "")
        if not code:
            continue
        from app.utils.xtquant_compat import format_code, strip_code_suffix
        code_fmt = format_code(code) if '.' not in code else code
        managed = code_fmt not in preserved and strip_code_suffix(code_fmt) not in [strip_code_suffix(p) for p in preserved]

        existing = store.get_position(code_fmt)
        pos_data = {
            "code": code_fmt,
            "volume": pos.get("volume", 0),
            "can_use_volume": pos.get("can_use_volume", 0),
            "frozen_volume": pos.get("frozen_volume", 0),
            "pending_buy_volume": (existing or {}).get("pending_buy_volume", 0),
            "avg_cost": pos.get("avg_cost", 0),
            "last_price": pos.get("last_price", 0),
            "market_value": pos.get("market_value", 0),
            "float_profit": pos.get("profit", 0),
            "profit_rate": 0,
            "peak_price": (existing or {}).get("peak_price", 0) or pos.get("last_price", 0),
            "sell_count": (existing or {}).get("sell_count", 0),
            "entry_date": (existing or {}).get("entry_date"),
            "managed": managed,
            "strategy_name": (existing or {}).get("strategy_name", ""),
        }
        store.upsert_position(pos_data)
        tag = "保留(ETF)" if not managed else "策略"
        logger.info(f"持仓接管: {code_fmt} {tag} vol={pos.get('volume',0)}")
        audit.log("position_takeover", code=code_fmt, reason=f"{tag} vol={pos.get('volume',0)}")


def _cleanup_dryrun_residue(store, config, audit) -> None:
    """清理 dry-run 残留(§18.3 严格保护)

    live 启动时(或 dry-run 切 live 前),删除本地 live_positions 中
    QMT 实际没有但本地有的 managed=true 持仓(dry-run mock 下单残留)。

    规则:
    - 拉本地所有持仓 + QMT 实际持仓(QMT 查询做 3 次重试取并集,对抗
      xtquant 缓存抖动导致的"部分结果")
    - 本地 managed=true 但 QMT 没有的 → 候选残留
    - 保留持仓(managed=false,ETF)不动,即使 QMT 没有也不删(可能是临时查不到)
    - dry-run 模式:候选残留直接清理(mock 数据,安全)
    - live 模式:**绝不自动删除**(防止 QMT 查询抖动误删真实持仓,2026-07-02 事故),
      只告警 + 写 audit,人工核查后手动清理
    """
    if not store or not store._conn:
        return
    try:
        local_positions = store.get_positions()
        if not local_positions:
            return

        # QMT 实际持仓代码集合(3 次重试取并集,任一次查到即视为 QMT 有)
        qmt_codes = set()
        qmt = _state.get("qmt")
        if qmt and qmt.connected:
            from app.utils.xtquant_compat import format_code
            import time as _time
            for attempt in range(3):
                qmt_positions = qmt.query_positions()
                for p in (qmt_positions or []):
                    code = p.get("code", "")
                    if code:
                        qmt_codes.add(format_code(code) if '.' not in code else code)
                # 已覆盖本地全部 managed 代码即可提前结束
                local_managed = {p.get("code", "") for p in local_positions if p.get("managed", True)}
                if local_managed <= qmt_codes:
                    break
                if attempt < 2:
                    _time.sleep(0.2)

        # 找候选残留:本地 managed=true 但 QMT 三次都没查到
        residue = []
        for p in local_positions:
            code = p.get("code", "")
            managed = p.get("managed", True)
            if managed and code not in qmt_codes:
                residue.append(code)

        if not residue:
            logger.info("dry-run 残留清理:无残留")
            return

        # dry-run 模式:清理残留委托和成交(mock 数据),并删残留持仓
        if config.mode == "dry-run":
            for code in residue:
                vol = next((p.get("volume", 0) for p in local_positions if p.get("code") == code), 0)
                store._conn.execute("DELETE FROM live_positions WHERE code = ?", [code])
                audit.log("dryrun_residue_cleanup", code=code,
                         reason=f"QMT无此持仓,本地残留已删除 vol={vol}")
                logger.warning(f"清理 dry-run 残留持仓: {code}")
            store._conn.execute("DELETE FROM live_orders WHERE mode = 'dry-run'")
            store._conn.execute("DELETE FROM live_deals WHERE mode = 'dry-run'")
            logger.info("dry-run 模式:清理 dry-run 残留委托和成交")
            logger.info(f"dry-run 残留清理完成:删除 {len(residue)} 个残留持仓")
            return

        # live 模式:绝不自动删除(防误删真实持仓),只告警 + audit
        for code in residue:
            vol = next((p.get("volume", 0) for p in local_positions if p.get("code") == code), 0)
            # 是否有 live 成交记录(有则更可能是真实持仓,强力提示人工核查)
            has_live_deal = False
            try:
                row = store._conn.execute(
                    "SELECT 1 FROM live_deals WHERE code = ? AND mode = 'live' LIMIT 1", [code]
                ).fetchone()
                has_live_deal = bool(row)
            except Exception:
                pass
            audit.log("live_residue_suspect", code=code,
                      reason=f"QMT三次未查到但本地 managed 持仓 vol={vol} live_deal={has_live_deal}(未自动删除,待人工核查)")
            logger.error(
                f"live 模式疑似残留(未自动删除): {code} vol={vol} "
                f"live_deal={has_live_deal} —— 需人工核查(QMT 查询可能抖动,或为 dry-run 残留)"
            )

        notifier = _state.get("notifier")
        kill_switch = _state.get("kill_switch")
        reason = (f"live启动发现{len(residue)}个疑似残留持仓(未自动删除): "
                  f"{', '.join(residue)}。QMT三次查询未覆盖,需人工核查")
        if notifier:
            try:
                notifier.kill_switch_activated(reason, "live_residue_check")
            except Exception:
                pass
        if kill_switch and not kill_switch.is_active():
            kill_switch.activate(reason=reason, source="live_residue_check")
        logger.error(f"live 模式发现 {len(residue)} 个疑似残留持仓(未自动删除,已激活 kill switch,需人工核查): {residue}")
    except Exception as e:
        logger.error(f"dry-run 残留清理失败: {e}")


# ===== FastAPI app =====

app = FastAPI(
    title="p9 实盘交易模块",
    version="5.4",
    description="实盘交易 live_trader(v5.4)",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://localhost:8888",
        "http://127.0.0.1:5173", "http://127.0.0.1:8888",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== 路由 =====

@app.get("/live/status")
async def status():
    """实盘总览状态"""
    config = _state.get("config")
    qmt = _state.get("qmt")
    ks = _state.get("kill_switch")
    store = _state.get("store")
    return {
        "mode": config.mode if config else "unknown",
        "qmt_connected": qmt.connected if qmt else False,
        "kill_switch": ks.status() if ks else {"activated": False},
        "live_capital": config.live_capital if config else 0,
        "account_id": config.qmt_account_id if config else "",
    }


@app.get("/live/asset")
async def asset():
    """资金查询"""
    qmt = _state.get("qmt")
    if not qmt or not qmt.connected:
        raise HTTPException(503, "QMT 未连接")
    return qmt.query_asset()


@app.get("/live/positions")
async def positions():
    """持仓查询(含 managed 标记)"""
    store = _state.get("store")
    if not store:
        raise HTTPException(503, "Store 未初始化")
    return store.get_positions(managed_only=False)


@app.get("/live/orders")
async def orders(limit: int = 50):
    """委托查询"""
    store = _state.get("store")
    if not store or not store._conn:
        raise HTTPException(503, "Store 未初始化")
    rows = store._conn.execute(
        "SELECT * FROM live_orders ORDER BY created_at DESC LIMIT ?", [limit]
    ).fetchall()
    cols = [d[0] for d in store._conn.description]
    return [dict(zip(cols, r)) for r in rows]


@app.get("/live/deals")
async def deals(limit: int = 50):
    """成交查询"""
    store = _state.get("store")
    return store.get_deals(limit=limit) if store else []


@app.get("/live/quotes")
async def quotes(codes: str):
    """行情查询(API 服务端 qmt_gateway 调用,替代旧 qmt_proxy:8081)"""
    qmt = _state.get("qmt")
    if not qmt:
        raise HTTPException(503, "QMT 未初始化")
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    return qmt.get_realtime_quotes(code_list)


@app.post("/live/kill-switch/activate")
async def activate_killswitch(reason: str = "manual_web"):
    """激活 kill switch(Web 人工)"""
    ks = _state.get("kill_switch")
    if not ks:
        raise HTTPException(503, "未初始化")
    ks.activate(reason=reason, source="web")
    return {"activated": True, "reason": reason}


@app.post("/live/kill-switch/deactivate")
async def deactivate_killswitch():
    """解除 kill switch(需人工确认)"""
    ks = _state.get("kill_switch")
    if not ks:
        raise HTTPException(503, "未初始化")
    ok = ks.deactivate()
    return {"deactivated": ok}


@app.post("/live/reconcile")
async def reconcile():
    """手动触发对账"""
    r = _state.get("reconciler")
    if not r:
        raise HTTPException(503, "未初始化")
    return r.reconcile()


@app.post("/live/exit-scan")
async def exit_scan():
    """手动触发离场扫描"""
    em = _state.get("exit_monitor")
    if not em:
        raise HTTPException(503, "未初始化")
    actions = em.scan_once()
    return {"executed": len(actions), "actions": [{"code": a["code"], "trigger": a["trigger"]} for a in actions]}


@app.get("/live/audit/replay/{order_id}")
async def audit_replay(order_id: int):
    """审计回放(5分钟内可回放)"""
    audit = _state.get("audit")
    if not audit:
        raise HTTPException(503, "未初始化")
    return audit.replay(order_id)


@app.post("/live/order")
async def place_order(req: dict):
    """手动下单(薄壳,调用 place_order_service)

    仅 live 模式允许;dry-run 模式走 mock 回报。
    """
    from .schemas import OrderIntent, OrderRequest

    config = _state.get("config")
    if not config:
        raise HTTPException(503, "未初始化")

    # 参数校验
    try:
        order_req = OrderRequest(**req)
    except Exception as e:
        raise HTTPException(400, f"参数错误: {e}")

    # kill switch 检查
    kill_switch = _state.get("kill_switch")
    if kill_switch and kill_switch.is_active():
        raise HTTPException(403, "kill switch 已激活,禁止下单")

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
        price=order_req.price,
        price_type=order_req.price_type,
        strategy_name=order_req.strategy_name or "manual",
        terminal=order_req.terminal,
        client_order_id=client_order_id,
        reason=f"手动下单({order_req.terminal})",
    )

    # 调用 service(手动模式:source=WEB, lock_wait=30s)
    return place_order_service(intent, source="WEB", lock_wait_sec=30)


def place_order_service(intent, source: str = "WEB", lock_wait_sec: int = 30) -> dict:
    """下单核心逻辑(幂等→风控→清仓锁→下单→写DB)

    供 /live/order 和 /live/buy-signal 共用。

    Args:
        intent: OrderIntent 下单意图
        source: 下单来源 "WEB"/"TDX"/"SCHEDULER",决定价格策略和 terminal 标记
        lock_wait_sec: 清仓锁等待秒数(手动30s, buy-signal 5s)

    Returns:
        dict 下单结果 {"ok", "order_id", "client_order_id", "status", "reason", ...}
    """
    from .schemas import OrderIntent
    from app.utils.xtquant_compat import (
        ORDER_TYPE_BUY, ORDER_TYPE_SELL, format_code,
    )

    config = _state.get("config")
    store = _state.get("store")
    qmt = _state.get("qmt")
    risk_gate = _state.get("risk_gate")
    clearance_lock = _state.get("clearance_lock")
    kill_switch = _state.get("kill_switch")
    callback_handler = _state.get("callback")
    audit_logger = _state.get("audit")
    notifier = _state.get("notifier")

    if not config:
        return {"ok": False, "status": "error", "reason": "未初始化"}

    # kill switch(二次检查,buy-signal 端点可能已在入口检查过)
    if kill_switch and kill_switch.is_active():
        return {"ok": False, "status": "forbidden", "reason": "kill switch 已激活"}

    # C3 幂等检查
    if store and intent.client_order_id:
        existing = store.get_order_by_client_id(intent.client_order_id)
        if existing:
            return {
                "ok": True, "order_id": existing.get("order_id"),
                "client_order_id": intent.client_order_id,
                "status": "duplicate", "reason": "幂等命中,不重复下单",
            }

    # 格式化代码
    code_fmt = format_code(intent.code) if '.' not in intent.code else intent.code

    # source=TDX 时,用 QMT 实时价覆盖信号源传的价格
    actual_price = intent.price
    if source == "TDX" and qmt and qmt.connected:
        try:
            quotes = qmt.get_realtime_quotes([code_fmt])
            qmt_price = quotes.get(code_fmt, {}).get("lastPrice", 0)
            if qmt_price and qmt_price > 0:
                actual_price = float(qmt_price)
                logger.info(f"TDX 信号价格覆盖: {intent.price} → QMT实时价 {actual_price}")
        except Exception as e:
            logger.warning(f"TDX 价格覆盖失败,用原始价格: {e}")

    # 拉行情+持仓+资产(给 RiskGate 用)
    asset = None
    positions = None
    quote = None
    if qmt and qmt.connected:
        try:
            asset = qmt.query_asset()
        except Exception:
            pass
        try:
            positions = store.get_positions(managed_only=True) if store else []
        except Exception:
            pass
        try:
            quotes = qmt.get_realtime_quotes([code_fmt])
            quote = quotes.get(code_fmt) if quotes else None
        except Exception:
            pass

    # RiskGate 闸门(含闸门10)
    if risk_gate:
        passed, gates, reason = risk_gate.check(intent, asset, positions, quote)
        if not passed:
            if audit_logger:
                audit_logger.gate_reject(code_fmt, intent.direction, reason)
            return {
                "ok": False, "client_order_id": intent.client_order_id,
                "code": code_fmt, "status": "risk_rejected", "reason": reason,
                "gates": gates,
            }
        if audit_logger:
            audit_logger.gate_pass(code_fmt, intent.direction,
                                   str([g for g in gates if g.get("passed")]))

    # ClearanceLock(支持 lock_wait_sec 参数)
    lock_acquired = False
    if clearance_lock:
        lock_acquired = clearance_lock.acquire_with_wait(
            code_fmt, timeout_sec=lock_wait_sec
        )
        if not lock_acquired:
            reason_msg = f"{code_fmt} 清仓锁冲突,等待{lock_wait_sec}s未获取"
            if source == "TDX":
                # buy-signal 批量模式:跳过+告警,不阻塞其他信号
                logger.warning(f"信号跳过: {reason_msg}")
                if notifier:
                    try:
                        notifier.send(f"⚠ 信号跳过: {reason_msg}")
                    except Exception:
                        pass
                return {
                    "ok": False, "client_order_id": intent.client_order_id,
                    "code": code_fmt, "status": "locked", "reason": reason_msg,
                }
            return {
                "ok": False, "client_order_id": intent.client_order_id,
                "code": code_fmt, "status": "locked", "reason": reason_msg,
            }

    try:
        # 下单(不等 callback,只等 seq 返回 — 漏洞D修复)
        if config.mode == "dry-run":
            if not callback_handler:
                return {"ok": False, "status": "error", "reason": "callback_handler 未初始化"}
            order_id = callback_handler.mock_order_async_response(
                intent.client_order_id, code_fmt, intent.direction,
                intent.volume, actual_price, intent.price_type,
                intent.strategy_name, intent.reason,
            )
        else:
            if not qmt or not qmt.connected:
                return {"ok": False, "status": "error", "reason": "QMT 未连接"}
            order_type = ORDER_TYPE_BUY if intent.direction == "buy" else ORDER_TYPE_SELL
            seq = qmt.order_stock_async(
                code_fmt, order_type, intent.volume,
                intent.price_type, actual_price,
                intent.strategy_name, intent.reason,
            )
            order_id = seq  # 实际 order_id 由 callback 回报

            # C1:买入成功后冻在途预扣
            if intent.direction == "buy" and seq > 0 and risk_gate:
                risk_gate.freeze_pending_buy(code_fmt, intent.volume)

        # 写入 live_orders
        if store:
            from datetime import datetime as dt
            now = dt.now()
            terminal = "TDX" if source == "TDX" else intent.terminal
            store.sync_terminal_write("order", {
                "order_id": order_id or 0,
                "client_order_id": intent.client_order_id,
                "code": code_fmt,
                "direction": intent.direction,
                "volume": intent.volume,
                "price": actual_price,
                "price_type": intent.price_type,
                "status": 50,  # 已报
                "status_msg": "已提交",
                "seq": order_id or 0,
                "mode": config.mode,
                "strategy_name": intent.strategy_name,
                "order_remark": intent.reason,
                "terminal": terminal,
                "created_at": now,
                "updated_at": now,
            })

        if audit_logger:
            audit_logger.order_placed(code_fmt, order_id, config.mode, {
                "direction": intent.direction, "volume": intent.volume,
                "price": actual_price, "price_type": intent.price_type,
                "source": source,
            })

        logger.info(f"下单成功 {code_fmt} {intent.direction} {intent.volume}@{actual_price}"
                    f" oid={order_id} mode={config.mode} source={source}")

        return {
            "ok": True, "order_id": order_id,
            "client_order_id": intent.client_order_id,
            "code": code_fmt,
            "status": "submitted", "reason": "",
            "mode": config.mode, "source": source,
        }

    except Exception as e:
        logger.error(f"下单异常 {intent.code}: {e}")
        if clearance_lock and lock_acquired:
            clearance_lock.release(code_fmt)
        return {"ok": False, "status": "error", "reason": f"下单异常: {e}"}


# ===== 信号桥接端点(v1.2.2 §5.2) =====

def _verify_token(auth_header: str, config) -> bool:
    """验证 Bearer token"""
    if not config or not config.buy_signal_token:
        return True  # 未配置 token 则不鉴权(向后兼容)
    if not auth_header:
        return False
    parts = auth_header.split(" ")
    if len(parts) != 2 or parts[0] != "Bearer":
        return False
    return parts[1] == config.buy_signal_token


async def _process_one_signal(signal, semaphore, lock_wait_sec: int = 5, strategy_name: str = "QUANTQQ") -> dict:
    """处理单个买入信号(在信号量控制下并发)"""
    from .schemas import OrderIntent
    from .buy_volume import _calc_buy_volume
    from app.utils.xtquant_compat import format_code

    async with semaphore:
        config = _state.get("config")
        store = _state.get("store")

        code = signal.code
        code_fmt = format_code(code) if '.' not in code else code

        # 幂等键:不带时间戳,同天同股唯一(漏洞9修复)
        import hashlib
        client_order_id = hashlib.md5(
            f"buy_signal|{code_fmt}|{date.today()}".encode()
        ).hexdigest()[:16]

        # 计算买入量:buy_position_size 是软上限,闸门1是硬上限(漏洞F)
        position_size = config.buy_position_size if config else 10000.0
        price = signal.price  # service 内 TDX source 会用 QMT 实时价覆盖

        # 先用传入价格估算 volume(如果是0则用默认估算价)
        if price <= 0:
            price = 10.0  # 兜底估算价

        # 计算买入量
        volume = _calc_buy_volume(code_fmt, position_size, price)
        if volume <= 0:
            return {
                "code": code_fmt, "ok": False,
                "status": "error", "reason": f"计算买入量为0(price={price}, size={position_size})",
            }

        # 尾盘价格策略(漏洞E修复):14:55-14:57 五档即成即撤,14:57+ 对手最优价
        from app.utils.xtquant_compat import (
            PRICE_TYPE_FIX, PRICE_TYPE_SH_5_CANCEL, PRICE_TYPE_SZ_5_CANCEL,
            PRICE_TYPE_PEER_FIRST,
        )
        now_str = datetime.now().strftime("%H:%M")
        if now_str >= "14:57":
            price_type = PRICE_TYPE_PEER_FIRST  # 对手方最优
        elif now_str >= "14:55":
            # 五档即成即撤:沪市42,深市47
            bare = code.split(".")[0] if "." in code else code
            price_type = PRICE_TYPE_SH_5_CANCEL if bare.startswith("6") else PRICE_TYPE_SZ_5_CANCEL
        else:
            price_type = PRICE_TYPE_FIX

        # 确定买入价格(尾盘用 0 让 price_type 决定)
        if price_type != PRICE_TYPE_FIX:
            order_price = 0  # 非限价单不需要价格
        else:
            order_price = price

        intent = OrderIntent(
            code=code_fmt,
            direction="buy",
            volume=volume,
            price=order_price,
            price_type=price_type,
            strategy_name=strategy_name,
            terminal="TDX",
            client_order_id=client_order_id,
            reason=f"TDX选股买入信号",
        )

        # 调用 service(TDX source, lock_wait=5s)
        # 用 asyncio.to_thread 避免阻塞事件循环(time.sleep in ClearanceLock)
        result = await asyncio.to_thread(
            place_order_service, intent, "TDX", lock_wait_sec
        )
        result["code"] = code_fmt
        return result


@app.post("/live/buy-signal")
async def buy_signal(req: dict, authorization: str = ""):
    """买入信号批量接收端点(API 服务→实盘服务)

    v1.2.2:鉴权 + 去重 + 时点策略 + 并发信号量3 + 心跳
    """
    from .schemas import BuySignalRequest, BuySignalResult, SignalResult

    config = _state.get("config")
    store = _state.get("store")
    notifier = _state.get("notifier")

    if not config:
        raise HTTPException(503, "未初始化")

    # 信号开关检查
    if not config.buy_signal_enabled:
        raise HTTPException(403, "buy_signal 未启用")

    # 鉴权(从 Header 读取 Authorization)
    if not _verify_token(authorization, config):
        raise HTTPException(401, "鉴权失败:token 不匹配")

    # 参数校验
    try:
        signal_req = BuySignalRequest(**req)
    except Exception as e:
        raise HTTPException(400, f"参数错误: {e}")

    # kill switch 检查
    kill_switch = _state.get("kill_switch")
    if kill_switch and kill_switch.is_active():
        raise HTTPException(403, "kill switch 已激活,禁止接收信号")

    # 时点检查(漏洞E修复 + §10.8):超过 buy_signal_cutoff 拒收
    now_str = datetime.now().strftime("%H:%M")
    cutoff = config.buy_signal_cutoff
    if now_str >= cutoff:
        reason = f"尾盘已过({now_str} ≥ {cutoff}),信号丢弃"
        logger.warning(reason)
        return BuySignalResult(
            accepted=[], rejected=[s.code for s in signal_req.signals],
            details=[SignalResult(code=s.code, ok=False, status="timeout", reason=reason)
                     for s in signal_req.signals]
        )

    # 漏洞A修复:信号去重(同 code 只保留第一条,用 format_code 统一 key)
    from app.utils.xtquant_compat import format_code
    seen_codes = set()
    unique_signals = []
    for s in signal_req.signals:
        code_key = format_code(s.code) if '.' not in s.code else s.code
        if code_key not in seen_codes:
            seen_codes.add(code_key)
            unique_signals.append(s)

    if len(unique_signals) < len(signal_req.signals):
        logger.info(f"信号去重: {len(signal_req.signals)} → {len(unique_signals)}")

    # 并发处理:信号量3(§10.1)
    # 策略名从请求 payload 传入(由 cron_jobs/sim_trader 动态填充)
    _strategy = getattr(signal_req, 'strategy', 'QUANTQQ') or 'QUANTQQ'
    semaphore = asyncio.Semaphore(3)
    tasks = [_process_one_signal(s, semaphore, lock_wait_sec=5, strategy_name=_strategy) for s in unique_signals]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 汇总结果
    accepted = []
    rejected = []
    details = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            code = unique_signals[i].code
            rejected.append(code)
            details.append(SignalResult(code=code, ok=False, status="error", reason=str(r)))
        else:
            code = r.get("code", unique_signals[i].code)
            if r.get("ok"):
                accepted.append(code)
            else:
                rejected.append(code)
            details.append(SignalResult(
                code=code,
                ok=r.get("ok", False),
                status=r.get("status", ""),
                reason=r.get("reason", ""),
                order_id=r.get("order_id"),
            ))

    # 心跳记录
    if store:
        try:
            scan_status = "ok" if len(accepted) > 0 else ("no_signal" if len(unique_signals) == 0 else "all_rejected")
            store.record_heartbeat("docker_tdx", len(unique_signals), scan_status)
        except Exception as e:
            logger.warning(f"心跳记录失败: {e}")

    logger.info(f"buy-signal 处理完成: accepted={accepted} rejected={rejected}")

    return BuySignalResult(accepted=accepted, rejected=rejected, details=details)


@app.post("/live/cancel-by-source")
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
            if seq > 0 and qmt.connected:
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


@app.get("/live/buy-signal/pending")
async def pending_signals():
    """拉取待处理信号(Windows 端主动拉取模式,备用)"""
    # 当前采用信号推送模式(API 服务定时选股后推送),pending 端点预留
    return {"pending": [], "note": "当前为推送模式,无待拉取信号"}

@app.post("/live/sync-positions")
async def sync_positions():
    """从 QMT 重新同步持仓到 live_positions(修复缺失持仓)"""
    qmt = _state.get("qmt")
    store = _state.get("store")
    audit = _state.get("audit")
    config = _state.get("config")
    if not qmt or not qmt.connected:
        raise HTTPException(503, "QMT 未连接")
    if not store:
        raise HTTPException(503, "Store 未初始化")
    _takeover_positions(store, qmt, config, audit)
    positions = store.get_positions()
    return {"synced": len(positions), "positions": [
        {"code": p.get("code"), "volume": p.get("volume"),
         "managed": p.get("managed")} for p in positions
    ]}


@app.get("/live/health")
async def health():
    """健康检查(NSSM 用)"""
    return {"status": "ok", "ts": datetime.now().isoformat()}


# ===== 子进程生命周期管理(从 qmt_proxy_server.py 迁移) =====

import subprocess
import sys
import threading as _threading

_spawned_processes: list = []
_spawned_lock = _threading.Lock()


def _cleanup_zombies():
    """清理已退出的子进程"""
    with _spawned_lock:
        alive = [p for p in _spawned_processes if p.poll() is None]
        _spawned_processes.clear()
        _spawned_processes.extend(alive)


def _kill_all_subprocesses():
    """终止所有子进程(服务关闭时调用)"""
    with _spawned_lock:
        for p in _spawned_processes:
            try:
                p.kill()
                p.wait(timeout=3)
            except Exception:
                pass
        _spawned_processes.clear()


# ===== 从 qmt_proxy_server.py 迁移的端点 =====

@app.get("/live/stocklist")
async def stocklist(details: bool = False, codes: str = ""):
    """获取 QMT 全市场股票列表(替代 qmt_proxy /api/stocklist)"""
    qmt = _state.get("qmt")
    if not qmt:
        raise HTTPException(503, "QMT 未初始化")

    try:
        markets = ['上证A股', '深证A股']
        try:
            test_codes = qmt.get_stock_list_in_sector('北证A股')
            if test_codes:
                markets.append('北证A股')
        except Exception:
            pass

        all_codes = []
        for m in markets:
            sector_codes = qmt.get_stock_list_in_sector(m)
            if sector_codes:
                all_codes.extend(sector_codes)
        all_codes = sorted(set(all_codes))
    except Exception as e:
        logger.error(f"获取QMT股票列表失败: {e}")
        return {"status": "error", "message": str(e)}

    if not details:
        return {"status": "ok", "count": len(all_codes), "codes": all_codes}

    # 筛选特定代码(增量详情查询)
    target = [c.strip() for c in codes.split(",") if c.strip()] if codes else all_codes

    stocks = []
    for code in target:
        d = qmt.get_instrument_detail(code)
        open_date = d.get("OpenDate", "")
        if open_date:
            od_str = str(open_date)
            open_date = f"{od_str[:4]}-{od_str[4:6]}-{od_str[6:]}" if len(od_str) == 8 else ""
        stocks.append({
            "code": code,
            "name": d.get("InstrumentName", ""),
            "sector": d.get("ProductName", ""),
            "list_date": open_date,
        })

    return {"status": "ok", "count": len(stocks), "stocks": stocks}


@app.get("/live/index/members")
async def index_members(index: str = "沪深300"):
    """获取指定指数的成分股列表(替代 qmt_proxy /api/index/members)"""
    qmt = _state.get("qmt")
    if not qmt:
        raise HTTPException(503, "QMT 未初始化")

    try:
        codes = qmt.get_stock_list_in_sector(index)
        if not codes:
            return {"status": "ok", "index": index, "count": 0, "codes": [], "stocks": []}

        codes = sorted(set(codes))
        stocks = []
        for c in codes:
            d = qmt.get_instrument_detail(c)
            stocks.append({
                "code": c,
                "name": d.get("InstrumentName", "") if d else "",
            })

        logger.info(f"index_members | {index}: 返回 {len(codes)} 只成分股")
        return {
            "status": "ok",
            "index": index,
            "count": len(codes),
            "codes": codes,
            "stocks": stocks,
        }
    except Exception as e:
        logger.error(f"获取指数 {index} 成分股失败: {e}")
        return {"status": "error", "index": index, "message": str(e)}


@app.post("/live/sync/intra")
async def sync_intra(req: dict):
    """分时数据同步(隔离子进程,替代 qmt_proxy /api/sync/intra)"""
    _cleanup_zombies()
    try:
        freq = req.get("freq", "5m")
        days = req.get("days", 30)
        start_date = req.get("start_date")
        end_date = req.get("end_date")

        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "qmt_sync_job.py"
        )
        cmd = [sys.executable, script_path, "--freq", freq, "--days", str(days)]
        if start_date:
            cmd.extend(["--start", start_date])
        if end_date:
            cmd.extend(["--end", end_date])

        logger.info(f"[TASK] Dispatching isolated worker: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd)
        with _spawned_lock:
            _spawned_processes.append(proc)
        return {"status": "dispatched_to_isolated_worker"}
    except Exception as e:
        logger.error(f"Failed to dispatch isolated worker: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/live/sync/index_daily")
async def sync_index_daily(req: dict):
    """指数日线同步(隔离子进程,替代 qmt_proxy /api/sync/index_daily)"""
    _cleanup_zombies()
    try:
        start_date = req.get("start_date")
        end_date = req.get("end_date")

        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "qmt_sync_index_job.py"
        )
        cmd = [sys.executable, script_path]
        if start_date:
            cmd.extend(["--start", start_date])
        if end_date:
            cmd.extend(["--end", end_date])

        logger.info(f"[TASK] Dispatching index daily sync worker: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd)
        with _spawned_lock:
            _spawned_processes.append(proc)
        return {"status": "dispatched_to_isolated_worker"}
    except Exception as e:
        logger.error(f"Failed to dispatch index daily sync worker: {e}")
        return {"status": "error", "message": str(e)}


# TODO: /live/quotes/subscribe (tick 订阅) — 暂缓
# 当前主服务 MarketBroadcaster 每 500ms 轮询 /live/quotes，已提供同等延迟的实时行情推送。
# 未来若需更低延迟的 push 模式，可实现 xtdata.subscribe_quote() + WebSocket 推送通道。


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
