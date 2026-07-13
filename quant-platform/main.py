# 固定项目路径，解决所有模块找不到问题
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import os
import signal
import threading
import time

from core.logger import get_logger
from core.settings import settings
from core.limiter import limiter, register_limiter
from database.duckdb_manager import db
from server.websocket.manager import manager, WebsocketGlobal

log = get_logger("API-Server")
ROOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = ROOT_DIR / "static"

main_loop = None

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    WebsocketGlobal.loop = main_loop
    
    log.info("Eurica Quant - 睿奕量化 后台任务启动...")
    
    # 模拟启动一些必须的后台任务
    # if settings.get('gateway', 'active_gateway') == 'qmt':
    #     try:
    #         from app.data_manager.qmt_sync import qmt_sync_manager
    #         qmt_sync_manager.start()
    #     except Exception as e:
    #         log.error(f"Cannot auto-start QMT: {e}")

    # 启动 APScheduler 定时任务调度器
    from app.scheduler.cron_jobs import pipeline_scheduler
    pipeline_scheduler.start()

    # 延迟 3 秒后检查行情网关健康状态（确保所有模块已加载）
    async def _check_market_gateway():
        await asyncio.sleep(3)
        try:
            from server.market.quotes import MarketQuotes
            mq = MarketQuotes()
            test_codes = ['000001.SH']
            result = await mq.get_realtime_quotes(test_codes)
            if not result:
                log.warning("⚠️ 行情网关不可用！live_trader(8001) 和 TDX 回退均失败，实时行情将无法推送")
                log.warning("⚠️ 请检查: 1) live_trader(8001) 是否启动  2) 网络连接")
            else:
                log.info("✅ 行情网关健康检查通过")
        except Exception as e:
            log.warning(f"⚠️ 行情网关健康检查异常: {e}")

    asyncio.create_task(_check_market_gateway())

    # 延迟 5 秒后启动自动数据补齐（确保页面优先加载完毕）
    async def _delayed_auto_sync():
        await asyncio.sleep(5)
        from app.data_manager.auto_sync import start_auto_sync
        start_auto_sync()

    asyncio.create_task(_delayed_auto_sync())
            
    yield
    # 优雅关闭: 显式关 DuckDB(触发 meta.db WAL checkpoint), 防 taskkill /F 绕过 atexit 致 WAL 损坏
    try:
        db.close_all()
    except Exception as e:
        log.warning(f"lifespan 退出 db.close_all 异常: {e}")
    log.info("Eurica Quant - 睿奕量化 已停止")

app = FastAPI(title="Eurica Quant (睿奕量化)", lifespan=app_lifespan)

# 速率限制器（单例定义在 core.limiter，此处仅向 app 注册）
register_limiter(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 修复static路径找不到的问题
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

@app.middleware("http")
async def log_requests(request, call_next):
    log.debug(f"Request: {request.method} {request.url.path}")
    response = await call_next(request)
    # 静态文件禁用浏览器缓存，避免修改后用户看到旧版本
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    from fastapi.responses import Response
    return Response(content=b"", media_type="image/x-icon")


def _is_local(request: Request) -> bool:
    """仅允许本机访问(用于保护 /shutdown 等危险端点)"""
    client_host = request.client.host if request.client else ""
    return client_host in ("127.0.0.1", "::1", "localhost")


@app.post("/shutdown")
async def shutdown_service(request: Request):
    """优雅关闭服务(仅 localhost)。

    替代 stop.bat 的 taskkill /F —— 后者走 TerminateProcess 绕过 atexit,
    导致 meta.db 连接不 close、WAL 不 checkpoint(2026-07 反复损坏根因)。
    流程: CHECKPOINT 强制刷 WAL -> 延迟 0.5s 发 SIGINT -> uvicorn 优雅退出
    -> lifespan/atexit 跑完 -> DuckDB 正常关闭。
    """
    if not _is_local(request):
        raise HTTPException(403, "forbidden: localhost only")
    log.info("/shutdown 收到请求, 准备优雅关闭 (CHECKPOINT + SIGINT)...")

    # 1. 强制刷 meta.db WAL: CHECKPOINT 不受 threading.local 多连接归属影响
    try:
        db.conn.execute("CHECKPOINT;")
    except Exception as e:
        log.warning(f"/shutdown CHECKPOINT 异常: {e}")

    # 2. 后台线程延迟发 SIGINT, 让本响应先返回; uvicorn 收到后走优雅关闭
    def _trigger_exit():
        time.sleep(0.5)
        try:
            os.kill(os.getpid(), signal.SIGINT)
        except Exception as e:
            log.warning(f"/shutdown SIGINT 触发失败: {e}")

    threading.Thread(target=_trigger_exit, daemon=True).start()
    return {"status": "shutting down"}


# WebSocket Mounts
from server.websocket.handler import websocket_endpoint as new_websocket_endpoint
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await new_websocket_endpoint(ws)

# Import and attach all routers
from app.api import (
    market,
    watchlist,
    data_sync,
    backtest,
    screener,
    sentiment,
    factory,
    trade,
    system,
    agents,
    hot_sector,
    sim_trader,
    tqsdk,
)
from app.api.settings_extra import router as settings_extra_router

app.include_router(system.router)
app.include_router(market.router)
app.include_router(watchlist.router)
app.include_router(data_sync.router)
app.include_router(backtest.router)
app.include_router(screener.router)
app.include_router(sentiment.router)
app.include_router(factory.router)
app.include_router(trade.router)
app.include_router(agents.router)
app.include_router(hot_sector.router)
app.include_router(sim_trader.router)
app.include_router(tqsdk.router)
app.include_router(settings_extra_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8888)