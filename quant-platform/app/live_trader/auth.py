"""实盘交易模块鉴权工具。

阶段 0b（2026-07-19）从 main.py 抽离：纯函数集合，为后续 routers/ 拆分提供
共享鉴权依赖（routers 可 `from .auth import _require_admin` 不绕回 main，避免循环 import）。

设计：纯函数 + 显式传参（config/request），无 _state 依赖、无闭包，可独立测试。
注意：_require_admin 内部调 _is_local，解析到 **本模块** 的 _is_local ——
测试打桩须 patch `app.live_trader.auth._is_local`，patch main._is_local 无效。

历史：
  - _verify_token : 原 main.py:962
  - _is_local     : 原 main.py:1342
  - _require_admin: 原 main.py:1349
"""
import hmac

from fastapi import HTTPException

from core.logger import get_logger

logger = get_logger("live_trader.auth")


def _verify_token(auth_header: str, config) -> bool:
    """验证 Bearer token（C1 修复: 未配 token → fail-closed, 拒所有请求）"""
    if not config or not config.buy_signal_token:
        logger.error("buy_signal_token 未配置, 所有 buy-signal 请求已被拒绝。请在 app_setting.json[live_trader] 中设置 buy_signal_token。")
        return False
    if not auth_header:
        return False
    parts = auth_header.split(" ")
    if len(parts) != 2 or parts[0] != "Bearer":
        return False
    # H5 修复(审计):常量时间比较,防时序攻击逐字节探测 token
    return hmac.compare_digest(parts[1], config.buy_signal_token)


def _is_local(request) -> bool:
    """v2(A8): admin 接口仅允许本地调用(防远程误操作真钱)"""
    if not request or not getattr(request, "client", None):
        return False
    return request.client.host in ("127.0.0.1", "::1", "localhost")


def _require_admin(request):
    """admin 端点护栏: 仅允许本机调用(浏览器 UI 同机访问)。

    浏览器前端无法持有服务端 buy_signal_token, 故本机写操作(测试通知/切换开关/
    改比例/改模式/优雅关闭等)仅靠 _is_local 防护——live_trader 仅监听 127.0.0.1,
    远程不可达。外部 API 调用(如 /live/buy-signal)不经过本函数, 仍由 _verify_token
    强制 Bearer token 校验, 保持 fail-closed。
    """
    if not _is_local(request):
        raise HTTPException(403, "仅允许本地访问")
