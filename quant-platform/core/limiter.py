"""全局速率限制器单例。

抽离到独立模块，避免 main ↔ app.api.agents 之间因 `from main import limiter`
形成循环依赖。main.py 与 agents.py 均从此处导入同一个 limiter 实例。
"""
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# 全局单例：所有路由共用，确保速率计数统一
limiter = Limiter(key_func=get_remote_address)


def register_limiter(app) -> None:
    """在 FastAPI 应用上注册 limiter 与 429 异常处理器。"""
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
