from fastapi import APIRouter
from datetime import date, timedelta
from core.logger import get_logger
from database.duckdb_manager import db
import threading
import json
from server.websocket.manager import sync_broadcast
from app.utils.threading import run_in_thread

log = get_logger("API-Screener")
router = APIRouter()
stop_events = {}
_stop_events_lock = threading.Lock()

@router.post("/api/screener/scan")
async def run_scan(req: dict):
    from app.screener.engine import ScreenerEngine
    log.info(f"📡 [API] 收到选股请求: {req.get('strategy_name', 'unknown')}")
    try:
        engine = ScreenerEngine()
        
        today = date.today()
        today_str = today.isoformat()
        start_str = req.get('start') or (today - timedelta(days=30)).isoformat()
        end_str = req.get('end') or today_str
        start_dt = date.fromisoformat(start_str)
        end_dt = date.fromisoformat(end_str)

        # 任务唯一终止信道
        stop_event = threading.Event()
        with _stop_events_lock:
            stop_events['scan'] = stop_event

        def _do_scan():
            log.info(f"🚀 [Thread] 选股任务线程已启动: {req.get('strategy_name')}")
            try:
                # 进度反馈闭包
                def _on_progress(curr, tot, msg):
                    sync_broadcast({
                        "type": "screener_progress",
                        "step": curr,
                        "total": tot,
                        "msg": msg
                    })
                    sync_broadcast({"type": "log", "level": "info", "msg": f"[{curr}/{tot}] {msg}"})

                res = engine.run_scan(
                    strategy_name=req['strategy_name'],
                    freq=req.get('freq', 'daily'),
                    exchanges=req.get('exchanges'),
                    sectors=req.get('sectors'),
                    hot_sectors_only=req.get('hot_only', False),
                    index_filter=req.get('index_filter') or None,
                    min_mv=req.get('min_mv'),
                    max_mv=req.get('max_mv'),
                    start=start_dt,
                    end=end_dt,
                    progress_callback=_on_progress,
                    stop_event=stop_event,
                )
                
                if stop_event.is_set():
                    sync_broadcast({"type": "log", "level": "warn", "msg": "🛑 选股任务已手动中止"})
                
                sync_broadcast({
                    "type": "scan_done",
                    "count": len(res),
                    "results": res
                })
            except Exception as e:
                import traceback
                log.error(f"扫描任务执行崩溃: {e}\n{traceback.format_exc()}")
                sync_broadcast({"type": "log", "level": "error", "msg": f"扫描崩溃: {str(e)}"})
                sync_broadcast({"type": "scan_done", "count": 0, "results": [], "error": str(e)})
            finally:
                with _stop_events_lock:
                    if 'scan' in stop_events: del stop_events['scan']

        run_in_thread(_do_scan)
        return {"status": "started"}
    except Exception as e:
        log.error(f"选股接口异常: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e), "results": []}

@router.get("/api/scan/history")
async def get_scan_history():
    df = db.get_scan_history(limit=50)
    return df.to_dict(orient="records")

@router.get("/api/scan/history/{history_id}")
async def get_scan_history_detail(history_id: int):
    try:
        row = db.conn.execute("SELECT stock_codes, scan_time FROM scan_history WHERE id=?", [history_id]).fetchone()
        if not row:
            return {"results": []}
        
        codes_json, scan_time = row[0], row[1]
        codes = json.loads(codes_json) if codes_json else []
        if not codes:
            return {"results": []}
        
        # 匹配元数据
        placeholders = ",".join(["?"] * len(codes))
        sql = f"SELECT code, name, sector FROM stocks WHERE code IN ({placeholders})"
        meta_df = db.conn.execute(sql, codes).df()
        
        # 尝试查询这批股票最新的日线收盘价作为参考
        # 由于我们只存了历史的股票代码，没有存当时的收盘价，这里取当前最新价即可
        results = []
        for c in codes:
            # 找到 meta
            m_row = meta_df[meta_df['code'] == c]
            name = m_row.iloc[0]['name'] if not m_row.empty else c
            sector = m_row.iloc[0]['sector'] if not m_row.empty else ""
            
            # 使用一个预估的最新价或0，如果是回测，回测能跑就行
            # 为保证界面的兼容性，增加 date, close 字段
            results.append({
                "code": c,
                "name": name,
                "sector": sector,
                "close": 0.0, # 选股历史快照不提供准确的复权价格，仅作列表展示
                "date": scan_time.strftime("%Y-%m-%d") if hasattr(scan_time, 'strftime') else str(scan_time)
            })
            
        return {"results": results}
    except Exception as e:
        log.error(f"获取选股历史详情异常: {e}")
        return {"results": []}

# ─── 舆情热点 API ───────────────────────────────────────────────

