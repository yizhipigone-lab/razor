"""NotificationStore.record() 回归测试

覆盖 2026-07-15 线上 bug:
'_duckdb.DuckDBPyConnection' object has no attribute 'last_id'

DuckDB 取自增 id 必须用 RETURNING 子句 + fetchone(),
不能用 result.last_id (那是 SQLite/MySQL API)。
"""
import os
import tempfile
import pytest

from app.live_trader.notifications import NotificationStore


@pytest.fixture
def store():
    # DuckDB 拒绝打开 0 字节空文件,先 unlink 让 DuckDB 自己创建
    fd, path = tempfile.mkstemp(suffix=".duckdb")
    os.close(fd)
    os.unlink(path)
    s = NotificationStore(path)
    yield s
    s.close()
    try:
        os.unlink(path)
    except OSError:
        pass


def test_record_returns_positive_id(store):
    """正常写入应返回正整数 id,不抛异常,不返回 -1。"""
    rid = store.record("INFO", "启动", "测试内容", source="test")
    assert rid > 0, f"record() 应返回正整数 id, 实得 {rid}"


def test_record_id_increments(store):
    """连续写入 id 单调递增。"""
    id1 = store.record("INFO", "t1", source="test")
    id2 = store.record("INFO", "t2", source="test")
    id3 = store.record("WARN", "t3", source="test")
    assert 0 < id1 < id2 < id3, f"id 应递增, 实得 {id1},{id2},{id3}"


def test_record_truncates_long_content(store):
    """content > 500 字符应被截断到 500,不抛异常。"""
    long_content = "x" * 1000
    rid = store.record("INFO", "t", long_content)
    assert rid > 0
    rows = store.recent(limit=1)
    assert len(rows[0]["content"]) == 500


def test_recent_after_record_returns_inserted(store):
    """写入后能通过 recent() 查到对应记录。"""
    rid = store.record("CRITICAL", "出错", "详情", source="manual")
    rows = store.recent(limit=10)
    assert any(r["id"] == rid and r["level"] == "CRITICAL" for r in rows)


def test_record_no_exception_on_unicode(store):
    """中文内容不应抛异常。"""
    rid = store.record("INFO", "中文标题", "中文内容", source="test")
    assert rid > 0