"""额外设置端点（小众配置）"""
from fastapi import APIRouter

from core.settings import settings

router = APIRouter()


@router.get("/api/settings/tqsdk-formula")
async def get_tqsdk_formula():
    formula_name = settings.get("tqsdk", "formula_name", default="QUANTQQ") or "QUANTQQ"
    return {"status": "ok", "formula_name": formula_name}


@router.put("/api/settings/tqsdk-formula")
async def set_tqsdk_formula(body: dict):
    """更新 TDX 公式名。保存后立即生效（settings 热加载 + bridge 下次调用自动读取）。"""
    formula_name = (body.get("formula_name") or "").strip()
    if not formula_name:
        return {"status": "error", "message": "公式名不能为空"}
    # 通过 settings.set 写入并持久化（不直接操作内部字典）
    settings.set("tqsdk", "formula_name", formula_name, save=True)
    settings.reload()
    return {"status": "ok", "formula_name": formula_name}