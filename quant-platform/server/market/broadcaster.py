# server/market/broadcaster.py
import asyncio
from typing import List, Dict, Any
from server.websocket.manager import manager
from server.market.quotes import MarketQuotes
from server.subscriptions.manager import SubscriptionManager

class MarketBroadcaster:
    def __init__(self):
        self.subscription_manager = SubscriptionManager()
        self.market_quotes = MarketQuotes()
        self.update_task = None
        self.last_broadcast_time = 0
        self.broadcast_interval = 500  # 500ms 广播间隔

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
            await manager.broadcast({
                'type': 'market_quotes',
                'data': quotes
            })

    async def subscribe_client(self, ws, codes):
        await self.subscription_manager.add_client(ws, codes)

        if self.subscription_manager.get_connected_clients() == 1:
            await self.start_broadcast_loop()

    async def unsubscribe_client(self, ws, codes):
        await self.subscription_manager.remove_client(ws, codes)

        if self.subscription_manager.get_connected_clients() == 0:
            await self.stop_broadcast_loop()