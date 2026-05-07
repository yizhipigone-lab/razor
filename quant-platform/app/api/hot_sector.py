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

@router.get("/api/hot/sector/{name}/stocks")
async def get_sector_stocks(name: str):
    """获取行业板块成分股"""
    try:
        rows = db.conn.execute(
            "SELECT code, name FROM stocks WHERE sector = ? AND status = 'active' ORDER BY code",
            [name]
        ).fetchall()
        return [{"code": r[0], "name": r[1]} for r in rows]
    except Exception as e:
        log.error(f"查询板块成分股失败 {name}: {e}")
        return []


@router.get("/api/hot/concept/{name}/stocks")
async def get_concept_stocks(name: str):
    """获取概念板块成分股"""
    try:
        rows = db.conn.execute(
            "SELECT cs.stock_code, s.name FROM concept_stocks cs "
            "LEFT JOIN stocks s ON cs.stock_code = s.code "
            "WHERE cs.concept_name = ? ORDER BY cs.stock_code",
            [name]
        ).fetchall()
        return [{"code": r[0], "name": r[1] or ""} for r in rows]
    except Exception as e:
        log.error(f"查询概念成分股失败 {name}: {e}")
        return []


@router.get("/api/hot/stock/{code}")
async def get_stock_hot_score(code: str):
    """查询单只股票的板块/概念评分详情"""
    try:
        result = hot_sector_engine.get_stock_sector_score(code)
        return {"status": "ok", "data": result}
    except Exception as e:
        log.error(f"查询个股评分失败 {code}: {e}")
        return {"status": "error", "data": {"code": code, "composite_score": 0}}


@router.get("/api/hot/stocks/batch")
async def batch_stock_scores(codes: str = Query(..., description="逗号分隔的股票代码")):
    """批量查询多只股票的板块评分（供策略调用）"""
    try:
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
        if not code_list:
            return {}
        scores = hot_sector_engine.batch_score_stocks(code_list)
        return scores
    except Exception as e:
        log.error(f"批量评分失败: {e}")
        return {}


@router.post("/api/hot/refresh")
async def refresh_hotness():
    """手动触发热度全量重算"""
    try:
        summary = hot_sector_engine.refresh_hotness()
        return {"status": "ok", "summary": summary}
    except Exception as e:
        log.error(f"热度刷新失败: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/api/hot/sync_concepts")
async def sync_concepts():
    """手动触发 Tushare 概念数据同步"""
    try:
        result = concept_syncer.sync_all()
        return {"status": result.get("status", "ok"), "data": result}
    except Exception as e:
        log.error(f"概念同步失败: {e}")
        return {"status": "error", "message": str(e)}
