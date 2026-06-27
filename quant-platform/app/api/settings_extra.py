"""额外设置端点（小众配置）"""
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/settings/tqsdk-formula")
async def get_tqsdk_formula():
    from core.settings import settings
    formula_name = settings.get("tqsdk", "formula_name", default="QUANTQQ") or "QUANTQQ"
    return {"status": "ok", "formula_name": formula_name}


@router.put("/api/settings/tqsdk-formula")
async def set_tqsdk_formula(body: dict):
    """更新 TDX 公式名。注：热生效需重启 bridge 进程。"""
    formula_name = (body.get("formula_name") or "").strip()
    if not formula_name:
        return {"status": "error", "message": "公式名不能为空"}
    from core.settings import settings
    if "tqsdk" not in settings._data:
        settings._data["tqsdk"] = {}
    settings._data["tqsdk"]["formula_name"] = formula_name
    settings.save()
    return {"status": "ok", "formula_name": formula_name}