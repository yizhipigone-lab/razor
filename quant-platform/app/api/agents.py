"""
报告仓库 API — 分析/列表/详情/删除/异步任务
"""
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict

from fastapi import APIRouter, Request
from core.limiter import limiter  # 全局单例，避免 from main import limiter 的循环依赖

from core.logger import get_logger
from pydantic import BaseModel
from database.duckdb_manager import db

log = get_logger('API-Agents')
router = APIRouter()

# ─── 输入校验 ─────────────────────────────────────────────
def _validate_stock_code(code: str) -> bool:
    """校验股票代码格式：6位数字，可选.SH/.SZ后缀"""
    return bool(re.match(r'^\d{6}(\.(SH|SZ))?$', code.strip()))

# ─── 异步任务管理器 ────────────────────────────────────────
_report_tasks: Dict[str, dict] = {}
_task_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-report")


def _do_generate(task_id: str, stock_code: str, name: str):
    """后台线程执行报告生成"""
    try:
        with _task_lock:
            _report_tasks[task_id]["status"] = "generating"
            _report_tasks[task_id]["progress"] = 10

        from app.agents.committee import generate_ai_report, parse_score_and_summary, parse_subscores, strip_header
        raw_report = generate_ai_report(stock_code)

        with _task_lock:
            _report_tasks[task_id]["progress"] = 80

        if not raw_report:
            raise ValueError("AI 未能生成有效的诊断内容")

        # P3-5: 解析结构化评分、摘要、分项得分
        score, summary = parse_score_and_summary(raw_report)
        subscores = parse_subscores(raw_report)
        # 剥离头部后的正文用于展示
        content = strip_header(raw_report) if (score is not None or summary is not None) else raw_report

        res = db.save_ai_report(stock_code, name, content,
                                summary=summary, score=score, subscores=subscores)

        with _task_lock:
            _report_tasks[task_id]["status"] = "done"
            _report_tasks[task_id]["progress"] = 100
            _report_tasks[task_id]["report"] = content
            _report_tasks[task_id]["report_id"] = res
            _report_tasks[task_id]["score"] = score
            _report_tasks[task_id]["summary"] = summary
            _report_tasks[task_id]["subscores"] = subscores

        log.info(f"✅ 异步报告生成完成: {task_id} ({stock_code}) score={score} subscores={subscores}")

    except Exception as e:
        import traceback
        with _task_lock:
            _report_tasks[task_id]["status"] = "error"
            _report_tasks[task_id]["error"] = str(e)
        log.error(f"❌ 异步报告生成失败 {task_id}: {e}\n{traceback.format_exc()}")


def _cleanup_old_tasks():
    """清理 >30 分钟的已完成任务"""
    now = time.time()
    with _task_lock:
        expired = [tid for tid, t in _report_tasks.items()
                   if t["status"] in ("done", "error") and now - t.get("started_at", now) > 1800]
        for tid in expired:
            del _report_tasks[tid]


# ─── API 端点 ─────────────────────────────────────────────

@router.post("/api/agents/analyze/{stock_code}")
@limiter.limit("5/minute")
def start_analysis(request: Request, stock_code: str, name: str = ""):
    """启动报告生成任务（异步后台线程）"""
    if not _validate_stock_code(stock_code):
        return {"status": "error", "message": f"无效的股票代码格式: {stock_code}"}

    _cleanup_old_tasks()

    task_id = f"rpt_{stock_code.split('.')[0]}_{int(time.time())}"

    with _task_lock:
        _report_tasks[task_id] = {
            "status": "started",
            "progress": 0,
            "stock_code": stock_code,
            "name": name,
            "started_at": time.time(),
        }

    _executor.submit(_do_generate, task_id, stock_code, name)
    log.info(f"🚀 报告生成任务已提交: {task_id} ({stock_code})")
    return {"status": "started", "task_id": task_id}


# ─── P3-3: 批量报告生成 ───────────────────────────────────

BATCH_MAX_CODES = 20  # 单次批量上限

class BatchReportReq(BaseModel):
    codes: list[str]  # 股票代码列表

@router.post("/api/agents/analyze/batch")
@limiter.limit("2/minute")
def batch_analyze(request: Request, req: BatchReportReq):
    """批量启动报告生成任务（逐只异步）

    前端传入 {"codes": ["600519", "000858"]}，后端逐个校验并提交后台任务。
    """
    _cleanup_old_tasks()

    if not req.codes:
        return {"status": "error", "message": "代码列表为空"}
    if len(req.codes) > BATCH_MAX_CODES:
        return {"status": "error", "message": f"单次最多 {BATCH_MAX_CODES} 只"}

    accepted = []
    rejected = []
    ts = int(time.time())
    for code in req.codes:
        code = str(code).strip()
        if not code:
            continue
        if not _validate_stock_code(code):
            rejected.append({"code": code, "reason": "代码格式无效"})
            continue
        clean = code.split('.')[0]
        task_id = f"rpt_{clean}_{ts}"
        with _task_lock:
            _report_tasks[task_id] = {
                "status": "queued",
                "progress": 0,
                "stock_code": code,
                "name": "",
                "started_at": time.time(),
            }
        _executor.submit(_do_generate, task_id, code, "")
        accepted.append({"code": code, "task_id": task_id})

    log.info(f"📦 批量生成: {len(accepted)} 已提交, {len(rejected)} 拒绝")
    return {
        "status": "started",
        "accepted": accepted,
        "rejected": rejected,
        "total": len(accepted),
    }


@router.get("/api/agents/task/{task_id}")
def get_task_status(task_id: str):
    """查询报告生成任务状态"""
    with _task_lock:
        task = _report_tasks.get(task_id)
    if not task:
        return {"status": "error", "message": "任务不存在"}

    result = {
        "status": task["status"],
        "progress": task.get("progress", 0),
        "stock_code": task.get("stock_code", ""),
        "name": task.get("name", ""),
    }
    if task["status"] == "done":
        result["report_id"] = task.get("report_id")
    if task["status"] == "error":
        result["error"] = task.get("error", "未知错误")
    return result


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


@router.delete("/api/reports/{report_id}")
def delete_ai_report(report_id: int):
    """删除指定报告"""
    try:
        ok = db.delete_ai_report(report_id)
        if ok:
            return {"status": "ok", "message": f"报告 {report_id} 已删除"}
        return {"status": "error", "message": "删除失败"}
    except Exception as e:
        log.error(f"删除报告失败: {e}")
        return {"status": "error", "message": str(e)}


# ─── 策略生成（非报告仓库，保留原有功能）──────────────────

class AgentStrategyGenReq(BaseModel):
    prompt: str


# ─── Trend Radar (舆情与事件驱动) ──────────────────────────
