"""xtquant 适配层(v5.4 §5.1 / §10)

封装所有 xtquant 调用,隔离 SDK 升级风险。
关键设计:
- 子线程实例化 XtQuantTrader(避免劫持主 asyncio loop)
- 所有同步调用 3 秒超时(ThreadPoolExecutor + Future.result)
- 复用 app/utils/xtquant_compat.py(4层 XtAccount 兼容 + safe_getattr)
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.logger import get_logger

from .config import LiveTraderConfig
from .exceptions import QmtConnectionError, QmtOrderError, QmtTimeoutError

logger = get_logger("live_trader.qmt_wrapper")

# xtquant 是 Windows 独占,延迟导入
_xtquant_available = False
try:
    from xtquant import xtdata
    from xtquant.xttrader import XtQuantTrader
    from app.utils.xtquant_compat import (
        get_stock_account_class, safe_getattr, safe_float, safe_int,
        format_code,
        ORDER_TYPE_BUY, ORDER_TYPE_SELL,
    )
    _xtquant_available = True
except ImportError as e:
    logger.warning(f"xtquant 未安装(非 Windows 环境?): {e}")


def _format_quote_code(code: str) -> str:
    """决定传给 xtdata.get_full_tick 的查询码(解决 000001 股票/指数歧义)。

    规则:
    - 带后缀的码:后缀已确定身份,原样返回。000001.SZ=平安银行(深市股票),
      000001.SH=上证指数(沪市指数)——绝不剥后缀重判,否则 000001.SZ 会被查成
      000001.SH 指数点位(2026-07-16"平安银行变上证指数"事故根因)。
    - 裸码:is_index_code 命中指数表 → format_index_code 强制 .SH(指数查询场景);
      否则 format_code 按数字补后缀(裸 000001 默认按深市股票)。

    纯函数(仅依赖 xtquant_compat,无 xtquant 依赖),便于单测。
    """
    from app.utils.xtquant_compat import format_code, format_index_code, is_index_code
    c = str(code)
    if '.' in c:
        return c  # 后缀已确定身份,原样返回
    if is_index_code(c):
        return format_index_code(c)
    return format_code(c)


class QmtWrapper:
    """xtquant 适配层(单例,由 ConnectionManager 持有)"""

    def __init__(self, config: LiveTraderConfig):
        self.config = config
        self._trader: Optional[Any] = None  # XtQuantTrader 实例
        self._account: Optional[Any] = None  # StockAccount 实例
        self._call_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="qmt-call")
        logger.info("QmtWrapper 初始化")

    @property
    def available(self) -> bool:
        return _xtquant_available

    def set_trader(self, trader: Any, account: Any) -> None:
        """由 ConnectionManager 在连接成功后注入"""
        self._trader = trader
        self._account = account

    def _call_with_timeout(self, func, *args, timeout: Optional[float] = None, **kwargs) -> Any:
        """所有 xtquant 同步调用包 3 秒超时(v5.1 §10.2)"""
        if not self._trader:
            raise QmtConnectionError("XtQuantTrader 未连接")
        t = timeout or self.config.qmt_call_timeout_sec
        future = self._call_executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=t)
        except FutureTimeout:
            raise QmtTimeoutError(f"xtquant 调用超时({t}s): {func.__name__}")
        except Exception as e:
            if "WaitingFreeWriter" in str(e):
                raise  # 由 ConnectionManager 处理强退
            raise QmtConnectionError(f"xtquant 调用失败: {e}")

    # ===== 行情 =====

    def connect_xtdata(self) -> Any:
        """xtdata 基础行情连接(两层连接模型第1层)"""
        if not _xtquant_available:
            raise QmtConnectionError("xtquant 未安装")
        return xtdata.connect()

    def get_realtime_quotes(self, codes: List[str]) -> Dict[str, Dict[str, float]]:
        """获取实时行情(批量)
        返回 key 严格用入参的 codes_fmt(避免 QMT 内部把 000001 默认映射成 000001.SH 上证指数)
        """
        if not _xtquant_available:
            return {}
        try:
            codes_fmt = [_format_quote_code(c) for c in codes]
            # 裸码 → 入参 fmt 的反向映射(第一个 wins,匹配原线性扫描语义);
            # QMT 可能返回不同后缀(例如 000001 → 000001.SH 指数),按裸码 O(1) 校正 key
            bare_to_fmt = {}
            for c in codes_fmt:
                bare_to_fmt.setdefault(c.split('.')[0], c)
            ticks = xtdata.get_full_tick(codes_fmt)
            result = {}
            for code, tick in (ticks or {}).items():
                if tick is None:
                    continue
                bare = code.split('.')[0] if '.' in code else code
                key = bare_to_fmt.get(bare) or code  # 找不到就用原 key 兜底
                if isinstance(tick, dict):
                    result[key] = {
                        "lastPrice": safe_float(tick.get("lastPrice", 0)),
                        "lastClose": safe_float(tick.get("lastClose", 0)),
                        "open": safe_float(tick.get("open", 0)),
                        "high": safe_float(tick.get("high", 0)),
                        "low": safe_float(tick.get("low", 0)),
                        "volume": safe_float(tick.get("volume", 0)),
                    }
                else:
                    result[key] = {
                        "lastPrice": safe_float(safe_getattr(tick, "lastPrice", 0)),
                        "lastClose": safe_float(safe_getattr(tick, "lastClose", 0)),
                        "open": safe_float(safe_getattr(tick, "open", 0)),
                        "high": safe_float(safe_getattr(tick, "high", 0)),
                        "low": safe_float(safe_getattr(tick, "low", 0)),
                        "volume": safe_float(safe_getattr(tick, "volume", 0)),
                    }
            return result
        except Exception as e:
            logger.error(f"获取行情失败: {e}")
            return {}

    # ===== Tick 订阅(2026-07-15 PLAN-tick-subscription Step 1) =====

    def subscribe_quote(self, codes: List[str], callback) -> int:
        """订阅实时 tick 推送(批量)。返回订阅 seq(int),供 unsubscribe 用。

        用 subscribe_whole_quote(批量); POC 实测回调收 {code: tick_dict}。
        tick_dict 字段: lastPrice/open/high/low/lastClose/askPrice/bidPrice/...
        """
        if not _xtquant_available:
            return 0
        codes_fmt = [format_code(c.split('.')[0]) for c in codes]
        seq = xtdata.subscribe_whole_quote(codes_fmt, callback=callback)
        logger.info(f"subscribe_whole_quote({codes_fmt}) seq={seq}")
        return seq

    def unsubscribe_quote(self, seq: int) -> None:
        """取消 tick 订阅。POC 实测 unsubscribe_quote 签名是 (seq: int) 非 code。"""
        if not _xtquant_available or not seq:
            return
        try:
            xtdata.unsubscribe_quote(seq)
            logger.info(f"unsubscribe_quote(seq={seq})")
        except Exception as e:
            logger.warning(f"unsubscribe_quote(seq={seq}) 失败(可忽略): {e}")

    # ===== 行情扩展(从 qmt_proxy_server.py 迁移) =====
    def get_stock_list_in_sector(self, market_name: str) -> List[str]:
        """获取板块/指数成分股代码列表(如 '上证A股', '沪深300')"""
        if not _xtquant_available:
            return []
        try:
            return xtdata.get_stock_list_in_sector(market_name) or []
        except Exception as e:
            logger.error(f"获取板块 {market_name} 成分股列表失败: {e}")
            return []

    def get_instrument_detail(self, code: str) -> Dict[str, Any]:
        """获取单只股票详情(名称/行业/上市日期等)"""
        if not _xtquant_available:
            return {}
        try:
            code_fmt = format_code(code.split('.')[0])
            d = xtdata.get_instrument_detail(code_fmt)
            return d if d else {}
        except Exception as e:
            logger.error(f"获取 {code} 详情失败: {e}")
            return {}

    # ===== 交易 =====

    def order_stock_async(self, code: str, order_type: int, volume: int,
                          price_type: int, price: float,
                          strategy_name: str = "", order_remark: str = "") -> int:
        """异步下单(返回 seq,≤0 失败)

        Args:
            code: 股票代码(自动 format_code)
            order_type: 23买/24卖
            volume: 股数(板块取整已处理)
            price_type: 11限价/44对手最优/42沪五档/47深五档
            price: 限价单填价格,市价单填0
        Returns:
            seq > 0 成功,≤ 0 失败
        """
        if not self._trader or not self._account:
            raise QmtConnectionError("未连接,无法下单")
        code_fmt = format_code(code) if '.' not in code else code

        def _do():
            return self._trader.order_stock_async(
                self._account, code_fmt, order_type, volume, price_type, price,
                strategy_name, order_remark
            )

        seq = self._call_with_timeout(_do)
        if seq <= 0:
            last_err = self._safe_last_error()
            raise QmtOrderError(f"下单失败 seq={seq} code={code_fmt} err={last_err}")
        logger.info(f"异步下单 seq={seq} code={code_fmt} type={order_type} vol={volume} pt={price_type}")
        return seq

    def cancel_order(self, order_id: int) -> int:
        """撤单(返回 0=成功,-1断开,-3未找到)"""
        if not self._trader or not self._account:
            raise QmtConnectionError("未连接,无法撤单")

        def _do():
            return self._trader.cancel_order_stock(self._account, order_id)

        return self._call_with_timeout(_do)

    def _safe_last_error(self) -> str:
        try:
            return str(self._trader.get_last_error()) if self._trader else ""
        except Exception:
            return ""

    # ===== 查询 =====

    def query_asset(self) -> Optional[Dict[str, float]]:
        """资金查询"""
        if not self._trader or not self._account:
            return None

        def _do():
            return self._trader.query_stock_asset(self._account)

        asset = self._call_with_timeout(_do)
        if asset is None:
            return None
        return {
            "account_id": safe_getattr(asset, "account_id", ""),
            "cash": safe_float(safe_getattr(asset, "cash", 0)),
            "frozen_cash": safe_float(safe_getattr(asset, "frozen_cash", 0)),
            "market_value": safe_float(safe_getattr(asset, "market_value", 0)),
            "total_asset": safe_float(safe_getattr(asset, "total_asset", 0)),
        }

    def query_positions(self) -> List[Dict[str, Any]]:
        """持仓查询

        xtquant 的 query_stock_positions 返回 SDK 本地缓存,偶发返回空/部分
        (缓存刷新窗口)。首次返回空时重试,避免下游(对账/清理)基于空结果
        做误判。部分结果由调用方(cleanup)做并集重试兜底。
        """
        if not self._trader or not self._account:
            return []

        def _do():
            return self._trader.query_stock_positions(self._account)

        for attempt in range(3):
            try:
                positions = self._call_with_timeout(_do)
            except QmtTimeoutError:
                # 超时:最后一次直接抛,由调用方兜底
                if attempt == 2:
                    raise
                time.sleep(0.15)
                continue
            if positions:
                result = []
                for pos in positions:
                    result.append({
                        "code": safe_getattr(pos, "stock_code", ""),
                        "volume": safe_int(safe_getattr(pos, "volume", 0)),
                        "can_use_volume": safe_int(safe_getattr(pos, "can_use_volume", 0)),
                        "frozen_volume": safe_int(safe_getattr(pos, "frozen_volume", 0)),
                        "avg_cost": safe_float(safe_getattr(pos, "avg_price", 0)),
                        "last_price": safe_float(safe_getattr(pos, "last_price", 0)),
                        "market_value": safe_float(safe_getattr(pos, "market_value", 0)),
                        "profit": safe_float(safe_getattr(pos, "position_profit", 0)),
                    })
                return result
            # 空结果:可能是缓存未就绪,短暂等待后重试
            if attempt < 2:
                time.sleep(0.15)
        logger.warning("query_stock_positions 连续 3 次返回空(缓存未就绪?)")
        return []

    def query_orders(self, cancelable_only: bool = False) -> List[Dict[str, Any]]:
        """委托查询"""
        if not self._trader or not self._account:
            return []

        def _do():
            return self._trader.query_stock_orders(self._account, cancelable_only)

        orders = self._call_with_timeout(_do)
        if not orders:
            return []
        result = []
        for o in orders:
            result.append({
                "order_id": safe_getattr(o, "order_id", 0),
                "code": safe_getattr(o, "stock_code", ""),
                "order_type": safe_int(safe_getattr(o, "order_type", 0)),
                "status": safe_int(safe_getattr(o, "order_status", 255)),  # int 类型(H6)
                "volume": safe_int(safe_getattr(o, "order_volume", 0)),
                "price": safe_float(safe_getattr(o, "price", 0)),
                "traded_volume": safe_int(safe_getattr(o, "traded_volume", 0)),
                "traded_price": safe_float(safe_getattr(o, "traded_price", 0)),
            })
        return result

    def query_order_by_id(self, order_id: int) -> Optional[Dict[str, Any]]:
        """查单个订单(ack 轮询用)"""
        if not self._trader or not self._account:
            return None

        def _do():
            return self._trader.query_stock_order(self._account, order_id)

        o = self._call_with_timeout(_do)
        if o is None:
            return None
        return {
            "order_id": safe_getattr(o, "order_id", 0),
            "status": safe_int(safe_getattr(o, "order_status", 255)),
            "traded_volume": safe_int(safe_getattr(o, "traded_volume", 0)),
            "traded_price": safe_float(safe_getattr(o, "traded_price", 0)),
        }

    @property
    def connected(self) -> bool:
        try:
            return bool(self._trader and self._trader.connected)
        except Exception:
            return False

    def stop(self) -> None:
        """停止"""
        try:
            if self._trader:
                self._trader.stop()
        except Exception as e:
            logger.error(f"trader.stop 异常: {e}")
        self._call_executor.shutdown(wait=False, timeout=3)
