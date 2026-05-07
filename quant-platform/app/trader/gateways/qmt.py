import requests
import time
import pandas as pd
from loguru import logger as log
from core.settings import settings
from database.duckdb_manager import db
from core.event_engine import event_engine, EVENT_TICK, EVENT_ORDER, EVENT_TRADE

class QMTGateway:
    """基于纯粹 HTTP 代理模式的跨平台交易与行情网关"""
    
    def __init__(self):
        self._connected = True # 代理模式默认永远"连结"（只要请求能通）
        import os
        proxy_host = os.environ.get("QMT_PROXY_HOST", "host.docker.internal")
        self.proxy_url = f"http://{proxy_host}:8081/api"
        
    def connect(self) -> bool:
        # 心跳检查代理是否存活
        try:
            resp = requests.get(f"{self.proxy_url}/balance", timeout=3)
            return resp.status_code == 200
        except:
            log.warning("QMTGateway | 无法连接宿主机代理服务(请检查 Windows 端是否启动了 qmt_proxy_server.py)")
            return False

    def buy(self, code: str, price: float, volume: int = None, reason: str = "手工买入") -> bool:
        from core.settings import calc_buy_volume
        if volume is None: volume = calc_buy_volume(price)
        if volume <= 0: return False
        
        try:
            res = requests.post(f"{self.proxy_url}/order", json={
                "code": code,
                "price": price,
                "volume": volume,
                "direction": 23
            }, timeout=3).json()
            return res.get("status") == "ok"
        except Exception as e:
            log.error(f"QMTGateway | 买入委托发送异常: {e}")
            return False

    def sell(self, code: str, price: float, volume: int, reason: str = "手工卖出") -> bool:
        try:
            res = requests.post(f"{self.proxy_url}/order", json={
                "code": code,
                "price": price,
                "volume": volume,
                "direction": 24
            }, timeout=3).json()
            return res.get("status") == "ok"
        except Exception as e:
            log.error(f"QMTGateway | 卖出委托发送异常: {e}")
            return False

    def get_balance(self) -> dict:
        try:
            return requests.get(f"{self.proxy_url}/balance", timeout=3).json()
        except Exception as e:
            log.error(f"QMTGateway | 代理通信超时 (获取资产): {e}")
            return {}

    def get_position(self) -> list:
        try:
            return requests.get(f"{self.proxy_url}/position", timeout=3).json()
        except Exception as e:
            log.error(f"QMTGateway | 代理通信超时 (获取持仓): {e}")
            return []

    def get_realtime_quotes(self, code_list: list) -> dict:
        if not code_list: return {}
        try:
            codes_str = ",".join([str(c) for c in code_list if str(c).strip()])
            return requests.get(f"{self.proxy_url}/quotes?codes={codes_str}", timeout=3).json()
        except:
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
