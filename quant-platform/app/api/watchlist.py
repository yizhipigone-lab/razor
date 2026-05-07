import pandas as pd
from fastapi import APIRouter
from database.duckdb_manager import db
from core.logger import get_logger

log = get_logger("API-Watchlist")
router = APIRouter(prefix="/api/watchlist", tags=["Watchlist"])

@router.get("")
async def api_get_watchlist(limit: int = 50, offset: int = 0):
    try:
        df = db.get_watchlist(limit=limit, offset=offset)
        if df is None or df.empty: return []
        
        # 补全 name 逻辑：更健壮的 apply 处理
        if 'name' not in df.columns or df['name'].isnull().any():
            def _get_name(row):
                if pd.isna(row.get('name')) or not row.get('name') or row['name'] == '-':
                    return db.get_stock_name_by_code(row['code'])
                return row['name']
            df['name'] = df.apply(_get_name, axis=1)
        
        # 时间格式转换
        if "added_at" in df.columns:
            df["added_at"] = df["added_at"].apply(lambda x: str(x) if x else "-")
            
        return df.fillna("-").to_dict(orient="records")
    except Exception as e:
        log.error(f"API/watchlist 意外崩溃: {e}")
        return []

@router.post("")
async def api_add_watchlist(body: dict):
    try:
        code = body.get("code", "").strip()
        name = body.get("name", "").strip()
        if not code: return {"status": "error", "message": "代码为空"}

        if not name or name == '-':
            name = db.get_stock_name_by_code(code)

        db.add_to_watchlist(code, name=name, source=body.get("source", "manual"))
        return {"status": "ok", "message": f"加入自选股: {name or code}"}
    except Exception as e:
        log.error(f"Watchlist 添加失败: {e}")
        return {"status": "error", "message": str(e)}

@router.delete("/{code}")
async def api_remove_watchlist(code: str):
    try:
        db.remove_from_watchlist(code)
        return {"status": "ok", "message": f"移出自选股: {code}"}
    except Exception as e:
        log.error(f"Watchlist 移除失败: {e}")
        return {"status": "error", "message": str(e)}
