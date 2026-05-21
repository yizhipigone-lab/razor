# 固定项目路径，解决所有模块找不到问题
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio

from core.logger import get_logger
from core.settings import settings
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

    # 延迟 5 秒后启动自动数据补齐（确保页面优先加载完毕）
    async def _delayed_auto_sync():
        await asyncio.sleep(5)
        from app.data_manager.auto_sync import start_auto_sync
        start_auto_sync()

    asyncio.create_task(_delayed_auto_sync())
            
    yield
    log.info("Eurica Quant - 睿奕量化 已停止")

app = FastAPI(title="Eurica Quant (睿奕量化)", lifespan=app_lifespan)

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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8888)