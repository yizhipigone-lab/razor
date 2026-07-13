import requests
import time
import pandas as pd
from functools import lru_cache
from typing import Dict, Tuple
from loguru import logger as log
from core.settings import settings
from database.duckdb_manager import db
from core.event_engine import event_engine, EVENT_TICK, EVENT_ORDER, EVENT_TRADE

class QMTGateway:
    """基于纯粹 HTTP 代理模式的跨平台交易与行情网关

    v5.4 更新：proxy_url 改为 live_trader:8001 (原 qmt_proxy:8081 已废弃)
    live_trader 独占 xtquant 连接，避免多进程资源池冲突 (WaitingFreeWriter)

    性能优化：行情数据内存缓存 3 秒 TTL（减少重复 HTTP 请求）
    """

    def __init__(self):
        self._connected = True # 代理模式默认永远"连结"（只要请求能通）
        import os
        proxy_host = os.environ.get("LIVE_TRADER_HOST", os.environ.get("QMT_PROXY_HOST", "127.0.0.1"))
        # v5.4: 改为 live_trader:8001 (不再用 qmt_proxy:8081)
        self.proxy_url = f"http://{proxy_host}:8001/live"

        # v5.4 性能优化：记住 live_trader 的可用状态（避免反复超时）
        self._live_trader_available = True  # 初始假设可用
        self._live_trader_last_check = 0.0  # 上次检查时间
        self._live_trader_check_interval = 30.0  # 失败后 30 秒才重试

    def connect(self) -> bool:
        # 心跳检查代理是否存活
        try:
            resp = requests.get(f"{self.proxy_url}/health", timeout=3)
            return resp.status_code == 200
        except:
            log.warning("QMTGateway | 无法连接 live_trader (8001)，请检查 Windows 端 live_trader 是否启动")
            return False

    def buy(self, code: str, price: float, volume: int = None, reason: str = "手工买入") -> bool:
        """手动买入 (live_trader 暂未实现 order_service,待阶段2)"""
        log.warning(f"QMTGateway.buy() 当前不可用:live_trader 未实现手动下单接口(待阶段2 order_service)")
        return False

    def sell(self, code: str, price: float, volume: int, reason: str = "手工卖出") -> bool:
        """手动卖出 (live_trader 暂未实现 order_service,待阶段2)"""
        log.warning(f"QMTGateway.sell() 当前不可用:live_trader 未实现手动下单接口(待阶段2 order_service)")
        return False

    def get_balance(self) -> dict:
        """获取资产 (live_trader:/live/asset)"""
        try:
            data = requests.get(f"{self.proxy_url}/asset", timeout=3).json()
            # live_trader 返回 {cash, frozen_cash, market_value, total_asset}
            # 兼容旧格式 {balance, ...}
            if "total_asset" in data:
                return {
                    "balance": data.get("cash", 0),
                    "available": data.get("cash", 0),
                    "frozen": data.get("frozen_cash", 0),
                    "market_value": data.get("market_value", 0),
                    "total_asset": data.get("total_asset", 0),
                }
            return data
        except Exception as e:
            log.error(f"QMTGateway | 获取资产失败: {e}")
            return {}

    def get_position(self) -> list:
        """获取持仓 (live_trader:/live/positions)"""
        try:
            return requests.get(f"{self.proxy_url}/positions", timeout=3).json()
        except Exception as e:
            log.error(f"QMTGateway | 获取持仓失败: {e}")
            return []

    def get_live_trader_quotes(self, code_list: list) -> dict:
        """仅 live_trader:8001(QMT 实时),不含腾讯/Parquet 兜底。

        供 quote_source.QmtHttpAdapter 调用:QMT 源的纯取数。
        兜底(TDX/腾讯/Parquet)由 quote_source 的其他 adapter 负责,昨收规则也在那统一。
        不做结果缓存(quote_source orchestrator 已有 3s TTL);保留可用性熔断(30s)。
        """
        if not code_list:
            return {}
        now_time = time.time()
        should_try = (self._live_trader_available or
                      (now_time - self._live_trader_last_check > self._live_trader_check_interval))
        if not should_try:
            return {}
        codes_str = ",".join(str(c).strip() for c in code_list if str(c).strip())
        if not codes_str:
            return {}
        try:
            data = requests.get(f"{self.proxy_url}/quotes?codes={codes_str}", timeout=1.5).json()
            if data:
                self._live_trader_available = True
                self._live_trader_last_check = now_time
                return data
        except Exception as e:
            self._live_trader_available = False
            self._live_trader_last_check = now_time
            log.warning(f"QMTGateway | live_trader 行情失败(quote_source 将降级其他源): {e}")
        return {}

    def get_stock_list(self, details: bool = False, codes: list = None) -> list:
        """通过 Proxy 获取 QMT 全市场股票列表"""
        try:
            params = {"details": str(details).lower()}
            if codes:
                params["codes"] = ",".join(codes)
            res = requests.get(f"{self.proxy_url}/stocklist", params=params, timeout=120)
            data = res.json()
            if data.get("status") == "ok":
                return data.get("stocks" if details else "codes", [])
            log.error(f"QMTGateway | 获取股票列表失败: {data.get('message', 'unknown')}")
        except Exception as e:
            log.error(f"QMTGateway | 获取股票列表异常: {e}")
        return []

    def get_index_members(self, sector_name: str) -> list:
        """通过 QMT Proxy 获取指定板块（指数）的成分股列表"""
        try:
            resp = requests.get(
                f"{self.proxy_url}/index/members",
                params={"index": sector_name},
                timeout=30,
            )
            data = resp.json()
            if data.get("status") != "ok":
                log.error(f"QMT Proxy 返回错误: {data.get('message', 'unknown')}")
                return []
            stocks = data.get("stocks", [])
            return [s["code"].split(".")[0] for s in stocks if s.get("code")]
        except requests.exceptions.ConnectionError:
            log.error(f"无法连接到 QMT Proxy ({self.proxy_url})，请确保 Windows 端代理已启动")
        except Exception as e:
            log.error(f"获取板块 [{sector_name}] 成分股异常: {e}")
        return []

    def cancel_order(self, order_id):
        log.warning("QMTGateway | 当前代理实现尚未全量覆盖自动撤单协议")
        return False

qmt_gateway = QMTGateway()
