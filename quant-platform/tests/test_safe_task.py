"""safe_task.TaskRunner 单元测试(候选④)

锁定调度入口深 module 契约:同步/异步执行 + 异常吞掉 + log.exception。
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from app.scheduler.safe_task import TaskRunner


# ===== 1. 同步执行 =====

class TestRunSync:
    def test_returns_fn_result_on_success(self):
        runner = TaskRunner("test")
        result = runner.run_sync(lambda: 42)
        assert result == 42

    def test_passes_args_kwargs(self):
        runner = TaskRunner("test")
        result = runner.run_sync(lambda x, y=10: x + y, 5, y=20)
        assert result == 25

    def test_swallows_exception_and_returns_none(self):
        runner = TaskRunner("test")
        log = MagicMock()
        runner._log = log
        result = runner.run_sync(lambda: 1 / 0)
        assert result is None
        log.exception.assert_called_once()
        # 检查日志内容含任务名
        call_args = log.exception.call_args
        assert "[test]" in call_args[0][0]
        assert "同步任务执行异常" in call_args[0][0]

    def test_does_not_propagate_cancelled_error(self):
        # asyncio.CancelledError 在 Python 3.9+ 继承自 BaseException,
        # 不属于 Exception,本模块只吞 Exception,应该让 Cancel 一路传出
        # (调度器取消场景不能被静默化)
        runner = TaskRunner("test")
        log = MagicMock()
        runner._log = log

        def raises_cancel_sync():
            raise asyncio.CancelledError()

        # run_sync 调同步函数 → CancelledError 应不被吞
        with pytest.raises(asyncio.CancelledError):
            runner.run_sync(raises_cancel_sync)
        log.exception.assert_not_called()


# ===== 2. 异步执行 =====

class TestRunAsync:
    def test_async_function_awaited(self):
        runner = TaskRunner("test")
        async def afn():
            return "async-result"
        result = asyncio.run(runner.run_async(afn))
        assert result == "async-result"

    def test_sync_function_works_too(self):
        runner = TaskRunner("test")
        result = asyncio.run(runner.run_async(lambda: "sync-in-async"))
        assert result == "sync-in-async"

    def test_swallows_async_exception(self):
        runner = TaskRunner("test")
        log = MagicMock()
        runner._log = log

        async def afn():
            raise ValueError("boom")

        result = asyncio.run(runner.run_async(afn))
        assert result is None
        log.exception.assert_called_once()
        assert "[test]" in log.exception.call_args[0][0]

    def test_passes_args_kwargs(self):
        runner = TaskRunner("test")

        async def afn(x, y=0):
            return x * y

        result = asyncio.run(runner.run_async(afn, 3, y=4))
        assert result == 12


# ===== 3. 自定义 logger =====

class TestCustomLogger:
    def test_default_logger_used(self):
        runner = TaskRunner("custom_name")
        # 默认 log 应有 logger 提供的 logger
        assert runner._log is not None
        assert hasattr(runner._log, "exception")

    def test_custom_logger_accepted(self):
        custom = MagicMock()
        runner = TaskRunner("custom_name", log=custom)
        runner.run_sync(lambda: 1 / 0)
        custom.exception.assert_called_once()


# ===== 4. 命名空间 =====

class TestNaming:
    def test_name_in_log_message(self):
        runner = TaskRunner("very.specific.name")
        log = MagicMock()
        runner._log = log
        runner.run_sync(lambda: 1 / 0)
        assert "very.specific.name" in log.exception.call_args[0][0]


# ===== 5. 集成模式 =====

class TestIntegration:
    """模拟真实 LiveScheduler / DataPipelineScheduler 调用模式"""

    def test_live_scheduler_run_exit_scan_pattern(self):
        """LiveScheduler._run_exit_scan 用 run_sync(scan_once) 模式"""
        runner = TaskRunner("exit_scan")
        scan_once = MagicMock(return_value=[{"action": "sell_a"}, {"action": "sell_b"}])
        result = runner.run_sync(scan_once)
        assert result == [{"action": "sell_a"}, {"action": "sell_b"}]
        scan_once.assert_called_once()

    def test_live_scheduler_exception_logs_and_returns_none(self):
        """LiveScheduler._run_quotes_refresh 等遇到 QMT 断开,异常吞掉"""
        runner = TaskRunner("quotes_refresh")
        log = MagicMock()
        runner._log = log
        qmt = MagicMock()
        qmt.get_realtime_quotes.side_effect = ConnectionError("QMT 断开")
        result = runner.run_sync(lambda: qmt.get_realtime_quotes(["000001.SZ"]))
        assert result is None
        log.exception.assert_called_once()

    def test_cron_simple_pattern(self):
        """DataPipelineScheduler.sync_concepts_daily 等简单任务模式"""
        runner = TaskRunner("sync_concepts_daily")
        concept_syncer = MagicMock()
        result = asyncio.run(runner.run_async(concept_syncer.sync_all))
        assert result is concept_syncer.sync_all.return_value
        concept_syncer.sync_all.assert_called_once()
