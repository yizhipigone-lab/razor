# server/live/broadcaster.py
"""实盘交易快照广播器

每 10s(仅交易日 09:25~15:05)从 live_trader(8001) 拉持仓/资产/委托/成交,
组装 {type:'live_trader_snapshot'} 经主服务 /ws 全量广播给前端实盘页。

与 MarketBroadcaster(行情,500ms) 关注点不同,独立循环 + 独立失败隔离。
行情现价走 market_quotes 通道(前端 applyLiveQuotes 实时刷新),本快照只补
结构数据:现金/冻结/委托/成交/持仓加减仓(这些 market_quotes 给不了)。

设计取舍(已知,审计确认可接受):
- 全量广播给所有 /ws 客户端(含不看实盘的):与 MarketBroadcaster 一致,payload 小;
  无客户端时 _broadcast_once 开头早退省请求。
- 部分接口失败仍推可用字段:前端 _renderLive* 对缺失字段有守卫,不崩。
"""
import asyncio
import contextlib
from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp

from core.logger import get_logger
from core.settings import settings
from server.websocket.manager import manager

log = get_logger("LiveTraderBroadcaster")

# 常量(避免魔法值;交易时段口径与 app/live_trader/scheduler.py:117 一致)
_TRADING_START = "09:25"
_TRADING_END = "15:05"
_BROADCAST_INTERVAL = 10.0
_FETCH_TIMEOUT = 2.0


def _base_url() -> str:
    """live_trader 服务 base url。默认值与 server/market/quotes.py 探测结果一致(均指向 live_trader:8001)。"""
    return settings.get("gateway", "live_trader_url") or "http://localhost:8001/live"


def _in_trading_hours() -> bool:
    """交易日(周一~周五)的 09:25~15:05。周末不推(与 scheduler.py:107 weekday>=5 一致)。"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周六=5 周日=6
        return False
    return _TRADING_START <= now.strftime("%H:%M") <= _TRADING_END


class LiveTraderBroadcaster:
    """实盘快照广播器(模块级单例 live_broadcaster)。"""

    def __init__(self) -> None:
        self.broadcast_interval = _BROADCAST_INTERVAL
        self.update_task: Optional[asyncio.Task] = None
        self.last_broadcast_time = 0.0
        self._fail_count = 0  # 不健康计数(全空/部分失败/异常),全成功清零
        self.total_broadcasts = 0  # 累计成功广播次数(可观测性 metric)
        self.total_fails = 0  # 累计失败次数

    def get_metrics(self) -> dict:
        """暴露 metric 供 /api/system/metrics 端点读取 (2026-07-15 第三轮迭代新增)"""
        return {
            "live_broadcaster_fails": self._fail_count,
            "live_broadcaster_total_broadcasts": self.total_broadcasts,
            "live_broadcaster_total_fails": self.total_fails,
            "live_broadcaster_running": self.update_task is not None and not self.update_task.done(),
            "live_broadcaster_interval_s": self.broadcast_interval,
        }

    async def start_broadcast_loop(self) -> None:
        if self.update_task:
            return
        self.update_task = asyncio.create_task(self._broadcast_loop())
        log.info("实盘快照广播循环启动(10s,交易日 09:25~15:05)")

    async def stop_broadcast_loop(self) -> None:
        if self.update_task:
            self.update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.update_task
            self.update_task = None
            log.info("实盘快照广播循环停止")

    async def _broadcast_loop(self) -> None:
        while True:
            try:
                now = asyncio.get_running_loop().time()
                if now - self.last_broadcast_time >= self.broadcast_interval:
                    await self._broadcast_once()
                    self.last_broadcast_time = now
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"实盘快照广播循环异常: {e}")
                await asyncio.sleep(2)

    async def _broadcast_once(self) -> None:
        # 非交易时段 / 无 ws 客户端 不推(省请求)
        if not _in_trading_hours():
            return
        if not getattr(manager, "active", None):
            return
        base = _base_url()
        try:
            async with aiohttp.ClientSession() as session:
                positions, asset, orders, deals = await asyncio.gather(
                    self._fetch_json(session, f"{base}/positions"),
                    self._fetch_json(session, f"{base}/asset"),
                    self._fetch_json(session, f"{base}/orders?limit=50"),
                    self._fetch_json(session, f"{base}/deals?limit=50"),
                    return_exceptions=True,
                )
            # 各接口独立容错:类型不对视为失败,记录缺失字段(集成M1:防静默)
            payload: Dict[str, Any] = {}
            missing = []
            if isinstance(positions, list):
                payload["positions"] = positions
            else:
                missing.append("positions")
            if isinstance(asset, dict):
                payload["asset"] = asset
            else:
                missing.append("asset")
            if isinstance(orders, list):
                payload["orders"] = orders
            else:
                missing.append("orders")
            if isinstance(deals, list):
                payload["deals"] = deals
            else:
                missing.append("deals")
            # 全空:可能 live_trader 未启动
            if not payload:
                self._fail_count += 1
                if self._fail_count == 1 or self._fail_count % 10 == 0:
                    log.warning(f"实盘快照全部接口空(fail={self._fail_count}),可能 live_trader(8001) 未启动")
                return
            # 部分失败:仍推可用字段,但周期告警(防 QMT 断连等场景被静默)
            if missing:
                self._fail_count += 1
                if self._fail_count == 1 or self._fail_count % 10 == 0:
                    log.warning(f"实盘快照部分接口失败(fail={self._fail_count}): 缺 {missing}")
            else:
                if self._fail_count > 0:
                    log.info(f"实盘快照恢复(此前 fail={self._fail_count})")
                self._fail_count = 0
            await manager.broadcast({"type": "live_trader_snapshot", "data": payload})
            self.total_broadcasts += 1
        except Exception as e:
            self._fail_count += 1
            self.total_fails += 1
            if self._fail_count == 1 or self._fail_count % 10 == 0:
                log.warning(f"实盘快照广播失败(fail={self._fail_count}): {e}")

    async def _fetch_json(self, session: aiohttp.ClientSession, url: str) -> Any:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT)) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            log.debug(f"实盘快照取数失败 {url}: {e}")
        return None


# 模块级单例(仿 server/websocket/handler.py:7 market_broadcaster)
live_broadcaster = LiveTraderBroadcaster()
