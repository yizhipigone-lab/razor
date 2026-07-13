# server/websocket/manager.py
from typing import List, Dict, Any
import json
import asyncio
from datetime import datetime, date
from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.active.append(ws)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            if ws in self.active:
                self.active.remove(ws)

    async def broadcast(self, data: Dict[str, Any]):
        safe_data = self._json_safe(data)
        dead = []

        async with self._lock:
            for ws in self.active:
                try:
                    await ws.send_json(safe_data)
                except Exception:
                    # 发送失败：WS已不可用，加入 dead 列表稍后清理
                    dead.append(ws)

        # 清理发送失败的 WS（关闭连接 + 从 active 移除）
        if dead:
            async with self._lock:
                for ws in dead:
                    if ws in self.active:
                        self.active.remove(ws)
                for ws in dead:
                    try:
                        await ws.close()
                    except Exception:
                        pass

    def _json_safe(self, obj):
        """递归转换不可序列化对象（NaN → None 确保 JSON 合法）"""
        import math
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if hasattr(obj, 'tolist'):
            return obj.tolist()
        if hasattr(obj, 'item'):
            try:
                val = obj.item()
                if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                    return None
                return val
            except:
                pass
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        if isinstance(obj, dict):
            return {k: self._json_safe(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._json_safe(x) for x in obj]
        return obj

manager = ConnectionManager()

class WebsocketGlobal:
    loop = None

    @classmethod
    def sync_broadcast(cls, data: dict):
        loop = cls.loop or asyncio.get_event_loop()
        if loop.is_closed():
            print("WebSocket 广播失败: 事件循环已关闭")
            return
        try:
            asyncio.run_coroutine_threadsafe(manager.broadcast(data), loop)
        except Exception as e:
            print(f"WebSocket 广播失败: {e}")

sync_broadcast = WebsocketGlobal.sync_broadcast