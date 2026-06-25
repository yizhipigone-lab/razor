"""
轻量级事件驱动引擎 - 类 VN.PY 风格
用于盘中监控循环、数据更新完成通知、买卖信号发出等异步消息传递。
"""
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Any
from core.logger import get_logger

log = get_logger("EventEngine")

# ---- 事件类型常量 ----
EVENT_TIMER = "eTimer"               # 定时器事件（盘中轮询）
EVENT_TICK = "eTick"                 # 实时行情 Tick
EVENT_BAR = "eBar"                   # K 线数据
EVENT_ORDER = "eOrder"               # 订单变化
EVENT_TRADE = "eTrade"               # 成交事件
EVENT_POSITION = "ePosition"         # 持仓变化
EVENT_SIGNAL = "eSignal"             # 策略选股信号
EVENT_RISK = "eRisk"                 # 风控触发事件
EVENT_LOG = "eLog"                   # 日志事件
EVENT_DATA_READY = "eDataReady"      # 数据下载完成


@dataclass
class Event:
    type: str
    data: Any = field(default=None)
    timestamp: datetime = field(default_factory=datetime.now)


class EventEngine:
    """
    异步事件引擎，支持多监听器注册与事件广播。
    计时器通过后台线程定期生成 EVENT_TIMER 事件。
    """

    def __init__(self):
        # L27 修复: 删除只进不出的假异步 _queue, 改为纯同步 dispatch
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._timer_thread: threading.Thread | None = None
        self._timer_interval: float = 60.0  # 秒

    # --- 注册与注销 ---

    def register(self, event_type: str, handler: Callable):
        """注册事件监听器"""
        with self._lock:
            if handler not in self._handlers[event_type]:
                self._handlers[event_type].append(handler)
                log.debug(f"注册监听器: {event_type} -> {handler.__qualname__}")

    def unregister(self, event_type: str, handler: Callable):
        """注销事件监听器"""
        with self._lock:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

    # --- 发送事件 ---

    def put(self, event: Event):
        """投放事件并同步广播给所有监听器（线程安全）"""
        self._process(event)

    def emit(self, event_type: str, data: Any = None):
        """快捷发送事件"""
        self.put(Event(type=event_type, data=data))

    # --- 内部处理 ---

    def _process(self, event: Event):
        """广播事件给所有对应监听器"""
        with self._lock:
            handlers = list(self._handlers.get(event.type, []))
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                log.error(f"事件处理异常 [{event.type}] in {handler.__qualname__}: {e}")

    # --- 定时器 ---

    def set_timer(self, interval_seconds: float):
        """设置定时器间隔（秒）"""
        self._timer_interval = interval_seconds

    def _timer_loop(self):
        """后台定时器线程，定期触发 EVENT_TIMER"""
        import time
        while self._running:
            time.sleep(self._timer_interval)
            if self._running:
                self.emit(EVENT_TIMER, {"timestamp": datetime.now()})

    def start(self, timer_interval: float = 60.0):
        """启动事件引擎"""
        self._running = True
        self._timer_interval = timer_interval
        self._timer_thread = threading.Thread(
            target=self._timer_loop, daemon=True, name="EventTimer"
        )
        self._timer_thread.start()
        log.info(f"事件引擎已启动，定时器间隔={timer_interval}秒")

    def stop(self):
        """停止事件引擎"""
        self._running = False
        log.info("事件引擎已停止")


# 全局事件引擎单例
event_engine = EventEngine()
