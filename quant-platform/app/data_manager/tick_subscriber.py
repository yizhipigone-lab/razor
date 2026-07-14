"""QMT Tick 订阅源 → event_engine.emit(EVENT_TICK) (2026-07-15)

专项计划书 PLAN-tick-subscription-2026-07-15 Step 1。
sim + live 共用: xtdata 真订阅 push → event_engine 总线 → sim intraday_monitor / live exit_monitor。

线程模型(v2 CRITICAL-1 修复):
  xtdata 回调线程 → queue.put(ticks)        # 微秒级, 永不阻塞 xtdata
  tick worker 线程 → drain queue → emit(EVENT_TICK)
handler 跑在 worker 线程, 不在 xtdata 线程。

POC 实测(2026-07-15):
  - subscribe_whole_quote(codes, callback) 可用, 回调收 {code: tick_dict}
  - tick_dict: lastPrice/open/high/low/lastClose/askPrice/bidPrice/...
  - unsubscribe_quote(seq: int), seq 由 subscribe 返回
"""
import queue
import threading
import time
from typing import Dict, List, Optional

from core.logger import get_logger
from core.event_engine import event_engine, EVENT_TICK

log = get_logger("TickSubscriber")

# 健康判定: N 秒内有 tick 才健康(下游据此决定是否走轮询 fallback)
HEALTH_TIMEOUT_SEC = 15.0
# 队列背压上限(满则丢最旧, 告警)
QUEUE_MAXSIZE = 10000


class TickSubscriber:
    """QMT xtdata tick 订阅源。

    回调运行在 xtdata 线程, 只做 queue.put(微秒级), 绝不做重活。
    重活(检查/下单)由下游 handler 各自 dispatch。
    """

    def __init__(self, qmt_wrapper):
        self._qmt = qmt_wrapper
        self._lock = threading.Lock()
        # code -> subscribe seq(供 unsubscribe)
        self._code_to_seq: Dict[str, int] = {}
        # seq -> codes(反向, 便于按 seq 取消)
        self._seq_to_codes: Dict[int, List[str]] = {}
        self._queue: "queue.Queue" = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None
        self._last_tick_ts: float = 0.0
        self._dropped = 0  # 背压丢弃计数

    # ── 生命周期 ────────────────────────────────

    def start(self):
        """启动 tick worker 线程。"""
        if self._running:
            return
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._drain_loop, daemon=True, name="TickWorker"
        )
        self._worker_thread.start()
        log.info("TickSubscriber worker 已启动")

    def stop(self):
        """停 worker + unsubscribe 全部。"""
        self._running = False
        # 唤醒 worker 退出
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        with self._lock:
            seqs = list(self._seq_to_codes.keys())
            self._code_to_seq.clear()
            self._seq_to_codes.clear()
        for seq in seqs:
            self._qmt.unsubscribe_quote(seq)
        if self._worker_thread:
            self._worker_thread.join(timeout=2.0)
        log.info("TickSubscriber 已停止")

    # ── 订阅管理 ────────────────────────────────

    def subscribe(self, codes: List[str]) -> int:
        """订阅 codes(幂等: 已订阅的 code 跳过)。返回 seq(0=失败)。"""
        if not codes:
            return 0
        with self._lock:
            new_codes = [c for c in codes if c not in self._code_to_seq]
        if not new_codes:
            return 0
        seq = self._qmt.subscribe_quote(new_codes, callback=self._on_xtdata_tick)
        if not seq:
            log.warning(f"subscribe 失败: {new_codes}")
            return 0
        with self._lock:
            for c in new_codes:
                self._code_to_seq[c] = seq
            self._seq_to_codes.setdefault(seq, []).extend(new_codes)
        return seq

    def unsubscribe(self, codes: List[str]) -> None:
        """取消订阅 codes(幂等)。按 seq 取消(seq 下无 code 后清 seq)。"""
        if not codes:
            return
        with self._lock:
            seqs_to_check = set()
            for c in codes:
                seq = self._code_to_seq.pop(c, None)
                if seq is None:
                    continue
                lst = self._seq_to_codes.get(seq, [])
                if c in lst:
                    lst.remove(c)
                seqs_to_check.add(seq)
            empty_seqs = [s for s in seqs_to_check if not self._seq_to_codes.get(s)]
            for s in empty_seqs:
                self._seq_to_codes.pop(s, None)
        for s in empty_seqs:
            self._qmt.unsubscribe_quote(s)

    @property
    def healthy(self) -> bool:
        """N 秒内有 tick 才健康; 不健康时下游应走轮询 fallback。"""
        if self._last_tick_ts == 0.0:
            return False
        return (time.time() - self._last_tick_ts) < HEALTH_TIMEOUT_SEC

    @property
    def dropped_count(self) -> int:
        return self._dropped

    # ── xtdata 回调(xtdata 线程) ────────────────

    def _on_xtdata_tick(self, ticks):
        """xtdata 回调(其线程): 只 put 队列, 绝不做重活, 永不抛异常(防崩 xtdata)。"""
        try:
            self._queue.put_nowait(ticks)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                log.warning(f"[TickSubscriber] 队列满, 丢 tick(累计 {self._dropped}, 背压)")

    # ── worker 线程 ─────────────────────────────

    def _drain_loop(self):
        """tick worker: drain queue → emit(EVENT_TICK, {code,price,high,low,preClose})。"""
        while self._running:
            try:
                ticks = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if ticks is None:
                break  # stop() 哨兵
            try:
                self._emit_ticks(ticks)
                self._last_tick_ts = time.time()
            except Exception as e:
                # worker 绝不因单批 tick 异常退出
                log.error(f"[TickSubscriber] emit 异常(继续): {e}")

    def _emit_ticks(self, ticks):
        """解析 xtdata 回调数据 → emit(EVENT_TICK) per code。

        ticks 期望 {code: tick_dict}。tick_dict 字段见模块 docstring。
        """
        if not isinstance(ticks, dict):
            return
        for code, t in ticks.items():
            if not isinstance(t, dict):
                continue
            price = float(t.get('lastPrice', 0) or 0)
            if price <= 0:
                continue  # 缺价 tick 不 emit(防 NaN 传入下游)
            event_engine.emit(EVENT_TICK, {
                'code': code,
                'price': price,
                'high': float(t.get('high', 0) or 0),
                'low': float(t.get('low', 0) or 0),
                'preClose': float(t.get('lastClose', 0) or 0),
                'volume': float(t.get('volume', 0) or 0),
                'ts': time.time(),
            })
