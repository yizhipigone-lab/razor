from fastapi import APIRouter
from pydantic import BaseModel
from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db
log = get_logger('API-Trade')
router = APIRouter()

# ─── /api/trade/buy 与 /api/trade/sell 已废弃(2026-07-14) ─────────
# 真实下单唯一入口:live_trader:8001 /live/order(qmt_wrapper 直连 xtquant)
# sim_trader 永远不真下单,前端"手工买入/卖出"按钮保留但点击会返"已迁移"提示
# 前端不需改:fetch 这些 URL 仍能返回 JSON,只是 status=error(用户明确要求不改前端)
# 见 docs/审计报告/项目质量审计_2026-07-13_全项目.md H4 决定


@router.get("/api/positions")
async def get_positions():
    df = db.get_open_positions()
    return df.to_dict(orient="records")


class BuyRequest(BaseModel):
    code: str
    price: float
    reason: str = "手工买入"


@router.post("/api/trade/buy")
async def manual_buy(body: BuyRequest):
    """已废弃(2026-07-14):模拟盘不再真下单。请在 live_trader 面板手工下单。"""
    log.warning(f"[已废弃端点] /api/trade/buy {body.code} - 已迁移到 live_trader /live/order")
    return {
        "status": "error",
        "message": "该端点已废弃:模拟盘不再真下单。请使用 live_trader 面板(8001 端口)的'手工下单'功能。",
        "migrated_to": "/live/order (live_trader:8001)",
    }


class SellRequest(BaseModel):
    position_id: int
    code: str
    price: float
    volume: int
    reason: str = "手工卖出"


@router.post("/api/trade/sell")
async def manual_sell(body: SellRequest):
    """已废弃(2026-07-14):同 manual_buy。"""
    log.warning(f"[已废弃端点] /api/trade/sell {body.code} - 已迁移到 live_trader /live/order")
    return {
        "status": "error",
        "message": "该端点已废弃:模拟盘不再真下单。请使用 live_trader 面板(8001 端口)的'手工下单'功能。",
        "migrated_to": "/live/order (live_trader:8001)",
    }

# ─── 交易记录 API ──────────────────────────────────────────────
@router.get("/api/trades")
async def get_trades():
    df = db.get_trade_history(limit=200)
    return df.to_dict(orient="records")


# ─── 行情长连接 Webhook ─────────────────────────────────────────
class QuotesPushReq(BaseModel):
    type: str
    data: dict


