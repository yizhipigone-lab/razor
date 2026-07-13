# server/market/quotes.py
import asyncio
import aiohttp
from typing import Dict, List, Any
from core.settings import settings
from core.logger import get_logger

log = get_logger("MarketQuotes")

# 行情源：live_trader(8001) 提供行情 + 交易 + 数据同步，qmt_proxy(8081) 已废弃
_QUOTE_SOURCES = [
    {"url": "http://localhost:8001/live", "name": "live_trader"},
]

class MarketQuotes:
    def __init__(self):
        self.default_timeout = 1.5  # 降低超时，避免 live_trader 宕机时广播循环每周期卡3秒拖垮事件循环
        self._active_source = None  # 缓存可用的行情源
        self._failed_source = None  # 失败源缓存
        self._failed_until = 0.0     # 失败源退避到期时间（避免每500ms重试死掉的源）

    def _get_quote_url(self) -> str | None:
        """获取可用的行情源 URL（带缓存，避免每次请求都探测）"""
        # 优先用配置中的显式 URL
        config_url = settings.get('gateway', 'live_trader_url')
        if config_url:
            return config_url
        # 用缓存的可用源
        if self._active_source:
            return self._active_source
        # 首次启动时由 get_qmt_quotes 动态探测，探测到后缓存
        return None

    async def get_realtime_quotes(self, codes: List[str]) -> Dict[str, Any]:
        if not codes:
            return {}

        # 1. 优先 QMT/live_trader
        if settings.get('gateway', 'active_gateway') == 'qmt':
            result = await self.get_qmt_quotes(codes)
            if result:
                return result
            log.warning("QMT 行情失败，降级到 TDX 回退")

        # 2. TDX HTTP 回退
        result = await self.get_fallback_quotes(codes)
        if result:
            return result

        # 3. 不再走 DuckDB 兜底：AutoSync 后台线程会写 DuckDB，
        #    单连接并发访问会导致进程崩溃。返回空让前端显示 '--' 更安全。
        log.warning("所有实时行情源均失败，前端将显示空值")
        return {}

    async def get_qmt_quotes(self, codes: List[str]) -> Dict[str, Any]:
        """尝试从可用行情源获取报价（自动探测 live_trader / qmt_proxy）"""
        import time as _time
        codes_str = ",".join(codes)

        # 失败源退避：未到退避时间直接跳过，避免每500ms重试死掉的 live_trader
        if self._failed_source and _time.time() < self._failed_until:
            return {}

        # 1) 如果有配置显式 URL 或缓存源，直接用
        known_url = self._get_quote_url()
        if known_url:
            result = await self._try_fetch(known_url, codes_str)
            if result is not None:
                self._active_source = known_url
                return result
            # 已知源失败，清缓存重试全部
            self._active_source = None

        # 2) 依次探测所有行情源
        for src in _QUOTE_SOURCES:
            result = await self._try_fetch(src["url"], codes_str)
            if result is not None:
                self._active_source = src["url"]
                log.info(f"行情源自动锁定: {src['name']} ({src['url']})")
                return result

        # 所有源失败，标记退避30秒
        self._failed_source = "all"
        self._failed_until = _time.time() + 30
        log.warning("所有行情源均不可用 (live_trader:8001)，30秒内不再重试")
        return {}

    async def _try_fetch(self, base_url: str, codes_str: str) -> Dict[str, Any] | None:
        """尝试从指定 URL 获取行情，成功返回 dict，失败返回 None"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base_url}/quotes?codes={codes_str}",
                    timeout=aiohttp.ClientTimeout(total=self.default_timeout)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data:  # 非空数据才算成功
                            return data
        except Exception:
            pass
        return None

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
                price = float(row.get('price', 0))
                if not (price > 0):  # 委托 quote_source 后缺价行 price=NaN,跳过(保留旧行为)
                    continue
                result[code] = {
                    'price': price,
                    'lastPrice': price,
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

    async def get_last_close_fallback(self, codes: List[str]) -> Dict[str, Any]:
        """最后兜底：从 Parquet 读取昨日收盘价（避免前端全空）"""
        try:
            from database.duckdb_manager import db
            result = {}
            for code in codes:
                try:
                    df = await asyncio.to_thread(db.load_bars, code, freq='daily')
                    if df is not None and not df.empty:
                        last_row = df.iloc[-1]
                        result[code] = {
                            'price': float(last_row.get('close', 0)),
                            'lastPrice': float(last_row.get('close', 0)),
                            'lastClose': float(last_row.get('close', 0)),
                            'preClose': float(last_row.get('close', 0)),
                            'change_pct': 0.0,
                            'priceChangeRatio': 0.0,
                            'open': float(last_row.get('open', 0)),
                            'high': float(last_row.get('high', 0)),
                            'low': float(last_row.get('low', 0)),
                            'volume': float(last_row.get('volume', 0)),
                            'amount': float(last_row.get('amount', 0)),
                        }
                except Exception as e:
                    log.debug(f"读取 {code} 历史数据失败: {e}")
                    continue
            return result
        except Exception as e:
            log.error(f"兜底行情源失败: {e}")
            return {}