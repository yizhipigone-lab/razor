"""实盘交易模块 FastAPI 入口(v5.4 §10)

路由聚合 + 生命周期管理 + 启动互斥(§16.8)。
端口 8001,NSSM 守护。
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.logger import get_logger

logger = get_logger("live_trader.main")


# ===== 全局组件(生命周期管理)=====
# _state 抽离到 _state.py(阶段0a, 2026-07-19):避免后续 routers/ 拆分时 main↔routers 循环 import
from ._state import state as _state
# 鉴权工具抽到 auth.py(阶段0b, 2026-07-19):纯函数,routers 拆分时共享依赖,不绕回 main
# _is_local 不 re-export:main 内部零使用(_require_admin 在 auth 内部闭环调 auth._is_local)
from .auth import _verify_token, _require_admin
# system 路由器抽到 routers/system.py(阶段1第1步, 2026-07-19)
from .routers.system import router as system_router
# market 路由器抽到 routers/market.py(阶段1第2步, 2026-07-19)
from .routers.market import router as market_router
# config_api 路由器抽到 routers/config_api.py(阶段1第3步, 2026-07-19)
from .routers.config_api import router as config_api_router
# trade 路由器抽到 routers/trade.py(阶段1第4步, 2026-07-19)
from .routers.trade import router as trade_router
# lifecycle 抽到 lifecycle.py(阶段2, 2026-07-19):lifespan 给 app + _takeover_positions re-export 给外部脚本/测试
from .lifecycle import lifespan, _takeover_positions


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

# ===== 子路由器(阶段1拆分)=====
app.include_router(system_router)
app.include_router(market_router)
app.include_router(config_api_router)
app.include_router(trade_router)


# ===== 路由 =====

# 简称缓存: code -> name(xtdata.get_instrument_detail 返回,股票/ETF/指数全覆盖)
# 只缓存非空结果,空结果不缓存(防 xtquant 抖动时永久踩死,下次轮询自动重试)
_instrument_name_cache: dict = {}


def _resolve_instrument_name(code: str, qmt) -> str:
    """补股票简称(xtdata.get_instrument_detail + 进程内缓存,只缓存非空)。
    /live/positions 与 get_risk_status 共用,避免名称解析逻辑重复。"""
    if not code:
        return ""
    name = _instrument_name_cache.get(code)
    if name is not None:
        return name
    if not qmt:
        return ""
    try:
        detail = qmt.get_instrument_detail(code) or {}
        n = str(detail.get("InstrumentName") or "").strip()
        if n:
            _instrument_name_cache[code] = n
        return n
    except Exception:
        return ""


# TODO: /live/quotes/subscribe (tick 订阅) — 暂缓
# 当前主服务 MarketBroadcaster 每 500ms 轮询 /live/quotes，已提供同等延迟的实时行情推送。
# 未来若需更低延迟的 push 模式，可实现 xtdata.subscribe_quote() + WebSocket 推送通道。


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
