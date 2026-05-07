from fastapi import APIRouter
from core.logger import get_logger
from pydantic import BaseModel
from database.duckdb_manager import db

log = get_logger('API-Agents')
router = APIRouter()

@router.get("/api/agents/analyze/{stock_code}")
def analyze_stock(stock_code: str, name: str = ""):
    """新股听诊分析：生成报告并强力持久化"""
    try:
        from app.agents.committee import generate_ai_report
        log.info(f"🚀 开始为 {stock_code} ({name}) 生成诊断报告...")
        report = generate_ai_report(stock_code)
        
        if not report:
            raise ValueError("AI 未能生成有效的诊断内容")
            
        # 自动持久化落库
        res = db.save_ai_report(stock_code, name, report)
        if res == -1:
            log.error(f"❌ 警告: 报告生成成功，但物理落库失败 (Check ai_reports Table Status)")
            return {"status": "error", "message": "报告生成成功但存库失败", "report": report}
        else:
            log.info(f"✅ AI 报告已成功存库，ID: {res}")
            
        return {"status": "ok", "report": report, "id": res}
    except Exception as e:
        import traceback
        log.error(f"AI Analyse Critical Failure: {e}")
        log.error(traceback.format_exc())
        return {"status": "error", "message": str(e)}

@router.get("/api/reports/list")
def list_ai_reports(limit: int = 10, offset: int = 0, search: str = ""):
    try:
        res = db.get_ai_reports(limit, offset, search)
        return {"status": "ok", "data": res}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/api/reports/content/{report_id}")
def get_ai_report_content(report_id: int):
    try:
        rep = db.get_ai_report_by_id(report_id)
        if rep:
            return {"status": "ok", "data": rep}
        return {"status": "error", "message": "Report not found"}
    except Exception as e:
        log.error(f"获取报告内容失败: {e}")
        return {"status": "error", "message": str(e)}

class AgentStrategyGenReq(BaseModel):
    prompt: str

@router.post("/api/agents/generate_strategy")
def generate_strategy(req: AgentStrategyGenReq):
    try:
        from app.agents.strategy_coder import strategy_coder
        code_content = strategy_coder.generate_strategy(req.prompt)
        return {"status": "ok", "code": code_content}
    except Exception as e:
        log.error(f"AI Strategy Gen Error: {e}")
        return {"status": "error", "message": str(e)}

# ─── Trend Radar (舆情与事件驱动) ──────────────────────────
