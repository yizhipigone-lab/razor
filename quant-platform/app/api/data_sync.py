from fastapi import APIRouter, Request, HTTPException
from typing import Optional
from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db
import threading
from server.websocket.manager import manager, sync_broadcast
from app.utils.threading import run_in_thread
from app.scheduler.cron_jobs import pipeline_scheduler

log = get_logger("API-DataSync")
router = APIRouter()
_sync_lock = threading.Lock()
_is_syncing = False


def _sync_acquire() -> bool:
    """线程安全地获取同步锁（非阻塞），可在 async 和 thread 中混用"""
    global _is_syncing
    if _sync_lock.acquire(blocking=False):
        if _is_syncing:
            _sync_lock.release()
            return False
        _is_syncing = True
        _sync_lock.release()
        return True
    return False


def _sync_release():
    """释放同步锁"""
    global _is_syncing
    with _sync_lock:
        _is_syncing = False

@router.post("/api/sync/force_run")
async def force_run_sync():
    import asyncio
    asyncio.create_task(pipeline_scheduler.trigger_manual_run())
    return {"status": "ok", "message": "全自动同步清洗已在后台启动！"}


@router.post("/api/data/download")
async def start_download(mode: str = "incremental", freq: str = "daily", years: int = 1, start_date: str = None, end_date: str = None):
    """异步启动数据下载 (支持 mode=full/incremental/custom)"""
    if not _sync_acquire():
        return {"status": "error", "message": "同步正在进行中..."}

    def _do_download():
        print(f"DEBUG: [Thread] 数据同步线程正式启动 (Mode: {mode}, Freq: {freq})")
        try:
            from app.data_manager.engine import batch_download_all
            def _on_progress(curr, tot, m):
                sync_broadcast({"type": "progress", "step": curr, "total": tot, "msg": m})
                # 恢复日志侧边栏的消息推送
                sync_broadcast({"type": "log", "level": "info", "msg": f"[{curr}/{tot}] {m}"})
            
            print(f"DEBUG: [Thread] 正在执行 batch_download_all...")
            batch_download_all(freq=freq, years=years, mode=mode, custom_start=start_date, custom_end=end_date, progress_cb=_on_progress)
            sync_broadcast({"type": "done", "msg": "同步圆满结束！"})
        except Exception as e:
            print(f"CRITICAL: [Thread] 同步流程崩溃: {e}")
            import traceback
            traceback.print_exc()
        finally:
            _sync_release()
            print("DEBUG: [Thread] 同步线程已安全退出")

    run_in_thread(_do_download)
    return {"status": "started", "message": "全量同步任务已由后端线程承接"}

@router.post("/api/data/sync_log")
async def receive_sync_log(payload: dict, request: Request):
    """供外部脚本向 WebSocket 广播同步日志并控制状态"""
    # C4 修复(2026-07-19 审计):只允许本机调用(服务已绑 127.0.0.1,纵深防御防伪造 done 释放全局同步锁)
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "仅允许本地访问")
    msg = payload.get("msg", "")
    level = payload.get("level", "info")
    msg_type = payload.get("type", "log")

    if msg_type == "done" or "全面告捷" in msg:
        _sync_release()
        log.success(f"[Sync] 外部任务反馈同步完成: {msg}")
        # 直接将这条最后的消息篡改为前端认识的类型
        msg_type = "qmt_sync_done"
        msg = f"{msg} (Sync Finished)"

    sync_broadcast({"type": msg_type, "level": level, "msg": f"[External] {msg}"})
    return {"status": "ok"}

@router.post("/api/data/sync_qmt_intra")
async def sync_qmt_intra(body: dict):
    """QMT 分时同步专用接口 (5m/1m)"""
    if not _sync_acquire():
        return {"status": "error", "message": "系统正忙于其他任务"}

    # 前端统一传送 "5m" 或 "1m"，无需转换
    freq = body.get("freq", "5m")
    days = int(body.get("days", 30))
    start_date = body.get("start_date")
    end_date = body.get("end_date")

    def _do_qmt_sync():
        log.info(f"[Async] [QMT] Start Sync (Freq: {freq}, Range: {start_date or f'Last {days}d'})")
        try:
            import requests
            payload = {"freq": freq, "days": days, "start_date": start_date, "end_date": end_date}
            import os
            proxy_host = os.environ.get("LIVE_TRADER_HOST", os.environ.get("QMT_PROXY_HOST", "127.0.0.1"))
            target_url = f"http://{proxy_host}:8001/live/sync/intra"
            print(f">>> [PROXY-NET] Dispatching to: {target_url}")
            # 请求宿主机 Proxy 执行下载
            res = requests.post(target_url, json=payload, timeout=3)
            print(f">>> [PROXY-NET] Proxy Response Code: {res.status_code}")
            if res.status_code == 200:
                sync_broadcast({"type": "log", "level": "info", "msg": "Send command to QMT Proxy successfully"})
            else:
                sync_broadcast({"type": "log", "level": "error", "msg": f"Proxy Feedback Error: {res.text}"})
        except Exception as e:
            msg = f"QMT Sync Dispatch Failed: {str(e)}"
            log.error(msg)
            sync_broadcast({"type": "log", "level": "error", "msg": msg})
        finally:
            _sync_release()

    run_in_thread(_do_qmt_sync)
    return {"status": "started", "message": "QMT 并发同步请求提交成功"}

@router.get("/api/data/qmt_intra_status")
def qmt_intra_status(freq: str = "5m", days: int = 30, start_date: Optional[str] = None, end_date: Optional[str] = None):
    try:
        from app.data_manager.sync_min5 import get_qmt_intra_status
        return get_qmt_intra_status(freq, days, start_date, end_date)
    except Exception as e:
        return {"error": str(e)}


@router.post("/api/data/stop_qmt_intra")
async def stop_qmt_intra():
    """用户手动中断 QMT 分时同步任务"""
    try:
        from app.data_manager.sync_min5 import stop_qmt_sync
        stop_qmt_sync()
        sync_broadcast({"type": "log", "level": "warning", "msg": "🛑 停止指令已下达，等待当前批次完成后退出..."})
        return {"status": "ok", "message": "停止信号已发送，同步将在当前批次结束后停止"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ─── AI 回测：参数自动优化端点 ───────────────────────────────────────


@router.post("/api/data/sync_index")
async def sync_index_data():
    """同步全量主流市场指数日线数据到本地 Parquet（QMT 优先，Tushare 降级）"""
    if not _sync_acquire():
        return {"status": "error", "message": "系统正忙于其他同步任务，请稍后再试"}

    def _do_sync_index():
        try:
            from app.data_manager.tushare_sync import tushare_sync_manager

            def _cb(curr, total, msg):
                sync_broadcast({"type": "progress", "step": curr, "total": total, "msg": msg})
                sync_broadcast({"type": "log", "level": "info", "msg": f"[{curr}/{total}] {msg}"})

            # ── 方案A: Tushare 优先（与个股日线数据源一致）─────────
            sync_broadcast({"type": "log", "level": "info",
                            "msg": "📈 [Tushare] 开始同步全量指数日线数据..."})
            ok = tushare_sync_manager.sync_index_daily(progress_cb=_cb)
            source = "Tushare"

            # ── 方案B: Tushare 失败，降级 QMT ────────────────────
            if not ok:
                from app.data_manager.qmt_index_sync import sync_index_daily_qmt
                sync_broadcast({"type": "log", "level": "warning",
                                "msg": "⚠️ Tushare 不可用，降级到 QMT..."})
                ok = sync_index_daily_qmt(progress_cb=_cb)
                source = "QMT"

            if ok:
                sync_broadcast({"type": "done",
                                "msg": f"✅ 市场指数同步完成！（数据来源: {source}）"})
            else:
                sync_broadcast({"type": "log", "level": "error",
                                "msg": "❌ 指数同步失败（Tushare 和 QMT 均不可用）"})
        except Exception as e:
            log.error(f"指数同步失败: {e}")
            sync_broadcast({"type": "log", "level": "error", "msg": f"❌ 指数同步异常: {e}"})
        finally:
            _sync_release()

    run_in_thread(_do_sync_index)
    return {"status": "started", "message": "指数日线同步任务已启动（QMT优先）"}


@router.get("/api/data/check_index")
async def check_index_data():
    """查询本地全部指数数据状态"""
    from pathlib import Path
    import pandas as pd

    PARQUET_DIR = Path("data/parquet/daily")
    INDICES = {
        "index_000001": "上证综指",
        "index_399001": "深证成指",
        "index_399006": "创业板指",
        "index_000688": "科创50",
        "index_000300": "沪深300 ★ Regime基准",
        "index_000905": "中证500",
        "index_000852": "中证1000",
        "index_000985": "中证全指",
        "index_000016": "上证50",
    }
    result = []
    for stem, label in INDICES.items():
        p = PARQUET_DIR / f"{stem}.parquet"
        if p.exists():
            try:
                df = pd.read_parquet(p)
                df["date"] = pd.to_datetime(df["date"])
                result.append({
                    "name": label, "file": f"{stem}.parquet",
                    "status": "ok", "rows": len(df),
                    "start": str(df["date"].min().date()),
                    "end": str(df["date"].max().date()),
                    "size_kb": round(p.stat().st_size / 1024, 1),
                })
            except Exception as e:
                result.append({"name": label, "file": f"{stem}.parquet",
                               "status": "error", "error": str(e)})
        else:
            result.append({"name": label, "file": f"{stem}.parquet", "status": "missing"})
    return {"status": "ok", "indices": result}


@router.get("/api/data/fundamentals_preview")
async def get_fundamentals_preview(q: Optional[str] = "", roe: Optional[float] = None, pe: Optional[float] = None):
    df = db.get_fundamentals_preview(limit=100, search=q, min_roe=roe, max_pe=pe)
    if df.empty:
        return {"status": "empty", "data": []}
    
    # 转换日期格式以便序列化
    if 'updated_at' in df.columns:
        df['updated_at'] = df['updated_at'].astype(str)
    return {"status": "ok", "data": df.fillna("-").to_dict(orient="records")}

# ─── 选股 API ─────────────────────────────────────────────────

@router.post("/api/data/sync_index_members")
async def sync_index_members():
    """独立触发指数成分股同步（QMT Proxy 优先，Tushare 降级，本地CSV兜底）"""
    if not _sync_acquire():
        return {"status": "error", "message": "系统正忙于其他同步任务，请稍后再试"}

    def _do_sync():
        try:
            from app.data_manager.qmt_index_sync import qmt_index_syncer
            from app.data_manager.tushare_sync import tushare_sync_manager

            def _cb(curr, total, msg):
                sync_broadcast({"type": "progress", "step": curr, "total": total, "msg": msg})
                sync_broadcast({"type": "log", "level": "info", "msg": f"[{curr}/{total}] {msg}"})

            # ── 方案A: QMT Proxy 优先 ────────────────────────────
            sync_broadcast({"type": "log", "level": "info",
                            "msg": "🔄 [QMT] 开始同步指数成分股（14个主流指数）..."})
            result = qmt_index_syncer.sync_all(progress_cb=_cb)
            ok = result["success"]
            partial = result["success_count"] < result["total"]

            # ── 方案B: QMT 完全失败，降级 Tushare ────────────────
            if not ok:
                sync_broadcast({"type": "log", "level": "warning",
                                "msg": "⚠️ QMT 完全失败，降级到 Tushare..."})
                ok = tushare_sync_manager.sync_index_members(progress_cb=_cb)
                source = "Tushare"
            else:
                source = "QMT"

            if ok:
                if partial:
                    failed_names = ", ".join(result["failed"])
                    sync_broadcast({
                        "type": "log", "level": "warning",
                        "msg": f"⚠️ QMT 部分成功（{result['success_count']}/{result['total']}），失败指数: {failed_names}"
                    })
                sync_broadcast({"type": "done",
                                "msg": f"✅ 指数成分股同步完成！（数据来源: {source}）"})
            else:
                sync_broadcast({"type": "log", "level": "error",
                                "msg": "❌ 指数成分同步失败（QMT 和 Tushare 均不可用）"})
        except Exception as e:
            log.error(f"指数成分同步失败: {e}")
            sync_broadcast({"type": "log", "level": "error", "msg": f"❌ 指数成分同步异常: {e}"})
        finally:
            _sync_release()

    run_in_thread(_do_sync)
    return {"status": "started", "message": "指数成分同步任务已启动（QMT优先）"}


@router.get("/api/data/index_display_map")
async def index_display_map():
    """返回 INDEX_DISPLAY 映射，前端无需硬编码名称"""
    from app.backtest.index_config import INDEX_DISPLAY
    return {"status": "ok", "data": INDEX_DISPLAY}


@router.get("/api/data/index_members/status")
async def index_members_status():
    """查询各指数在库成分股数量和最后更新时间"""
    return {"status": "ok", "data": db.get_index_member_status()}


# ─── 回测历史 API ─────────────────────────────────────────────────

