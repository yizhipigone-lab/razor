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

        # 行情缓存：{code: (data, expire_ts)}
        self._quote_cache = {}
        self._cache_ttl = 3  # 秒（盘中 3 秒足够，减少 HTTP 请求）

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

    def get_realtime_quotes(self, code_list: list) -> dict:
        """获取行情 (live_trader:/live/quotes?codes=...)

        三级回退（CLAUDE.md 规则）：
        1. live_trader:8001 (QMT 实时行情，首选)
        2. 腾讯 HTTP 行情 (live_trader 连不上时)
        3. Parquet 历史收盘价 (前两者都失败时兜底)

        性能优化：内存缓存 3 秒 TTL
        """
        if not code_list:
            return {}

        # 检查缓存（减少重复 HTTP 请求）
        now = time.time()
        result = {}
        uncached_codes = []

        for code in code_list:
            code_str = str(code).strip()
            if not code_str:
                continue
            cached = self._quote_cache.get(code_str)
            if cached and cached[1] > now:  # 未过期
                result[code_str] = cached[0]
            else:
                uncached_codes.append(code_str)

        # 全部命中缓存，直接返回
        if not uncached_codes:
            return result

        codes_str = ",".join(uncached_codes)
        now_time = time.time()

        # 1. 优先 live_trader:8001（快速失败优化：连续失败后跳过 30 秒）
        should_try_live_trader = (
            self._live_trader_available or
            (now_time - self._live_trader_last_check > self._live_trader_check_interval)
        )

        if should_try_live_trader:
            try:
                data = requests.get(f"{self.proxy_url}/quotes?codes={codes_str}", timeout=1.5).json()
                if data:  # 成功拿到数据
                    # 标记为可用
                    self._live_trader_available = True
                    self._live_trader_last_check = now_time
                    # 更新缓存
                    expire_ts = now + self._cache_ttl
                    for code, quote in data.items():
                        self._quote_cache[code] = (quote, expire_ts)
                        result[code] = quote
                    return result
            except Exception as e:
                # 标记为不可用，30 秒后重试
                self._live_trader_available = False
                self._live_trader_last_check = now_time
                log.warning(f"QMTGateway | live_trader 行情失败(降级腾讯，30s后重试): {e}")
        else:
            log.debug(f"QMTGateway | live_trader 暂时不可用，直接降级腾讯行情")

        # 2. 降级腾讯 HTTP
        try:
            tencent_data = self._fallback_tencent_quotes(uncached_codes)
            if tencent_data:
                # 更新缓存
                expire_ts = now + self._cache_ttl
                for code, quote in tencent_data.items():
                    self._quote_cache[code] = (quote, expire_ts)
                    result[code] = quote
                return result
        except Exception as e:
            log.warning(f"QMTGateway | 腾讯行情失败(降级 Parquet): {e}")

        # 3. 兜底 Parquet
        try:
            parquet_data = self._fallback_parquet_quotes(uncached_codes)
            if parquet_data:
                # 更新缓存（Parquet 是历史数据，可以缓存更久）
                expire_ts = now + 60  # Parquet 缓存 60 秒
                for code, quote in parquet_data.items():
                    self._quote_cache[code] = (quote, expire_ts)
                    result[code] = quote
            return result
        except Exception as e:
            log.error(f"QMTGateway | Parquet 行情也失败: {e}")
            return result  # 返回部分缓存的结果（如果有）

    def _fallback_tencent_quotes(self, code_list: list) -> dict:
        """腾讯 HTTP 行情降级（移植自 sim_trader data_loader）"""
        import requests
        result = {}
        for code in code_list:
            code_str = str(code).strip()
            if not code_str:
                continue
            market = "sz" if code_str.startswith(("0", "3", "159")) else "sh"
            try:
                url = f"http://qt.gtimg.cn/q={market}{code_str}"
                r = requests.get(url, timeout=2)
                r.encoding = "gbk"
                txt = r.text.strip()
                if "~" not in txt:
                    continue
                parts = txt.split("~")
                if len(parts) < 40:
                    continue
                result[code_str] = {
                    "lastPrice": float(parts[3]) if parts[3] else 0,
                    "lastClose": float(parts[4]) if parts[4] else 0,
                    "open": float(parts[5]) if parts[5] else 0,
                    "high": float(parts[33]) if parts[33] else 0,
                    "low": float(parts[34]) if parts[34] else 0,
                }
            except Exception:
                pass
        return result

    def _fallback_parquet_quotes(self, code_list: list) -> dict:
        """Parquet 历史收盘价兜底（取最新一条）"""
        import os
        import pandas as pd
        result = {}
        for code in code_list:
            code_str = str(code).strip()
            if not code_str:
                continue
            # 去掉 .SH/.SZ 后缀
            code_bare = code_str.split(".")[0]
            pq_path = f"data/parquet/daily/{code_bare}.parquet"
            if not os.path.exists(pq_path):
                continue
            try:
                df = pd.read_parquet(pq_path)
                if df.empty:
                    continue
                last = df.iloc[-1]
                result[code_str] = {
                    "lastPrice": float(last.get("close", 0)),
                    "lastClose": float(last.get("close", 0)),  # Parquet 无昨收，用收盘代替
                    "open": float(last.get("open", 0)),
                    "high": float(last.get("high", 0)),
                    "low": float(last.get("low", 0)),
                }
            except Exception:
                pass
        return result

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
