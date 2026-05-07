"""
持久化同步日志：按天轮转，保留 30 天，同时实时推送到 WebSocket。
"""

import os
import glob
import logging
from datetime import datetime, timedelta
from pathlib import Path

_LOG_DIR = None
_file_handler = None
_logger = None


def _ensure_log_dir() -> str:
    global _LOG_DIR
    if _LOG_DIR is None:
        _LOG_DIR = os.path.join(Path(__file__).resolve().parent.parent, "logs")
        os.makedirs(_LOG_DIR, exist_ok=True)
    return _LOG_DIR


def get_sync_logger() -> logging.Logger:
    """获取或创建同步日志记录器（单例）"""
    global _logger, _file_handler

    if _logger is not None:
        return _logger

    log_dir = _ensure_log_dir()

    _logger = logging.getLogger("SyncAuto")
    _logger.setLevel(logging.INFO)
    _logger.propagate = False

    # 如果已经有 handler 就清掉（避免重复注册）
    if _logger.handlers:
        _logger.handlers.clear()

    # 按天轮转：每天一个文件
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = os.path.join(log_dir, f"sync_{today}.log")
    _file_handler = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    _file_handler.setLevel(logging.INFO)
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    _logger.addHandler(_file_handler)

    # 清理 30 天前的旧日志
    _cleanup_old_logs(log_dir)

    return _logger


def _cleanup_old_logs(log_dir: str, days: int = 30):
    """删除超过 days 天的同步日志"""
    cutoff = datetime.now() - timedelta(days=days)
    for fpath in glob.glob(os.path.join(log_dir, "sync_*.log")):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
            if mtime < cutoff:
                os.remove(fpath)
        except Exception:
            pass


def info(msg: str):
    """记录 INFO 日志并广播到 WebSocket"""
    _log_and_broadcast(msg, "info")


def warn(msg: str):
    """记录 WARNING 日志并广播到 WebSocket"""
    _log_and_broadcast(msg, "warning")


def error(msg: str):
    """记录 ERROR 日志并广播到 WebSocket"""
    _log_and_broadcast(msg, "error")


def ok(msg: str):
    """记录成功日志并广播到 WebSocket"""
    _log_and_broadcast(f"✅ {msg}", "info")


def _log_and_broadcast(msg: str, level: str):
    """写日志文件 + 推送到前端 WebSocket"""
    logger = get_sync_logger()
    log_fn = getattr(logger, level, logger.info)
    log_fn(msg)

    # WebSocket 广播
    try:
        from server.websocket.manager import sync_broadcast
        sync_broadcast({"type": "log", "level": level, "msg": f"[AutoSync] {msg}"})
    except Exception:
        pass  # WS 不可用时安静降级
