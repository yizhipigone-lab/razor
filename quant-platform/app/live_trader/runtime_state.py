"""运行时状态(v2 §3.3/§3.4): 买入/卖出开关 + 模式(dry-run/live) + auto_buy 热开关

mode 从 config frozen dataclass 移出,运行时可变(A6)。
buy_enabled 复用原 buy_signal_enabled,移到此处运行时可变(A9)。
auto_buy_enabled 控制实盘自给自足尾盘选股(2026-07-14 新增,scheduler 读)。
持久化到 app_setting.json live_trader.runtime 段;threading.Lock 防并发(M5)。
"""
import threading
from typing import Optional

from core.logger import get_logger
from core.settings import settings

logger = get_logger("live_trader.runtime_state")


class RuntimeState:
    """运行时可变状态: buy_enabled / sell_enabled / mode / auto_buy_enabled"""

    def __init__(self, initial_mode: str = "dry-run",
                 initial_buy: bool = True, initial_sell: bool = True,
                 initial_ratio: float = 0.05,
                 initial_auto_buy: bool = False):
        self._lock = threading.Lock()
        self._mode = initial_mode
        self._buy_enabled = initial_buy
        self._sell_enabled = initial_sell
        self._buy_position_ratio = initial_ratio
        self._auto_buy_enabled = initial_auto_buy  # 实盘自给自足选股(默认关,真钱安全)

    # ===== mode =====
    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def is_live(self) -> bool:
        with self._lock:
            return self._mode == "live"

    def is_dry_run(self) -> bool:
        with self._lock:
            return self._mode == "dry-run"

    # ===== 开关 =====
    @property
    def buy_enabled(self) -> bool:
        with self._lock:
            return self._buy_enabled

    @property
    def sell_enabled(self) -> bool:
        with self._lock:
            return self._sell_enabled

    @property
    def auto_buy_enabled(self) -> bool:
        """实盘自给自足尾盘选股开关(scheduler._run_auto_buy_scan 读)"""
        with self._lock:
            return self._auto_buy_enabled

    # ===== 单只占本金比例(实盘 tab 热配;不进 _persist,由专用端点写 settings 顶层) =====
    @property
    def buy_position_ratio(self) -> float:
        with self._lock:
            return self._buy_position_ratio

    def set_buy_position_ratio(self, ratio: float) -> float:
        """设置单只占本金比例(仅改内存;持久化由端点写 settings 顶层)。返回旧值(供 audit)。"""
        if not (0 < ratio <= 1):
            raise ValueError(f"buy_position_ratio 必须在 (0,1],当前 {ratio}")
        with self._lock:
            old = self._buy_position_ratio
            self._buy_position_ratio = ratio
        logger.info(f"buy_position_ratio 切换: {old} -> {ratio}")
        return old

    # ===== 状态快照 =====
    def get_state(self) -> dict:
        with self._lock:
            return {
                "mode": self._mode,
                "buy_enabled": self._buy_enabled,
                "sell_enabled": self._sell_enabled,
                "auto_buy_enabled": self._auto_buy_enabled,
            }

    # ===== 变更(持久化 + 审计由调用方记) =====
    def set_mode(self, mode: str) -> str:
        """切换模式。返回旧 mode(供调用方记 audit)。"""
        if mode not in ("dry-run", "live"):
            raise ValueError(f"mode 必须是 dry-run/live,当前 {mode}")
        with self._lock:
            old = self._mode
            if old == mode:
                return old
            self._mode = mode
        self._persist()
        logger.info(f"mode 切换: {old} -> {mode}")
        return old

    def set_switches(self, buy_enabled: Optional[bool] = None,
                     sell_enabled: Optional[bool] = None,
                     auto_buy_enabled: Optional[bool] = None) -> dict:
        """切换买入/卖出/auto_buy 开关。返回旧状态(供 audit)。

        auto_buy_enabled 控制实盘自给自足尾盘选股;真钱系统可在此一秒热关。
        """
        with self._lock:
            old = {
                "buy_enabled": self._buy_enabled,
                "sell_enabled": self._sell_enabled,
                "auto_buy_enabled": self._auto_buy_enabled,
            }
            if buy_enabled is not None:
                self._buy_enabled = buy_enabled
            if sell_enabled is not None:
                self._sell_enabled = sell_enabled
            if auto_buy_enabled is not None:
                self._auto_buy_enabled = auto_buy_enabled
        self._persist()
        logger.info(f"开关切换: {old} -> buy={buy_enabled} sell={sell_enabled} auto_buy={auto_buy_enabled}")
        return old

    def _persist(self) -> None:
        """持久化到 app_setting.json live_trader.runtime 段(F6: 重启不丢)"""
        try:
            with self._lock:
                state = {
                    "mode": self._mode,
                    "buy_enabled": self._buy_enabled,
                    "sell_enabled": self._sell_enabled,
                    "auto_buy_enabled": self._auto_buy_enabled,
                }
            settings.set("live_trader", "runtime", state, save=True)
        except Exception as e:
            logger.error(f"RuntimeState 持久化失败(内存值仍有效): {e}")


def load_runtime_state(config) -> RuntimeState:
    """启动加载: live_trader.runtime 段优先,fallback config 种子(F6 单源)

    启动时若 runtime 段有 mode,以它为准(运行时切过);否则用 config.mode 种子。
    """
    try:
        rt = settings.get("live_trader", "runtime", default={}) or {}
        # buy_position_ratio 真相源是 live_trader.buy_position_ratio 顶层(不进 runtime 段),fallback config 种子
        ratio = settings.get("live_trader", "buy_position_ratio",
                             default=getattr(config, "buy_position_ratio", 0.05))
        return RuntimeState(
            initial_mode=rt.get("mode", getattr(config, "mode", "dry-run")),
            initial_buy=rt.get("buy_enabled", True),
            initial_sell=rt.get("sell_enabled", True),
            initial_ratio=float(ratio) if ratio is not None else 0.05,
            initial_auto_buy=rt.get(
                "auto_buy_enabled",
                getattr(config, "auto_buy_enabled", False),
            ),
        )
    except Exception as e:
        logger.warning(f"加载 runtime 段失败,用 config 种子: {e}")
        return RuntimeState(initial_mode=getattr(config, "mode", "dry-run"))
