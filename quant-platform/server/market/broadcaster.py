# server/market/broadcaster.py
import asyncio
from typing import List, Dict, Any
from server.websocket.manager import manager
from server.market.quotes import MarketQuotes
from server.subscriptions.manager import SubscriptionManager
from core.logger import get_logger

log = get_logger("MarketBroadcaster")

class MarketBroadcaster:
    def __init__(self):
        self.subscription_manager = SubscriptionManager()
        self.market_quotes = MarketQuotes()
        self.update_task = None
        self.last_broadcast_time = 0
        self.broadcast_interval = 0.5  # 500ms 广播间隔(单位:秒;asyncio loop.time() 返回秒,勿用 500)
        self._empty_broadcast_count = 0  # 连续空数据计数

    async def start_broadcast_loop(self):
        if self.update_task:
            return

        self.update_task = asyncio.create_task(self._broadcast_loop())

    async def stop_broadcast_loop(self):
        if self.update_task:
            self.update_task.cancel()
            self.update_task = None

    async def _broadcast_loop(self):
        while True:
            try:
                now = asyncio.get_event_loop().time()
                if now - self.last_broadcast_time >= self.broadcast_interval:
                    await self._broadcast_once()
                    self.last_broadcast_time = now
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"广播任务错误: {e}")
                await asyncio.sleep(2)

    async def _broadcast_once(self):
        codes = self.subscription_manager.get_all_codes()
        if not codes:
            return

        quotes = await self.market_quotes.get_realtime_quotes(list(codes))
        if quotes:
            self._empty_broadcast_count = 0
            await manager.broadcast({
                'type': 'market_quotes',
                'data': quotes
            })
        else:
            self._empty_broadcast_count += 1
            log.warning(f"广播失败：无行情数据（订阅数: {len(codes)}，连续空: {self._empty_broadcast_count}）")
            # 连续 3 次空数据则向前端推送告警
            if self._empty_broadcast_count == 3:
                await manager.broadcast({
                    'type': 'connection_status',
                    'data': {
                        'status': 'disconnected',
                        'message': '行情源连续失败，请检查 QMT 代理或 live_trader 是否启动'
                    }
                })
            # 连续 10 次空数据则每 10 次告警一次（避免刷屏）
            elif self._empty_broadcast_count % 10 == 0:
                await manager.broadcast({
                    'type': 'connection_status',
                    'data': {
                        'status': 'disconnected',
                        'message': f'行情源已断开 {self._empty_broadcast_count} 轮，请检查服务'
                    }
                })

    async def subscribe_client(self, ws, codes):
        # add_client / remove_client 是同步函数，不能 await
        self.subscription_manager.add_client(ws, codes)

        if self.subscription_manager.get_connected_clients() == 1:
            await self.start_broadcast_loop()

    async def unsubscribe_client(self, ws, codes):
        self.subscription_manager.remove_client(ws, codes)

        if self.subscription_manager.get_connected_clients() == 0:
            await self.stop_broadcast_loop()