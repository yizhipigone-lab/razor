import os
import redis
import json
from datetime import datetime
from core.logger import get_logger

log = get_logger("RedisManager")

class RedisManager:
    """
    Redis 管理器单例：处理连接池、Key-Value 缓存以及信号发布订阅。
    配置优先从环境变量获取（Docker 模式），本地开发回落到 localhost。
    """
    _instance = None
    _client = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_client(self) -> redis.Redis:
        """获取 Redis 客户端单例（带连接池）"""
        if self._client is None:
            host = os.getenv("REDIS_HOST", "localhost")
            port = int(os.getenv("REDIS_PORT", 6379))
            try:
                self._client = redis.Redis(
                    host=host,
                    port=port,
                    db=0,
                    decode_responses=True, # 自动解码为字符串
                    socket_connect_timeout=5,
                    retry_on_timeout=True
                )
                # 测试连通性
                self._client.ping()
                log.info(f"Redis 连接成功 | {host}:{port}")
            except Exception as e:
                log.error(f"Redis 连接失败 ({host}:{port}): {e}")
                # 此处不抛出异常，允许系统在 Redis 缺失时以降级模式启动（按具体业务逻辑而定）
        return self._client

    def publish_signal(self, channel: str, data: dict):
        """发布信号到指定频道"""
        client = self.get_client()
        if client:
            try:
                msg = json.dumps(data)
                client.publish(channel, msg)
                log.debug(f"Redis Publish | Channel: {channel} | Msg: {msg[:100]}...")
            except Exception as e:
                log.error(f"Redis 发布信号失败: {e}")

    def hset_all(self, name: str, mapping: dict):
        """批量设置哈希表内容"""
        client = self.get_client()
        if client:
            try:
                client.hset(name, mapping=mapping)
            except Exception as e:
                log.error(f"Redis HSET 失败: {e}")

    def hget_all(self, name: str) -> dict:
        """获取哈希表全部内容"""
        client = self.get_client()
        if client:
            try:
                return client.hgetall(name)
            except Exception as e:
                log.error(f"Redis HGETALL 失败: {e}")
                return {}
        return {}

# 全局单例
redis_manager = RedisManager()
# 快捷导出客户端
redis_client = redis_manager.get_client()
