"""
热点板块/概念 API 路由
"""

from fastapi import APIRouter, Query
from core.logger import get_logger
from database.duckdb_manager import db
from app.hot_sector.engine import hot_sector_engine
from app.hot_sector.concept_sync import concept_syncer

log = get_logger("API-HotSector")
router = APIRouter(tags=["HotSector"])


@router.get("/api/hot/sectors")
async def get_hot_sectors(limit: int = 20, force_refresh: bool = False):
    """获取热门行业板块 TOP N"""
    if force_refresh:
        hot_sector_engine.refresh_hotness()
    df = hot_sector_engine.get_top_sectors(limit=limit)
    return df.to_dict(orient="records") if not df.empty else []


@router.get("/api/hot/concepts")
async def get_hot_concepts(limit: int = 20, min_stocks: int = 3):
    """获取热门概念 TOP N（最少成分股过滤）"""
    df = hot_sector_engine.get_top_concepts(limit=limit, min_stocks=min_stocks)
    return df.to_dict(orient="records") if not df.empty else []


@router.get("/api/hot/last-updated")
async def get_last_updated():
    """获取最近一次热度重算的时间"""
    return {"last_updated": hot_sector_engine.last_updated}


@router.get("/api/hot/stock/{code}")
async def get_stock_hot_score(code: str):
    """查询单只股票的板块/概念评分详情"""
    try:
        result = hot_sector_engine.get_stock_sector_score(code)
        return {"status": "ok", "data": result}
    except Exception as e:
        log.error(f"查询个股评分失败 {code}: {e}")
        return {"status": "error", "data": {"code": code, "composite_score": 0}}


@router.post("/api/hot/refresh")
async def refresh_hotness():
    """手动触发热度全量重算"""
    try:
        summary = hot_sector_engine.refresh_hotness()
        return {"status": "ok", "summary": summary}
    except Exception as e:
        log.error(f"热度刷新失败: {e}")
        return {"status": "error", "message": str(e)}
