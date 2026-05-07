from fastapi import APIRouter
from pydantic import BaseModel
from core.logger import get_logger
from core.settings import settings, calc_buy_volume
from core.gateway import get_gateway
from database.duckdb_manager import db
log = get_logger('API-Trade')
router = APIRouter()

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
    volume = calc_buy_volume(body.price)
    if volume <= 0:
        return {"status": "error", "message": f"股价 {body.price} 超出单笔限额 {settings.max_buy_amount}"}
    # 从统一网关获取实例
    gw = get_gateway()
    try:
        gw.buy(body.code, body.price, volume, reason=body.reason)
    except Exception as e:
        log.error(f"网关买入失败，未记录持仓: {e}")
        return {"status": "error", "message": f"网关下单失败: {e}"}
    # 网关成功后才记录持仓
    pos_id = db.open_position(
        code=body.code, name=body.code,
        price=body.price, volume=volume,
        source="manual"
    )
    db.record_trade(pos_id, body.code, body.code, "BUY", body.price, volume, "manual", body.reason)
    return {"status": "ok", "code": body.code, "volume": volume, "amount": body.price * volume}

class SellRequest(BaseModel):
    position_id: int
    code: str
    price: float
    volume: int
    reason: str = "手工卖出"

@router.post("/api/trade/sell")
async def manual_sell(body: SellRequest):
    gw = get_gateway()
    try:
        gw.sell(body.code, body.price, body.volume, reason=body.reason)
    except Exception as e:
        log.error(f"网关卖出失败，未记录交易: {e}")
        return {"status": "error", "message": f"网关卖出失败: {e}"}
    # 网关成功后才记录交易
    db.record_trade(body.position_id, body.code, body.code, "SELL", body.price, body.volume, "manual", body.reason)
    db.reduce_position(body.position_id, body.volume)
    return {"status": "ok"}

# ─── 交易记录 API ──────────────────────────────────────────────
@router.get("/api/trades")
async def get_trades():
    df = db.get_trade_history(limit=200)
    return df.to_dict(orient="records")


# ─── 行情长连接 Webhook ─────────────────────────────────────────
class QuotesPushReq(BaseModel):
    type: str
    data: dict


