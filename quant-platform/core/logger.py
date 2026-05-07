"""
审计日志模块 - 基于 loguru 实现每日滚动日志 + 结构化审计日志
所有的买卖、风控信号、选股结果，都必须通过此模块记录并附带原因。
"""
import sys
from pathlib import Path
from loguru import logger as _logger

ROOT_DIR = Path(__file__).parent.parent
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

def custom_formatter(record):
    """自定义格式化函数，处理缺失 module 的情况"""
    module = record["extra"].get("module", "System")
    # 注意: loguru 的自定义 formatter 应该返回一个格式字符串或者直接格式化好的字符串
    # 如果返回带 {} 的字符串，loguru 会继续对其进行格式化 (处理 time, level 等)
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        f"<cyan>{module:<10}</cyan> | "
        "<level>{message}</level>\n"
    )

def audit_formatter(record):
    module = record["extra"].get("module", "System")
    return f"{{time:YYYY-MM-DD HH:mm:ss}} | {{level}} | {module} | {{message}}\n"

# 移除默认 stderr 输出，重新配置
_logger.remove()

# 控制台日志：高清晰格式
_logger.add(
    sys.stderr,
    format=custom_formatter,
    level="DEBUG",
    colorize=True,
)

# 文件日志：每天一个文件，保留 30 天
_logger.add(
    LOG_DIR / "{time:YYYY-MM-DD}.log",
    format=custom_formatter,
    level="INFO",
    rotation="00:00",       # 每日 0 点滚动
    retention="30 days",    # 保留 30 天
    encoding="utf-8",
    enqueue=True,           # 异步写入，不阻塞主线程
)

# 单独的审计日志文件：只记录交易操作
_logger.add(
    LOG_DIR / "audit_{time:YYYY-MM}.log",
    format=audit_formatter,
    level="INFO",
    rotation="1 month",
    retention="365 days",
    filter=lambda r: r["extra"].get("audit") is True,
    encoding="utf-8",
    enqueue=True,
)


def get_logger(module: str):
    """
    获取带模块标记的日志记录器。
    """
    return _logger.bind(module=module)


def get_audit_logger(module: str):
    """
    获取审计日志记录器
    """
    return _logger.bind(module=module, audit=True)
