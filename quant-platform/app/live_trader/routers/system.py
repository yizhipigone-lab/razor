"""实盘交易 - 系统类路由(通知/同步/health/shutdown)。

阶段 1 第 1 步(2026-07-19) 从 main.py 抽离,共 9 个路由:
  POST /live/positions/sync     (sync_positions_admin, 原 main.py:559)
  POST /live/sync-positions     (sync_positions_full,  原 main.py:1120)
  GET  /live/health             (原 main.py:1139)
  GET  /live/notifications      (原 main.py:1147)
  GET  /live/notifications/summary (原 main.py:1162)
  POST /live/notifications/test (原 main.py:1176, 带 _last_test_notify 冷却)
  POST /shutdown                (原 main.py:1197)
  POST /live/sync/intra         (原 main.py:1834, 子进程)
  POST /live/sync/index_daily   (原 main.py:1864, 子进程)

依赖:顶部 import _state(独立模块) + auth(独立模块)。
对 main 内工具函数(_takeover_positions / _cleanup_zombies / _spawned_*)
用**函数内 import**,避免 router→main 顶层循环 import;阶段 2 这些工具搬
lifecycle.py 后改 import 源即可。

⚠️ __file__ 路径:本文件在 app/live_trader/routers/ 下,到项目根要四层 dirname
(比原 main.py 多一层 routers/)。sync/intra、sync/index_daily 的 script_path
已据此调整。
"""
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import date as _date, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from core.logger import get_logger

from .._state import state as _state
from ..auth import _require_admin

logger = get_logger("live_trader.routers.system")

router = APIRouter()

# 项目根(本文件在 app/live_trader/routers/system.py, parents[3]=项目根;
# 原 main.py 在 app/live_trader/main.py 只需三层 dirname)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

# 测试通知 60s 冷却(原 main.py:1173, 仅 test_notification 用)
_last_test_notify = {"ts": 0.0, "_lock": threading.Lock()}


@router.post("/live/positions/sync")
async def sync_positions_admin(request: Request, body: dict = None):
    """持仓同步(2026-07-18 手工下单 M4):本地持仓立即与 QMT 对齐

    - body.code 可选:只同步单只;不传=全量
    - 复用 _takeover_positions(upsert 保留 peak_price/sell_count/entry_date 等扩展字段)
    - 语义:只刷新数量/价格,不删本地行(卖出全平靠成交回调置零;物理删除归 _cleanup_dryrun_residue)
    """
    _require_admin(request)
    store = _state.get("store")
    qmt = _state.get("qmt")
    config = _state.get("config")
    audit = _state.get("audit")
    if not store or not config:
        raise HTTPException(503, "未初始化")
    if not qmt or not qmt.connected:
        raise HTTPException(503, "QMT 未连接,无法同步持仓")

    from ..main import _takeover_positions  # 阶段2搬 lifecycle.py 后改 import 源
    code = (body or {}).get("code")
    try:
        codes = _takeover_positions(store, qmt, config, audit, code=code)
    except Exception as e:
        logger.error(f"持仓同步失败 code={code}: {e}")
        raise HTTPException(500, f"持仓同步失败: {e}")
    logger.info(f"持仓同步完成: code={code or '全量'} synced={len(codes)}")
    return {"synced": len(codes), "codes": sorted(codes)}


@router.post("/live/sync-positions")
async def sync_positions_full():
    """从 QMT 重新同步持仓到 live_positions(修复缺失持仓)"""
    qmt = _state.get("qmt")
    store = _state.get("store")
    audit = _state.get("audit")
    config = _state.get("config")
    if not qmt or not qmt.connected:
        raise HTTPException(503, "QMT 未连接")
    if not store:
        raise HTTPException(503, "Store 未初始化")
    from ..main import _takeover_positions  # 阶段2搬 lifecycle.py 后改 import 源
    _takeover_positions(store, qmt, config, audit)
    positions = store.get_positions()
    return {"synced": len(positions), "positions": [
        {"code": p.get("code"), "volume": p.get("volume"),
         "managed": p.get("managed")} for p in positions
    ]}


@router.get("/live/health")
async def health():
    """健康检查(NSSM 用)"""
    return {"status": "ok", "ts": datetime.now().isoformat()}


# ===== 通知历史 API(v6.0 Phase 1 + Phase 3) =====

@router.get("/live/notifications")
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


@router.get("/live/notifications/summary")
async def get_notifications_summary():
    """今日通知统计(各 level 计数)"""
    store = _state.get("notif_store")
    if not store:
        raise HTTPException(503, "通知存储未初始化")
    today_start = f"{_date.today().isoformat()}T00:00:00"
    return store.count_by_level(since_iso=today_start)


@router.post("/live/notifications/test")
async def test_notification(request: Request, body: dict | None = None):
    """手动触发测试通知(仅本地,60 秒冷却)"""
    _require_admin(request)
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


@router.post("/shutdown")
async def shutdown_service(request: Request):
    """优雅关闭服务(仅 localhost)。

    替代 stop.bat 的 taskkill /F(后者绕过 atexit、DuckDB 不 close)。
    Windows 下 SIGINT 不能可靠触发 uvicorn lifespan shutdown(实测日志无"已关闭"),
    故在此显式 store.close() 触发 live_trader.duckdb 的 WAL checkpoint, 不依赖 lifespan。
    """
    _require_admin(request)
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


# ===== 数据同步(隔离子进程,原 qmt_proxy 迁移端点) =====

@router.post("/live/sync/intra")
async def sync_intra(req: dict):
    """分时数据同步(隔离子进程,替代 qmt_proxy /api/sync/intra)"""
    from ..main import _cleanup_zombies, _spawned_lock, _spawned_processes  # 阶段2搬 lifecycle
    _cleanup_zombies()
    try:
        freq = req.get("freq", "5m")
        days = req.get("days", 30)
        start_date = req.get("start_date")
        end_date = req.get("end_date")

        # 项目根(本文件比原 main.py 多一层 routers/,用 _PROJECT_ROOT 常量避免数层数)
        script_path = str(_PROJECT_ROOT / "qmt_sync_job.py")
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


@router.post("/live/sync/index_daily")
async def sync_index_daily(req: dict):
    """指数日线同步(隔离子进程,替代 qmt_proxy /api/sync/index_daily)"""
    from ..main import _cleanup_zombies, _spawned_lock, _spawned_processes  # 阶段2搬 lifecycle
    _cleanup_zombies()
    try:
        start_date = req.get("start_date")
        end_date = req.get("end_date")

        script_path = str(_PROJECT_ROOT / "qmt_sync_index_job.py")
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
