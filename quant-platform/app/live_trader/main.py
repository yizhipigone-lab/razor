"""实盘交易模块 FastAPI 入口(v5.4 §10)

路由聚合 + 生命周期管理 + 启动互斥(§16.8)。
端口 8001,NSSM 守护。
"""
import asyncio
import os
import signal
import socket
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
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
    from .notifications import NotificationStore
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
    # v2(A6): 运行时状态(mode/开关)从 config 移出,支持运行时切换;启动单源(F6)
    from .runtime_state import load_runtime_state
    runtime_state = load_runtime_state(config)
    _state["runtime_state"] = runtime_state
    logger.info(f"模式: {runtime_state.mode} | 资金: {config.live_capital} | 账号: {config.qmt_account_id}")

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
    # 通知历史存储(独立 DuckDB, sync 写, Phase 1)
    notif_store = NotificationStore(
        os.path.join(os.path.dirname(config.db_path), "live_notifications.duckdb")
    )
    notifier = Notifier(config.wework_webhook, config.multi_channel_alert,
                        feishu_webhook=config.feishu_webhook, channel=config.notify_channel,
                        notif_store=notif_store)
    kill_switch = KillSwitch(config, store, notifier)
    clearance_lock = ClearanceLock(config)
    pnl_engine = PnlEngine(store)
    audit = AuditLogger(store)

    # 检查 kill switch 文件(上次崩溃可能留下)
    if kill_switch.is_active():
        # 带上当初激活的真实信息(从残留文件加载), 否则笼统"检测到残留"让人不知为何被按下
        _ks = kill_switch.status()
        # 激活时间 ISO -> 友好格式, 解析失败回退原值
        _at_raw = _ks.get('activated_at')
        try:
            from datetime import datetime as _dt
            _at = _dt.fromisoformat(_at_raw).strftime('%Y-%m-%d %H:%M:%S') if _at_raw else '未知'
        except Exception:
            _at = _at_raw or '未知'
        _detail = (f"当初原因:{_ks.get('reason') or '未知'} | "
                   f"当初来源:{_ks.get('source') or '未知'} | "
                   f"激活于:{_at}")
        logger.warning(f"⚠ 启动检测到残留 kill switch ({_detail}), 需人工解除后才下单")
        if notifier:
            # scheduler 激活的(非交易日)会在交易日 09:20 自动解除, 提示无需人工; 其他来源仍需人工
            _hint = "交易日 09:20 自动解除, 无需人工" if _ks.get('source') == 'scheduler' else "需人工介入"
            notifier.kill_switch_activated(f"启动检测到残留（{_detail}）", "startup_check", hint=_hint)

    qmt = QmtWrapper(config)
    callback = CallbackHandler(config, store, kill_switch, clearance_lock, pnl_engine, notifier, runtime_state)
    conn = ConnectionManager(config, qmt, callback, kill_switch, store)
    risk_gate = RiskGate(config, store, kill_switch, qmt)
    reconciler = Reconciler(config, store, qmt, kill_switch, notifier, pnl_engine)
    exit_monitor = ExitMonitor(config, store, qmt, risk_gate, clearance_lock,
                                kill_switch, callback, audit, pnl_engine, runtime_state)

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
        # 持仓接管(§3.3.1),返回 QMT 实际持仓集合供残留检查复用
        confirmed_qmt_codes = _takeover_positions(store, qmt, config, audit)
        # 清理 dry-run 残留(§18.3 严格保护:live 启动时校验无 dry-run 残留)
        _cleanup_dryrun_residue(store, config, audit, confirmed_qmt_codes=confirmed_qmt_codes)
        # 写入当日首条资产备份(为闸门5a日亏计算提供基准,§16.4)
        if qmt.connected:
            asset_data = qmt.query_asset()
            if asset_data:
                store.backup_asset(asset_data)
                logger.info(f"资产备份(闸门5a基准): total_asset={asset_data.get('total_asset', 0):.2f}")
        # 2026-07-04 修复:启动时若 QMT 连接+持仓接管成功,自动解除残留 kill_switch
        # 场景:周末 QMT 服务端维护期间,旧版本因 status=3 误激活 kill_switch,
        #       维护结束后新进程启动时,残留 .kill_switch 文件导致系统一直处于停机状态。
        #       现在连接+接管都成功说明交易通道正常,自动清理残留(只清来源=on_account_status 的残留)
        if kill_switch.is_active() and qmt.connected and confirmed_qmt_codes is not None:
            ks_status = kill_switch.status()
            if ks_status.get("source") in ("on_account_status", "startup_check"):
                logger.warning(
                    f"启动健康检查:QMT 已连接+持仓接管成功({len(confirmed_qmt_codes)} 只),"
                    f"自动清理残留 kill_switch(原因为 {ks_status.get('source')}:{ks_status.get('reason', '')})"
                )
                kill_switch.deactivate()
                audit.log("startup_killswitch_auto_clear",
                          reason=f"QMT连接+持仓接管成功,自动解除残留 ({ks_status.get('reason', '')})")
            else:
                logger.warning(
                    f"启动时 kill_switch 已激活(source={ks_status.get('source')}),"
                    f"非自动激活,需人工解除"
                )
        # 冷启动 last_close 补全:QMT 连接后主动刷新一次行情
        # 确保持仓有 last_close(昨收),供今日盈亏计算(过夜持仓=现价-昨收)
        if qmt.connected:
            try:
                _positions = store.get_positions()
                if _positions:
                    _codes = [p.get("code", "") for p in _positions if p.get("code")]
                    if _codes:
                        _quotes = qmt.get_realtime_quotes(_codes)
                        if _quotes:
                            _updated = store.refresh_quotes(_quotes)
                            logger.info(f"冷启动行情刷新: {_updated} 条持仓补 last_close")
                        else:
                            logger.warning("冷启动 refresh 跳过:QMT 未返回行情,等首次行情回调")
                else:
                    logger.info("冷启动 refresh 跳过:无持仓")
            except Exception as e:
                logger.warning(f"冷启动行情刷新失败: {e},等首次行情回调")
        else:
            logger.warning("冷启动 refresh 跳过:QMT 未连接,等首次行情回调")
    except Exception as e:
        logger.error(f"QMT 连接失败: {e}")
        kill_switch.activate(reason=f"QMT连接失败: {e}", source="startup")
        if notifier:
            notifier.kill_switch_activated(f"QMT连接失败: {e}", "startup")

    _state.update({
        "store": store, "notifier": notifier, "notif_store": notif_store,
        "kill_switch": kill_switch,
        "clearance_lock": clearance_lock, "qmt": qmt, "callback": callback,
        "conn": conn, "risk_gate": risk_gate, "reconciler": reconciler,
        "exit_monitor": exit_monitor, "audit": audit, "pnl_engine": pnl_engine,
    })

    # 候选③:OrderExecutor 深 module(3 路下单委托同一入口)
    from .order_executor import OrderExecutor
    executor = OrderExecutor(
        config=config, runtime_state=runtime_state,
        store=store, qmt=qmt, risk_gate=risk_gate,
        clearance_lock=clearance_lock, kill_switch=kill_switch,
        callback_handler=callback, audit=audit, notifier=notifier,
    )
    _state["executor"] = executor
    # exit_monitor 也需要同实例(exit_monitor 内部 _execute_sell 要委托)
    exit_monitor.order_executor = executor

    # 启动调度器(离场扫描 + 对账 + EOD 归档 + 非交易日检测)
    scheduler = LiveScheduler(
        config, store, qmt, exit_monitor, reconciler, kill_switch, notifier, audit,
        runtime_state=runtime_state,
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
        _notif_store = _state.get("notif_store")
        if _notif_store:
            _notif_store.close()
        _kill_all_subprocesses()
        # 释放文件锁
        if "lock_fd" in _state:
            _state["lock_fd"].close()
        if "lock_file" in _state and os.path.exists(_state["lock_file"]):
            os.remove(_state["lock_file"])
    except Exception as e:
        logger.error(f"关闭异常: {e}")
    logger.info("live_trader 已关闭")


def _takeover_positions(store, qmt, config, audit) -> set:
    """持仓接管(§3.3.1):分类 managed=true/false

    Returns:
        QMT 实际持仓代码集合(供后续残留检查复用,避免重复查询 xtquant 抖动)
    """
    if not qmt.connected:
        return set()
    qmt_positions = qmt.query_positions()
    if not qmt_positions:
        logger.info("持仓接管:QMT 无持仓")
        return set()

    qmt_codes = set()
    preserved = set(config.preserved_codes)
    for pos in qmt_positions:
        code = pos.get("code", "")
        if not code:
            continue
        from app.utils.xtquant_compat import format_code, strip_code_suffix
        code_fmt = format_code(code) if '.' not in code else code
        qmt_codes.add(code_fmt)
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
    return qmt_codes


def _cleanup_dryrun_residue(store, config, audit, confirmed_qmt_codes: set = None) -> None:
    """清理 dry-run 残留(§18.3 严格保护)

    live 启动时(或 dry-run 切 live 前),删除本地 live_positions 中
    QMT 实际没有但本地有的 managed=true 持仓(dry-run mock 下单残留)。

    规则:
    - 优先使用 confirmed_qmt_codes(由 _takeover_positions 刚确认的 QMT 持仓集合),
      避免重复查询 xtquant 导致缓存抖动误报(2026-07-02 事故根因)
    - 无 confirmed_qmt_codes 时才独立查询 QMT(QMT 查询做 3 次重试取并集)
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

        # QMT 实际持仓代码集合
        # 优先复用 _takeover_positions 刚确认的集合(防抖动误报)
        qmt_codes = set(confirmed_qmt_codes) if confirmed_qmt_codes else set()

        # 无 confirmed 集合时才独立查询 QMT(3 次重试取并集)
        if not qmt_codes:
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
    runtime_state = _state.get("runtime_state")
    return {
        "mode": runtime_state.mode if runtime_state else (config.mode if config else "unknown"),
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


# 简称缓存: code -> name(xtdata.get_instrument_detail 返回,股票/ETF/指数全覆盖)
# 只缓存非空结果,空结果不缓存(防 xtquant 抖动时永久踩死,下次轮询自动重试)
_instrument_name_cache: dict = {}

@app.get("/live/positions")
async def positions():
    """持仓查询(含 managed 标记 + today_buy_volume + 简称)"""
    store = _state.get("store")
    if not store:
        raise HTTPException(503, "Store 未初始化")
    positions = store.get_positions(managed_only=False)
    # 补 today_buy_volume(今日买入量,从 live_deals 算)
    # 前端按"今日买入部分按买入价、过夜部分按昨收"拆分今日盈亏
    if positions and store._conn:
        from datetime import date as _date
        today = _date.today().isoformat()
        rows = store._conn.execute(
            "SELECT code, SUM(filled_volume) FROM live_deals "
            "WHERE direction = 'buy' AND traded_at >= ? "
            "GROUP BY code", [today]
        ).fetchall()
        buy_map = {r[0]: int(r[1] or 0) for r in rows}
        for p in positions:
            p['today_buy_volume'] = buy_map.get(p.get('code'), 0)
    # 补股票简称(stocks 基础表不含 ETF,改用 xtdata.get_instrument_detail 全覆盖)
    qmt = _state.get("qmt")
    for p in positions:
        code = p.get('code', '')
        if not code:
            p['name'] = ''
            continue
        name = _instrument_name_cache.get(code)
        if name is None and qmt:
            try:
                detail = qmt.get_instrument_detail(code) or {}
                name = str(detail.get('InstrumentName') or '').strip()
                if name:
                    _instrument_name_cache[code] = name
            except Exception:
                name = ''
        p['name'] = name or ''
    return positions


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
    """下单核心逻辑 — 委托给 OrderExecutor(候选③)。

    供 /live/order(WEB)和 /live/buy-signal(TDX)共用。

    Args:
        intent: OrderIntent 下单意图
        source: 下单来源 "WEB"/"TDX",决定价格策略和 terminal 标记
        lock_wait_sec: 清仓锁等待秒数(手动30s, buy-signal 5s)

    Returns:
        dict 下单结果 {"ok", "order_id", "client_order_id", "status", "reason", ...}
    """
    from .order_executor import OrderExecutor
    executor: OrderExecutor = _state.get("executor")
    if not executor:
        return {"ok": False, "status": "error", "reason": "OrderExecutor 未初始化"}
    return executor.execute(intent, source=source, lock_wait_sec=lock_wait_sec)


async def process_buy_signals(
    signals: list,
    strategy: str = "QUANTQQ",
    source: str = "TDX",
    lock_wait_sec: int = 5,
):
    """买入信号批量处理(共享内核)—— HTTP 端点 + scheduler 自给自足同源。

    保留全部副作用(与原 buy_signal 内核一致):
      - kill_switch 检查(active → 全拒,不抛)
      - 时点 cutoff 检查(now >= buy_signal_cutoff → 全拒,不抛)
      - 信号去重(format_code 统一 key,同 code 只留首条)
      - 并发信号量 3 + _process_one_signal(幂等键 + 尾盘定价,零改动复用)
      - 心跳 store.record_heartbeat("docker_tdx", count, scan_status)  ★必须保留★
        (否则 14:55 看门狗 scheduler.py:_check_signal_heartbeat 误报"无信号心跳")
    删除 HTTP 专属(鉴权 / BuySignalRequest 校验 / buy_enabled 检查 / HTTPException)。
    绝不 raise,失败转 rejected。

    Args:
        signals: List[SignalItem](pydantic 模型,有 .code/.price)
        strategy: 策略名(透传 _process_one_signal)
        source: "TDX" 决定 _process_one_signal 的 terminal/定价
        lock_wait_sec: 清仓锁等待(默认 5s,与原 buy_signal 一致)
    Returns:
        BuySignalResult(accepted/rejected/details)
    """
    from .schemas import BuySignalResult, SignalResult
    from app.utils.xtquant_compat import format_code

    config = _state.get("config")
    store = _state.get("store")

    # 1. kill_switch(必须保留;HTTP 路径已在端点 raise 403,这里是 scheduler 路径 + 防御)
    kill_switch = _state.get("kill_switch")
    if kill_switch and kill_switch.is_active():
        return BuySignalResult(
            accepted=[], rejected=[s.code for s in signals],
            details=[SignalResult(code=s.code, ok=False, status="forbidden",
                                  reason="kill switch 已激活") for s in signals])

    # 2. 时点 cutoff(必须保留,防尾盘过点乱下单)
    now_str = datetime.now().strftime("%H:%M")
    cutoff = config.buy_signal_cutoff if config else "14:59"
    if now_str >= cutoff:
        reason = f"尾盘已过({now_str} >= {cutoff}),信号丢弃"
        logger.warning(reason)
        return BuySignalResult(
            accepted=[], rejected=[s.code for s in signals],
            details=[SignalResult(code=s.code, ok=False, status="timeout", reason=reason)
                     for s in signals])

    # 3. 信号去重(format_code 统一 key,同 code 只留首条)
    seen_codes = set()
    unique_signals = []
    for s in signals:
        code_key = format_code(s.code) if '.' not in s.code else s.code
        if code_key not in seen_codes:
            seen_codes.add(code_key)
            unique_signals.append(s)
    if len(unique_signals) < len(signals):
        logger.info(f"信号去重: {len(signals)} -> {len(unique_signals)}")

    # 4. 并发处理(信号量3)+ _process_one_signal(零改动复用:幂等键+尾盘定价+下单)
    semaphore = asyncio.Semaphore(3)
    tasks = [_process_one_signal(s, semaphore, lock_wait_sec=lock_wait_sec,
                                 strategy_name=strategy) for s in unique_signals]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 5. 汇总
    accepted, rejected, details = [], [], []
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
                code=code, ok=r.get("ok", False), status=r.get("status", ""),
                reason=r.get("reason", ""), order_id=r.get("order_id"),
            ))

    # 6. 心跳(必须保留,否则 14:55 看门狗误报)
    if store:
        try:
            scan_status = "ok" if len(accepted) > 0 else (
                "no_signal" if len(unique_signals) == 0 else "all_rejected")
            store.record_heartbeat("docker_tdx", len(unique_signals), scan_status)
        except Exception as e:
            logger.warning(f"心跳记录失败: {e}")

    logger.info(f"buy-signal 处理完成: accepted={accepted} rejected={rejected}")
    return BuySignalResult(accepted=accepted, rejected=rejected, details=details)


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

        # 计算买入量:本金×比例,卡在全局 [min_buy_amount, max_buy_amount] 之间
        # buy_position_ratio 走 runtime_state 可变 holder(热更新);min/max 走 settings trading 段(全局,与模拟盘同源)
        rs = _state.get("runtime_state")
        ratio = rs.buy_position_ratio if rs else (config.buy_position_ratio if config else 0.05)
        capital = config.live_capital if config else 0
        from core.settings import settings as _settings
        min_amt = float(_settings.get("trading", "min_buy_amount", default=5000))
        max_amt = float(_settings.get("trading", "max_buy_amount", default=60000))
        position_size = max(min_amt, min(ratio * capital, max_amt))
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
    from .schemas import BuySignalRequest

    config = _state.get("config")

    if not config:
        raise HTTPException(503, "未初始化")

    # 信号开关检查(v2 A9: 运行时开关,从 runtime_state 读)
    runtime_state = _state.get("runtime_state")
    _buy_enabled = runtime_state.buy_enabled if runtime_state else config.buy_signal_enabled
    if not _buy_enabled:
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

    # 业务内核委托共享函数(去重/cutoff/并发/心跳/幂等 全在 process_buy_signals)
    # HTTP 端点与 scheduler 自给自足同源,保证副作用一致(尤其心跳,防 14:55 看门狗误报)
    _strategy = getattr(signal_req, 'strategy', 'QUANTQQ') or 'QUANTQQ'
    return await process_buy_signals(
        signal_req.signals, strategy=_strategy, source=signal_req.source,
    )


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


# ===== 通知历史 API(v6.0 Phase 1 + Phase 3) =====

@app.get("/live/notifications")
async def get_notifications(level: str = "", limit: int = 50):
    """拉取通知历史

    Args:
        level: 可选过滤 INFO / WARN / CRITICAL
        limit: 最大返回条数(默认 50，上限 200)
    """
    store = _state.get("notif_store")
    if not store:
        raise HTTPException(503, "通知存储未初始化")
    lv = level if level in ("INFO", "WARN", "CRITICAL") else None
    return store.recent(limit=min(limit, 200), level=lv)


@app.get("/live/notifications/summary")
async def get_notifications_summary():
    """今日通知统计(各 level 计数)"""
    store = _state.get("notif_store")
    if not store:
        raise HTTPException(503, "通知存储未初始化")
    from datetime import date as _date
    today_start = f"{_date.today().isoformat()}T00:00:00"
    return store.count_by_level(since_iso=today_start)


_last_test_notify = {"ts": 0.0, "_lock": threading.Lock()}


@app.post("/live/notifications/test")
async def test_notification(request: Request, body: dict | None = None):
    """手动触发测试通知(仅本地,60 秒冷却)"""
    if not _is_local(request):
        raise HTTPException(403, "仅允许本地调用")
    import time
    now = time.time()
    with _last_test_notify["_lock"]:
        if now - _last_test_notify["ts"] < 60:
            return {"sent": False, "msg": "请 60 秒后再试"}
        _last_test_notify["ts"] = now
    notifier = _state.get("notifier")
    if not notifier:
        raise HTTPException(503, "Notifier 未初始化")
    level = (body or {}).get("level", "INFO")
    if level not in ("INFO", "WARN", "CRITICAL"):
        level = "INFO"
    msg = f"测试通知({level}) @ {datetime.now().strftime('%H:%M:%S')}"
    notifier.send(msg)
    return {"sent": True, "msg": msg}


@app.post("/shutdown")
async def shutdown_service(request: Request):
    """优雅关闭服务(仅 localhost)。

    替代 stop.bat 的 taskkill /F(后者绕过 atexit、DuckDB 不 close)。
    Windows 下 SIGINT 不能可靠触发 uvicorn lifespan shutdown(实测日志无"已关闭"),
    故在此显式 store.close() 触发 live_trader.duckdb 的 WAL checkpoint, 不依赖 lifespan。
    """
    if not _is_local(request):
        raise HTTPException(403, "仅允许本地调用")
    logger.info("/shutdown 收到请求, 准备优雅关闭 (显式 store.close + SIGINT)...")

    # 显式关 store: 停 flusher + 最后 flush + close 连接(= checkpoint WAL)
    store = _state.get("store")
    if store:
        try:
            store.close()
        except Exception as e:
            logger.warning(f"/shutdown store.close 异常: {e}")

    def _trigger_exit():
        time.sleep(0.5)
        try:
            os.kill(os.getpid(), signal.SIGINT)
        except Exception as e:
            logger.warning(f"/shutdown SIGINT 触发失败: {e}")

    threading.Thread(target=_trigger_exit, daemon=True).start()
    return {"status": "shutting down"}


@app.get("/live/config/scan-interval")
async def get_scan_interval():
    """获取当前离场扫描间隔(秒)"""
    scheduler = _state.get("scheduler")
    if not scheduler:
        raise HTTPException(503, "调度服务未启动")
    return {"interval_sec": scheduler.get_scan_interval()}


@app.put("/live/config/scan-interval")
async def set_scan_interval(body: dict):
    """设置离场扫描间隔(秒)。保存后立即生效,不阻塞。范围:10~300"""
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


@app.get("/live/config/buy-ratio")
async def get_buy_ratio():
    """获取单只占本金比例(实盘下单 = clamp(本金×比例, 全局min, 全局max))"""
    rs = _state.get("runtime_state")
    if not rs:
        raise HTTPException(503, "未初始化")
    return {"buy_position_ratio": rs.buy_position_ratio}


@app.put("/live/config/buy-ratio")
async def set_buy_ratio(request: Request, body: dict):
    """设置单只占本金比例(仅本地)。内存立即生效 + 持久化到 live_trader.buy_position_ratio 顶层。范围:(0,1]"""
    if not _is_local(request):
        raise HTTPException(403, "仅允许本地调用")
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

def _is_local(request) -> bool:
    """v2(A8): admin 接口仅允许本地调用(防远程误操作真钱)"""
    if not request or not getattr(request, "client", None):
        return False
    return request.client.host in ("127.0.0.1", "::1", "localhost")


@app.get("/live/config/switches")
async def get_switches():
    """读取买入/卖出开关状态"""
    rs = _state.get("runtime_state")
    if not rs:
        raise HTTPException(503, "未初始化")
    return rs.get_state()


@app.put("/live/config/switches")
async def set_switches(request: Request, body: dict):
    """切换买入/卖出开关(仅本地)"""
    if not _is_local(request):
        raise HTTPException(403, "仅允许本地调用")
    rs = _state.get("runtime_state")
    if not rs:
        raise HTTPException(503, "未初始化")
    buy = body.get("buy_enabled")
    sell = body.get("sell_enabled")
    auto_buy = body.get("auto_buy_enabled")
    old = rs.set_switches(buy_enabled=buy, sell_enabled=sell, auto_buy_enabled=auto_buy)
    audit = _state.get("audit")
    if audit:
        audit.log("switch_changed", snapshot={"old": old, "new": {"buy_enabled": buy, "sell_enabled": sell, "auto_buy_enabled": auto_buy}})
    logger.info(f"开关切换: {old} -> buy={buy} sell={sell} auto_buy={auto_buy}")
    return rs.get_state()


@app.get("/live/config/mode")
async def get_mode():
    """读取当前模式(dry-run/live)"""
    rs = _state.get("runtime_state")
    if not rs:
        raise HTTPException(503, "未初始化")
    return {"mode": rs.mode}


@app.post("/live/config/mode")
async def set_mode(request: Request, body: dict):
    """切换模式(仅本地)。live→dry-run 撤在途单等终态;dry-run→live 清残留+QMT检查"""
    if not _is_local(request):
        raise HTTPException(403, "仅允许本地调用")
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


@app.get("/live/config/risk-params")
async def get_risk_params():
    """返回 risk 段止盈止损参数(前端展示用,不写死)"""
    from core.settings import settings
    keys = ["hard_stop_loss_pct", "take_profit_tiers", "trailing_stop_activate_pct",
            "trailing_stop_drawdown_pct", "time_exit_days", "time_exit_min_profit_pct",
            "time_exit_force_days", "first_day_exit_min_profit", "first_day_exit_days"]
    return {"params": {k: settings.get("risk", k) for k in keys}}


@app.get("/live/equity")
async def get_equity(days: int = 1):
    """净值曲线数据(从 live_assets_backup 5min 快照,v2 §3.5)"""
    store = _state.get("store")
    if not store:
        raise HTTPException(503, "未初始化")
    try:
        assert store._conn is not None
        from datetime import date as _date, timedelta
        start = _date.today() - timedelta(days=int(days))
        rows = store._conn.execute(
            "SELECT backup_date, backup_time, cash, market_value, total_asset "
            "FROM live_assets_backup WHERE backup_date >= ? "
            "ORDER BY backup_date, backup_time",
            [start]
        ).fetchall()
        pts = [{"date": str(r[0]), "time": r[1], "cash": float(r[2] or 0),
                "market_value": float(r[3] or 0), "total": float(r[4] or 0)} for r in rows]
        return {"points": pts}
    except Exception as e:
        logger.error(f"净值查询失败: {e}")
        return {"points": [], "error": str(e)}


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
