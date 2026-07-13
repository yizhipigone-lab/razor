"""_run_worker 进度报告 + 超时 + stop_event + diag 键名 单元测试

审计 MEDIUM-7: 补单元测试。mock subprocess.Popen, 验证:
1. progress 行 → progress_cb 被调用(参数对)
2. status==ok 行 → 返回 result
3. worker 超时(永不退出) → kill + error
4. stop_event set → kill + stopped
5. worker_signals diag(新键名) → 不 KeyError
"""
import json
import subprocess
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from app.tqsdk.bridge import TdxBridge


class FakeProc:
    """模拟 subprocess.Popen 返回的 proc 对象"""

    def __init__(self, stdout_lines=None, stderr_text="", never_exit=False):
        self._lines = stdout_lines or []
        self._stderr = stderr_text
        self._never_exit = never_exit
        self._killed = False
        # proc.stdout / proc.stderr 被 `for line in proc.stdout/stderr` 消费
        self.stdout = iter(self._lines)
        self.stderr = iter([self._stderr] if self._stderr else [])

    def wait(self, timeout=None):
        if self._never_exit and not self._killed:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return 0

    def poll(self):
        if self._never_exit and not self._killed:
            return None
        return 0

    def kill(self):
        self._killed = True


def _make_bridge():
    """绕过 TdxBridge.__init__(避免触发 _ensure_worker_deployed 部署 worker 到 TDX 目录)"""
    return TdxBridge.__new__(TdxBridge)


def test_progress_forwarded():
    """progress 行 → progress_cb 被调用, 参数 (done, total, msg) 正确"""
    lines = [
        json.dumps({"progress": "screen", "done": 1, "total": 3, "msg": "扫描 1/3 批"}) + "\n",
        json.dumps({"progress": "screen", "done": 2, "total": 3, "msg": "扫描 2/3 批"}) + "\n",
        json.dumps({"status": "ok", "signals": {}, "prices": {}, "total": 0}) + "\n",
    ]
    proc = FakeProc(stdout_lines=lines)
    calls = []
    with patch("app.tqsdk.bridge.subprocess.Popen", return_value=proc):
        b = _make_bridge()
        result = b._run_worker(
            {"task_type": "range"},
            progress_cb=lambda d, t, m: calls.append((d, t, m)),
        )
    assert result["status"] == "ok"
    assert len(calls) == 2
    assert calls[0] == (1, 3, "扫描 1/3 批")
    assert calls[1] == (2, 3, "扫描 2/3 批")


def test_status_ok_returned():
    """status==ok 行 → 返回 result(含 signals)"""
    lines = [json.dumps({"status": "ok", "signals": {"000001.SZ": {}}, "total": 1}) + "\n"]
    proc = FakeProc(stdout_lines=lines)
    with patch("app.tqsdk.bridge.subprocess.Popen", return_value=proc):
        b = _make_bridge()
        result = b._run_worker({"task_type": "range"})
    assert result["status"] == "ok"
    assert "000001.SZ" in result["signals"]


def test_timeout_kills_worker():
    """worker 永不退出(stdout 空 + wait 总超时) → deadline 到 kill + 返回 error"""
    proc = FakeProc(stdout_lines=[], never_exit=True)
    with patch("app.tqsdk.bridge.subprocess.Popen", return_value=proc):
        b = _make_bridge()
        with patch("app.tqsdk.bridge.TIMEOUT", 1):  # 缩短超时到 1s
            result = b._run_worker({"task_type": "range"}, timeout_multiplier=1)
    assert result["status"] == "error"
    assert proc._killed is True  # 确认被 kill(不无限阻塞)


def test_stop_event_kills_worker():
    """stop_event 被 set → kill worker + 返回 stopped"""
    proc = FakeProc(stdout_lines=[], never_exit=True)
    stop_event = threading.Event()
    with patch("app.tqsdk.bridge.subprocess.Popen", return_value=proc):
        b = _make_bridge()

        def _set_stop():
            time.sleep(0.2)
            stop_event.set()

        threading.Thread(target=_set_stop, daemon=True).start()
        with patch("app.tqsdk.bridge.TIMEOUT", 600):
            result = b._run_worker({"task_type": "range"}, stop_event=stop_event)
    assert result["status"] == "stopped"
    assert proc._killed is True


def test_diag_no_keyerror():
    """worker_signals diag 用新键名(batch/stock_count/first_stock) → 不 KeyError, 正常 log"""
    lines = [
        json.dumps({"diag": "worker_signals", "batch": 5,
                    "stock_count": 50, "first_stock": "000001.SZ"}) + "\n",
        json.dumps({"status": "ok", "signals": {}, "total": 0}) + "\n",
    ]
    proc = FakeProc(stdout_lines=lines)
    with patch("app.tqsdk.bridge.subprocess.Popen", return_value=proc):
        b = _make_bridge()
        result = b._run_worker({"task_type": "range"})  # 不应抛 KeyError
    assert result["status"] == "ok"


def test_no_output_returns_error():
    """worker 无输出(空 stdout) + 进程正常退出 → 返回 error(无 status==ok)"""
    proc = FakeProc(stdout_lines=[], stderr_text="some stderr")
    with patch("app.tqsdk.bridge.subprocess.Popen", return_value=proc):
        b = _make_bridge()
        result = b._run_worker({"task_type": "range"})
    assert result["status"] == "error"
    assert "some stderr" in result["message"]
