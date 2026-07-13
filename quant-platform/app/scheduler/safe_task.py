"""调度入口深 module — 候选④

设计:小接口大实现 — 统一 try/except + exc_info logging 样板。

调用方契约:
- 简单任务(LiveScheduler._run_* × 6 + cron_jobs 中的纯执行任务)
  委托 runner.run_sync() 或 await runner.run_async()。
- 复杂任务(多级 fallback 如 sync_index_daily Tushare→QMT→akshare)
  仍保持原 try/except 链 — 不是本模块覆盖范围。

之前散点问题:14+ cron 任务 + 6+ LiveScheduler._run_*
每处都重复 `try: ... except Exception as e: log.error(..., exc_info=True)`。
本模块固定该样板为深 module,异常吞掉(不阻塞调度主循环),
所有调用点写成单行 `runner.run_sync(work_fn)`。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from core.logger import get_logger

logger = get_logger("scheduler.safe_task")


class TaskRunner:
    """调度入口深 module — 隐藏 try/except + exc_info logging。

    实例级别命名 — 一个 TaskRunner 对应一个语义任务名(如 "exit_scan")。
    异常吞掉 + 用 logger.exception 记 traceback,不向调用方抛。
    主调度循环在异常发生时仍继续(防止单任务崩溃拖整个调度器)。

    使用:
        runner = TaskRunner("exit_scan")
        result = runner.run_sync(do_exit_scan)   # 同步函数
        result = await runner.run_async(do_sync_async_fn)   # async 函数
    """

    def __init__(self, name: str, log=None):
        self.name = name
        self._log = log or logger

    def run_sync(self, fn: Callable, *args, **kwargs) -> Optional[Any]:
        """执行同步函数;异常吞掉并 log(含 exc_info);不影响调度主循环。

        Returns:
            fn 的返回值;异常时 None。
        """
        try:
            return fn(*args, **kwargs)
        except Exception:
            self._log.exception(f"[{self.name}] 同步任务执行异常")
            return None

    async def run_async(self, fn: Callable, *args, **kwargs) -> Optional[Any]:
        """执行 async(或 sync)函数;coroutine 用 await,sync 函数直接调。

        Returns:
            fn 的返回值;异常时 None(协程也走 except 路径)。
        """
        try:
            if asyncio.iscoroutinefunction(fn):
                return await fn(*args, **kwargs)
            return fn(*args, **kwargs)
        except Exception:
            self._log.exception(f"[{self.name}] 异步任务执行异常")
            return None
