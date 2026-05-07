from fastapi import APIRouter
from pathlib import Path
from core.logger import get_logger
from database.duckdb_manager import db

log = get_logger("API-Factory")
router = APIRouter()

ROOT_DIR = Path(__file__).resolve().parent.parent.parent

@router.get("/api/factory/strategies")
async def list_strategies():
    """获取所有策略列表（包括物理文件内容同步）"""
    df = db.get_strategies(active_only=False)
    records = df.to_dict(orient="records")
    strategy_root = ROOT_DIR / "app" / "screener" / "strategies"
    physical_files = {f.name.lower(): f for f in strategy_root.glob("*.py")}
    
    for r in records:
        name = r['name']
        target_filename = f"{name.lower()}.py"
        p = physical_files.get(target_filename)
        if p and p.exists():
            try:
                r['code_content'] = p.read_text(encoding="utf-8")
                # 尝试同步提取描述
                try:
                    from app.strategy_factory.engine import factory
                    r['description'] = factory.extract_docstring(p)
                except Exception:
                    pass  # docstring提取失败不影响主功能
            except Exception as e:
                log.error(f"读取策略文件 {name} 失败: {e}")
    return records

@router.post("/api/factory/sync_strategies")
async def sync_strategies():
    """触发物理目录扫描，将新文件同步至数据库"""
    from app.strategy_factory.engine import factory
    factory.sync_local_strategies()
    return {"status": "ok", "message": "本地策略库已同步"}

@router.post("/api/factory/save_physical_strategy")
async def save_strategy(req: dict):
    """保存策略内容到物理文件"""
    from app.strategy_factory.engine import factory
    # 强制统一返回格式
    res = factory.save_and_reload(req['name'], req['code_content'])
    if isinstance(res, dict) and res.get('status') == 'success':
        res['status'] = 'ok'
    return res

@router.post("/api/factory/test_syntax_secure")
async def test_strategy(req: dict):
    """高隔离度语法与信号测试接口"""
    from app.strategy_factory.engine import factory
    import traceback
    try:
        code = req.get('code_content', '')
        if not code: return {"status": "error", "message": "代码内容为空"}
        res = factory.test_run(code)
        if isinstance(res, dict) and "status" not in res:
            res["status"] = "ok"
        return res
    except Exception as e:
        log.error(f"语法测试崩溃: {traceback.format_exc()}")
        return {"status": "error", "message": f"引擎异常: {str(e)}"}

@router.post("/api/factory/destroy_physical_strategy")
async def physical_delete_strategy(req: dict):
    """物理销毁策略文件及数据库记录"""
    from app.strategy_factory.engine import factory
    name = req.get('name')
    if not name: return {"status": "error", "message": "未提供策略名称"}
    try:
        log.warning(f"☢️ [物理销毁] {name}")
        try:
            factory.delete_local_strategy(name)
        except Exception as e:
            log.warning(f"物理删除策略文件失败（可能已不存在）: {e}")
        db.conn.execute("DELETE FROM strategies WHERE name=?", [name])
        db.conn.commit() 
        factory.sync_local_strategies()
        return {"status": "ok", "message": f"物理销毁完成: {name}"}
    except Exception as e:
        log.error(f"物理删除异常: {e}")
        return {"status": "error", "message": str(e)}

@router.post("/api/factory/translate_tdx")
async def translate_tdx(req: dict):
    """通达信公式转 Python"""
    from app.strategy_factory.translator import translator
    code = translator.translate(req['formula'], req['name'])
    return {"status": "ok", "code": code}

@router.post("/api/factory/toggle_status")
async def toggle_strategy(req: dict):
    """切换策略有效/废弃状态"""
    db.set_strategy_status(req['id'], req['is_active'])
    return {"status": "ok"}

# ─── 持仓 API ─────────────────────────────────────────────────

