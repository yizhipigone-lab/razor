import os
import redis
import json
import time
import threading
from datetime import datetime
from core.logger import get_logger

log = get_logger("RedisManager")

# ─── 内存缓存降级层 ─────────────────────────────────────────────
# Redis 不可用时，用进程内 dict 做 TTL 缓存，保证热度计算等功能不报错。

class _MemoryCacheEntry:
    __slots__ = ("value", "expire_at")

    def __init__(self, value: str, ttl_seconds: int):
        self.value = value
        self.expire_at = time.monotonic() + ttl_seconds

    def is_alive(self) -> bool:
        return time.monotonic() < self.expire_at


class MemoryCacheFallback:
    """Redis 不可用时的进程内 TTL 缓存（线程安全）"""

    def __init__(self):
        self._store: dict[str, _MemoryCacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            entry = self._store.get(key)
            if entry and entry.is_alive():
                return entry.value
            # 过期清理
            if entry:
                del self._store[key]
            return None

    def setex(self, key: str, ttl_seconds: int, value: str):
        with self._lock:
            self._store[key] = _MemoryCacheEntry(str(value), ttl_seconds)

    def delete(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def keys(self, pattern: str = "*") -> list[str]:
        """简易 pattern 匹配（仅支持 * 通配符，与 Redis KEYS 最常用模式对齐）

        支持: *, prefix*, *suffix, prefix*suffix
        不支持: ?、[]、[a-z] 等 fnmatch 扩展语法（避免与 Redis KEYS 行为不一致）
        """
        with self._lock:
            # 先清理过期
            expired = [k for k, v in self._store.items() if not v.is_alive()]
            for k in expired:
                del self._store[k]

            # 仅支持 * 通配符的简单匹配，与 Redis KEYS 行为对齐
            if pattern == "*":
                return list(self._store.keys())

            # 将 pattern 拆为前缀和后缀
            parts = pattern.split("*")
            if len(parts) == 2:
                prefix, suffix = parts
                return [k for k in self._store
                        if k.startswith(prefix) and k.endswith(suffix)]
            elif len(parts) == 1:
                # 无通配符，精确匹配
                return [k for k in self._store if k == pattern]
            else:
                # 多段 * 通配，回退到全量过滤（性能差但正确）
                import fnmatch
                return [k for k in self._store if fnmatch.fnmatch(k, pattern)]

    def hset(self, name: str, mapping: dict, ttl_seconds: int = 86400):
        """模拟 Redis HSET：以 hash_key 格式存储

        注意：Redis 原生 HSET 无 TTL，但内存缓存需要 TTL 防内存泄漏。
        默认 86400s(24h)，调用方可通过 ttl_seconds 覆盖。
        """
        with self._lock:
            for field, val in mapping.items():
                self._store[f"{name}::{field}"] = _MemoryCacheEntry(str(val), ttl_seconds)

    def hgetall(self, name: str) -> dict:
        """模拟 Redis HGETALL"""
        with self._lock:
            prefix = f"{name}::"
            result = {}
            expired = []
            for k, v in self._store.items():
                if k.startswith(prefix):
                    if v.is_alive():
                        field = k[len(prefix):]
                        result[field] = v.value
                    else:
                        expired.append(k)
            for k in expired:
                del self._store[k]
            return result

    def cleanup_expired(self):
        """主动清理过期条目"""
        with self._lock:
            expired = [k for k, v in self._store.items() if not v.is_alive()]
            for k in expired:
                del self._store[k]


# 全局内存缓存单例
_memory_cache = MemoryCacheFallback()

class RedisManager:
    """
    Redis 管理器单例：处理连接池、Key-Value 缓存以及信号发布订阅。
    配置优先从环境变量获取，本地开发回落到 localhost。
    Redis 不可用时自动降级，所有写操作 fallback 到进程内内存缓存。
    """
    _instance = None
    _client = None
    _available = False  # 标记 Redis 是否真正可用
    _connect_attempted = False  # 标记是否已尝试过连接（避免反复重试）
    _last_connect_fail = 0.0  # 上次连接失败的时间戳
    _RETRY_COOLDOWN = 60  # 连接失败后冷却 60 秒再重试

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_client(self) -> redis.Redis | None:
        """获取 Redis 客户端单例（带连接池）。
        Redis 不可用时返回 None，调用方应检查返回值。
        连接失败后有冷却期，不会每次调用都重试。
        """
        if self._available and self._client:
            return self._client

        if not self._connect_attempted:
            # 尚未尝试过连接，试一次
            self._try_connect()
        else:
            # 已尝试过，检查是否过了冷却期
            elapsed = time.monotonic() - self._last_connect_fail
            if elapsed > self._RETRY_COOLDOWN:
                self._try_connect()

        return self._client if self._available else None

    def _try_connect(self):
        """尝试连接 Redis，成功则标记 _available=True，失败则确保 _client=None"""
        host = os.getenv("REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("REDIS_PORT", 6379))
        try:
            # 先用 socket 探测端口，快速失败（避免 redis-py 内部重试拖慢）
            import socket
            sock = socket.create_connection((host, port), timeout=2)
            sock.close()

            client = redis.Redis(
                host=host,
                port=port,
                db=0,
                decode_responses=True,
                socket_connect_timeout=2,
                retry_on_timeout=False
            )
            client.ping()
            self._client = client
            self._available = True
            self._connect_attempted = True
            log.info(f"Redis 连接成功 | {host}:{port}")
        except Exception as e:
            log.warning(f"Redis 连接失败 ({host}:{port}): {e}，将使用内存缓存降级")
            self._client = None
            self._available = False
            self._connect_attempted = True
            self._last_connect_fail = time.monotonic()

    def is_available(self) -> bool:
        """检查 Redis 是否可用"""
        if not self._available:
            return False
        # 健康检查：确认连接还活着
        try:
            if self._client:
                self._client.ping()
                return True
        except Exception:
            self._available = False
            self._client = None
            log.warning("Redis 连接已断开，切换到内存缓存降级")
        return False

    def publish_signal(self, channel: str, data: dict):
        """发布信号到指定频道（Redis 不可用时记录错误日志，pub/sub 本身是尽力而为）"""
        client = self.get_client()
        if client:
            try:
                msg = json.dumps(data)
                client.publish(channel, msg)
                log.debug(f"Redis Publish | Channel: {channel} | Msg: {msg[:100]}...")
            except Exception as e:
                log.error(f"Redis 发布信号失败: {e}")

    def hset_all(self, name: str, mapping: dict):
        """批量设置哈希表内容（Redis 不可用时写入内存缓存）"""
        client = self.get_client()
        if client:
            try:
                client.hset(name, mapping=mapping)
                return
            except Exception as e:
                log.debug(f"Redis HSET 失败，降级内存缓存: {e}")
        # 降级到内存缓存
        _memory_cache.hset(name, mapping)

    def hget_all(self, name: str) -> dict:
        """获取哈希表全部内容（Redis 不可用时从内存缓存读）"""
        client = self.get_client()
        if client:
            try:
                return client.hgetall(name)
            except Exception as e:
                log.debug(f"Redis HGETALL 失败，降级内存缓存: {e}")
        return _memory_cache.hgetall(name)

    def cache_setex(self, key: str, ttl_seconds: int, value):
        """统一缓存写入：Redis 优先，不可用时 fallback 到内存缓存"""
        client = self.get_client()
        if client:
            try:
                client.setex(key, ttl_seconds, value)
                return
            except Exception as e:
                log.debug(f"Redis SETEX 失败，降级内存缓存: {e}")
        _memory_cache.setex(key, ttl_seconds, str(value))

    def cache_get(self, key: str) -> str | None:
        """统一缓存读取：Redis 优先，不可用时 fallback 到内存缓存"""
        client = self.get_client()
        if client:
            try:
                return client.get(key)
            except Exception as e:
                log.debug(f"Redis GET 失败，降级内存缓存: {e}")
        return _memory_cache.get(key)

    def cache_keys(self, pattern: str = "*") -> list[str]:
        """统一缓存键扫描：Redis 优先，不可用时 fallback 到内存缓存"""
        client = self.get_client()
        if client:
            try:
                return client.keys(pattern)
            except Exception as e:
                log.debug(f"Redis KEYS 失败，降级内存缓存: {e}")
        return _memory_cache.keys(pattern)

    def cache_delete(self, *keys: str):
        """统一缓存删除：Redis 优先，不可用时 fallback 到内存缓存"""
        client = self.get_client()
        if client:
            try:
                client.delete(*keys)
                return
            except Exception as e:
                log.debug(f"Redis DELETE 失败，降级内存缓存: {e}")
        for k in keys:
            _memory_cache.delete(k)


# 全局单例
redis_manager = RedisManager()
# 延迟连接：不在模块加载时主动连接 Redis，等第一次 get_client() 时再连
# 兼容旧代码 from core.redis_manager import redis_client 后直接使用：
# redis_client 现在是 property 代理，自动获取实际连接
class _RedisClientProxy:
    """延迟代理：兼容旧代码 `from core.redis_manager import redis_client` 后直接使用。
    每次属性访问时动态获取真实 Redis 客户端，避免 eager init。
    如果 Redis 不可用，属性访问会抛 AttributeError（与 None 行为一致）。
    """
    def __getattr__(self, name):
        client = redis_manager.get_client()
        if client is None:
            raise AttributeError(
                f"Redis 不可用，无法访问 redis_client.{name}。"
                "请使用 redis_manager.cache_get/cache_set 等降级方法。"
            )
        return getattr(client, name)

    def __bool__(self):
        return redis_manager.get_client() is not None

redis_client = _RedisClientProxy()
