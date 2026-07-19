"""清仓锁(v5.4 §5.4 / §18.2)

照搬 MQ clearance-lock-service.ts + Redis 接口预留。
Key: (env, account, code) 三元组。
内存实现 + 双索引(主 Map + order_id→key 反查)。
TTL 300s 兜底,终态回调释放。
"""
import threading
import time
from typing import Dict, Optional, Set, Tuple

from core.logger import get_logger

from .config import LiveTraderConfig

logger = get_logger("live_trader.clearance_lock")


class ClearanceLock:
    """清仓锁(内存实现,预留 Redis 接口)"""

    def __init__(self, config: LiveTraderConfig):
        self.config = config
        self._locks: Dict[str, Dict] = {}  # key → {order_id, expire_at, acquired_at}
        self._order_index: Dict[int, str] = {}  # order_id → key(反查)
        self._lock = threading.Lock()
        self._env = "live"
        self._account = config.qmt_account_id

    def _key(self, code: str) -> str:
        return f"{self._env}::{self._account}::{code}"

    def acquire(self, code: str, order_id: Optional[int] = None,
                ttl: Optional[float] = None) -> bool:
        """加锁。返回 True=成功,False=已被占用"""
        key = self._key(code)
        ttl = ttl or self.config.clearance_lock_ttl_sec
        with self._lock:
            self._cleanup_expired()
            if key in self._locks:
                # 已锁
                return False
            self._locks[key] = {
                "order_id": order_id,
                "expire_at": time.time() + ttl,
                "acquired_at": time.time(),
            }
            if order_id:
                self._order_index[order_id] = key
            logger.debug(f"清仓锁 acquire: {key} ttl={ttl}")
            return True

    def acquire_with_wait(self, code: str, order_id: Optional[int] = None,
                          ttl: Optional[float] = None, timeout_sec: float = 30.0) -> bool:
        """加锁(带等待超时)。buy-signal 批量模式传 timeout_sec=5,手动传 30。

        在 timeout_sec 内循环重试获取锁,超过则返回 False。
        """
        deadline = time.time() + timeout_sec
        interval = 0.5  # 每 0.5s 重试一次
        while True:
            if self.acquire(code, order_id, ttl):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(interval)

    def release(self, code: str) -> bool:
        """按 code 释放"""
        key = self._key(code)
        with self._lock:
            entry = self._locks.pop(key, None)
            if entry and entry.get("order_id"):
                self._order_index.pop(entry["order_id"], None)
            return entry is not None

    def release_by_order_id(self, order_id: int) -> bool:
        """按 order_id 释放(幂等,回调用)"""
        with self._lock:
            key = self._order_index.pop(order_id, None)
            if key:
                self._locks.pop(key, None)
                logger.debug(f"清仓锁 release_by_order_id: oid={order_id}")
                return True
            return False

    def is_locked(self, code: str) -> bool:
        key = self._key(code)
        with self._lock:
            self._cleanup_expired()
            return key in self._locks

    def rebind_order_id(self, code: str, order_id: int) -> bool:
        """下单拿到 order_id 后补建反查索引(H1 修复,2026-07-19 审计)。

        acquire/acquire_with_wait 在下单前调用,此时 order_id 还没有(传 None),
        _order_index 没建。下单成功拿到 seq/order_id 后调本方法补索引;
        否则成交回调 release_by_order_id 找不到 → 锁持有到 TTL(300s) → 止损/止盈延迟。
        """
        if not order_id:
            return False
        key = self._key(code)
        with self._lock:
            entry = self._locks.get(key)
            if not entry:
                return False
            old_oid = entry.get("order_id")
            if old_oid:
                self._order_index.pop(old_oid, None)
            entry["order_id"] = order_id
            self._order_index[order_id] = key
            return True

    def _cleanup_expired(self) -> None:
        """清理过期锁"""
        now = time.time()
        expired = [k for k, v in self._locks.items() if v["expire_at"] <= now]
        for k in expired:
            entry = self._locks.pop(k)
            if entry.get("order_id"):
                self._order_index.pop(entry["order_id"], None)
            logger.warning(f"清仓锁 TTL 过期释放: {k}")
