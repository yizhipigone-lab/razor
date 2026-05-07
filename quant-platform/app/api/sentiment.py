from fastapi import APIRouter
from pydantic import BaseModel
from core.logger import get_logger
from database.duckdb_manager import db

log = get_logger("API-Sentiment")
router = APIRouter()

@router.get("/api/sentiment/history")
async def get_sentiment_history():
    try:
        df = db.get_sentiment_history(limit=50)
        return df.to_dict(orient="records")
    except Exception as e:
        log.error(f"获取情感分析历史失败: {e}")
        return []

@router.get("/api/sentiment/history/{sid}")
async def get_sentiment_detail(sid: int):
    try:
        return db.get_sentiment_by_id(sid) or {}
    except Exception as e:
        log.error(f"获取情感分析详情失败: {e}")
        return {}

class SentimentFetchRequest(BaseModel):
    hours: int = 4
    count: int = 15

@router.post("/api/sentiment/fetch")
async def fetch_sentiment(req: SentimentFetchRequest):
    try:
        from app.agents.concept_miner import concept_miner
        news = concept_miner.fetch_recent_news(hours=req.hours)
        if not news:
            return {"error": "未获取到有效快讯"}
        result = concept_miner.analyze_and_extract(news, target_count=req.count)
        return {"success": True, "data": result}
    except Exception as e:
        log.error(f"热点获取异常: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}

# ─── 策略工厂 API ─────────────────────────────────────────────


