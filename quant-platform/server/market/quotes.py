# server/market/quotes.py
import asyncio
import aiohttp
from typing import Dict, List, Any
from core.settings import settings
from core.logger import get_logger

log = get_logger("MarketQuotes")

class MarketQuotes:
    def __init__(self):
        self.qmt_proxy_url = settings.get('qmt_proxy_url', 'http://localhost:8081/api')
        self.default_timeout = 3

    async def get_realtime_quotes(self, codes: List[str]) -> Dict[str, Any]:
        if not codes:
            return {}

        if settings.get('gateway', 'active_gateway') == 'qmt':
            result = await self.get_qmt_quotes(codes)
            if result:
                return result
            log.warning("QMT 行情失败，降级到 TDX 回退")

        return await self.get_fallback_quotes(codes)

    async def get_qmt_quotes(self, codes: List[str]) -> Dict[str, Any]:
        try:
            codes_str = ",".join(codes)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.qmt_proxy_url}/quotes?codes={codes_str}",
                    timeout=self.default_timeout
                ) as response:
                    if response.status == 200:
                        return await response.json()
        except Exception as e:
            log.debug(f"QMT 行情获取失败: {e}")
        return {}

    async def get_fallback_quotes(self, codes: List[str]) -> Dict[str, Any]:
        """TDX/腾讯 HTTP 兜底行情源"""
        try:
            from app.data_manager.engine import get_realtime_quote

            df = await asyncio.to_thread(get_realtime_quote, codes)
            if df is None or df.empty:
                return {}

            result = {}
            for _, row in df.iterrows():
                code = str(row.get('code', ''))
                if not code:
                    continue
                result[code] = {
                    'price': float(row.get('price', 0)),
                    'lastPrice': float(row.get('price', 0)),
                    'lastClose': float(row.get('last_close', 0)),
                    'preClose': float(row.get('last_close', 0)),
                    'change_pct': float(row.get('change_pct', 0)),
                    'priceChangeRatio': float(row.get('change_pct', 0)),
                    'open': float(row.get('open', 0)),
                    'high': float(row.get('high', 0)),
                    'low': float(row.get('low', 0)),
                    'volume': float(row.get('volume', 0)),
                    'amount': float(row.get('amount', 0)),
                }
            return result
        except Exception as e:
            log.error(f"TDX 回退行情失败: {e}")
            return {}