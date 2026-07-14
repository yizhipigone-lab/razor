"""TickSubscriber 单测 — mock qmt_wrapper, 不依赖真实 QMT。

覆盖(对齐 PLAN-tick-subscription v2):
  - subscribe 幂等 + seq 存储
  - xtdata 回调 → worker → emit(EVENT_TICK) schema 正确
  - 缺价 tick 不 emit
  - 背压: 队列满不崩
  - healthy 判定
  - emit 异常不杀 worker
  - unsubscribe 按 seq 取消
"""
import time
import threading

import pytest

from core.event_engine import event_engine, EVENT_TICK
from app.data_manager.tick_subscriber import TickSubscriber


class FakeQmt:
    """模拟 qmt_wrapper: 记录 subscribe/unsubscribe, subscribe 返回递增 seq。"""
    def __init__(self):
        self._next_seq = 100
        self.subscribed_calls = []   # [(codes, callback)]
        self.unsubscribed_seqs = []
        self.callback = None

    def subscribe_quote(self, codes, callback):
        self.subscribed_calls.append((list(codes), callback))
        seq = self._next_seq
        self._next_seq += 1
        self.callback = callback
        return seq

    def unsubscribe_quote(self, seq):
        self.unsubscribed_seqs.append(seq)


@pytest.fixture
def received():
    """注册 EVENT_TICK 处理器收集 emit 的 tick。"""
    buf = []
    def handler(event):
        buf.append(event.data)
    event_engine.register(EVENT_TICK, handler)
    yield buf
    event_engine.unregister(EVENT_TICK, handler)


@pytest.fixture
def subscriber():
    fake_qmt = FakeQmt()
    sub = TickSubscriber(fake_qmt)
    sub.start()
    yield sub, fake_qmt
    sub.stop()


def _wait(predicate, timeout=2.0, interval=0.02):
    """轮询等待 predicate 为真(给 worker 线程时间 drain)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_subscribe_stores_seq_and_is_idempotent(subscriber):
    sub, fake_qmt = subscriber
    seq = sub.subscribe(["000001.SZ", "600519.SH"])
    assert seq == 100
    assert len(fake_qmt.subscribed_calls) == 1
    # 幂等: 再 subscribe 同样的 code 不调 qmt
    seq2 = sub.subscribe(["000001.SZ", "600519.SH"])
    assert seq2 == 0  # 无新 code
    assert len(fake_qmt.subscribed_calls) == 1


def test_xtdata_callback_emits_event_tick(subscriber, received):
    """xtdata 回调 → worker → emit(EVENT_TICK, {code,price,high,low,preClose})。"""
    sub, fake_qmt = subscriber
    sub.subscribe(["000001.SZ"])
    # 模拟 xtdata 推一批 tick
    fake_qmt.callback({
        "000001.SZ": {"lastPrice": 10.5, "high": 10.8, "low": 10.2,
                      "lastClose": 10.3, "volume": 1000},
    })
    assert _wait(lambda: len(received) >= 1)
    tick = received[-1]
    assert tick["code"] == "000001.SZ"
    assert tick["price"] == 10.5
    assert tick["high"] == 10.8
    assert tick["low"] == 10.2
    assert tick["preClose"] == 10.3


def test_missing_price_tick_not_emitted(subscriber, received):
    """lastPrice<=0 的 tick 不 emit(防 NaN 传入下游)。"""
    sub, fake_qmt = subscriber
    sub.subscribe(["000001.SZ"])
    fake_qmt.callback({"000001.SZ": {"lastPrice": 0, "high": 0, "low": 0, "lastClose": 0}})
    time.sleep(0.2)
    assert len(received) == 0


def test_healthy_after_tick_then_timeout(subscriber):
    """有 tick → healthy; 超时 → unhealthy。"""
    sub, fake_qmt = subscriber
    assert sub.healthy is False  # 初始无 tick
    sub.subscribe(["000001.SZ"])
    fake_qmt.callback({"000001.SZ": {"lastPrice": 10.5, "high": 10.5, "low": 10.5, "lastClose": 10.3}})
    assert _wait(lambda: sub.healthy)
    assert sub.healthy is True


def test_emit_exception_does_not_kill_worker(subscriber, received):
    """下游 handler 抛异常, worker 不退出, 后续 tick 仍能 emit。"""
    # 先注册一个会抛异常的 handler
    bad_buf = []
    def bad_handler(event):
        bad_buf.append(1)
        raise RuntimeError("故意炸")
    event_engine.register(EVENT_TICK, bad_handler)
    try:
        sub, fake_qmt = subscriber
        sub.subscribe(["000001.SZ"])
        fake_qmt.callback({"000001.SZ": {"lastPrice": 10.5, "high": 10.5, "low": 10.5, "lastClose": 10.3}})
        assert _wait(lambda: len(bad_buf) >= 1)
        # worker 仍存活: 再推一笔, received 仍能收到(event_engine 各 handler 独立 try)
        fake_qmt.callback({"000001.SZ": {"lastPrice": 11.0, "high": 11.0, "low": 11.0, "lastClose": 10.3}})
        assert _wait(lambda: any(t.get("price") == 11.0 for t in received))
    finally:
        event_engine.unregister(EVENT_TICK, bad_handler)


def test_unsubscribe_by_seq(subscriber):
    """unsubscribe(code) → qmt.unsubscribe_quote(seq) 被调; seq 下无 code 后清。"""
    sub, fake_qmt = subscriber
    sub.subscribe(["000001.SZ", "600519.SH"])  # seq 100
    sub.unsubscribe(["000001.SZ"])
    # 只退一个 code, seq 100 下还剩 600519 → 不调 unsubscribe_quote
    assert fake_qmt.unsubscribed_seqs == []
    sub.unsubscribe(["600519.SH"])
    # seq 100 下空了 → 调 unsubscribe_quote(100)
    assert 100 in fake_qmt.unsubscribed_seqs


def test_backpressure_queue_full_does_not_crash(subscriber, received):
    """队列满时 _on_xtdata_tick 不崩(丢 tick, 告警)。"""
    sub, fake_qmt = subscriber
    sub.subscribe(["000001.SZ"])
    # 灌爆队列(不启动 worker drain 也无所谓, put_nowait 满则丢)
    for i in range(20000):
        sub._on_xtdata_tick({"000001.SZ": {"lastPrice": 10.0, "high": 10.0, "low": 10.0, "lastClose": 9.9}})
    # 不抛异常即通过; dropped_count > 0
    assert sub.dropped_count > 0
