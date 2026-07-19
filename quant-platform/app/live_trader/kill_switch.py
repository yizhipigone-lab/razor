"""Kill Switch 一键停机(v5.4 §5.5 / §8.6)

三重状态:DB + 文件 + 内存,任一触发即生效。
4 触发方式:Web / API / 文件 / 自动。
解除:人工确认,系统不自动解除。
"""
import os
import threading
from datetime import datetime
from typing import Callable, Optional

from core.logger import get_logger

from .config import LiveTraderConfig

logger = get_logger("live_trader.kill_switch")


class KillSwitch:
    """一键停机(三重状态)"""

    def __init__(self, config: LiveTraderConfig, store=None, notify=None,
                 enabled_check: Optional[Callable[[], bool]] = None):
        self.config = config
        self.store = store
        self.notify = notify
        # 主开关回调:返回 False 时整个急停机制禁用(activate 空操作、is_active 恒 False)。
        # None=默认启用(向后兼容测试:KillSwitch(config, store) 不传此参数)。
        self._enabled_check = enabled_check

        self._memory_flag = False
        self._lock = threading.Lock()
        self._activated_at: Optional[datetime] = None
        self._reason = ""
        self._source = ""

        # 文件路径
        self._file_path = os.path.join(os.path.dirname(config.db_path), ".kill_switch")

        # 启动时检测文件(可能上次崩溃前留下的)
        self._load_from_file()

    def is_enabled(self) -> bool:
        """主开关是否启用。回调异常时 fail-safe 返回 True(保守:视为启用)。"""
        if self._enabled_check is None:
            return True
        try:
            return bool(self._enabled_check())
        except Exception as e:
            logger.warning(f"kill_switch enabled_check 异常,fail-safe 视为启用: {e}")
            return True

    def activate(self, reason: str = "", source: str = "manual") -> bool:
        """激活 kill switch(三重状态置位)。主开关禁用时为空操作,返回 False。"""
        if not self.is_enabled():
            logger.warning(f"kill switch 主开关已禁用,activate 被忽略: {reason} (source={source})")
            return False
        with self._lock:
            if self._memory_flag:
                logger.warning(f"kill switch 已激活,重复触发: {reason}")
                return False
            self._memory_flag = True
            self._activated_at = datetime.now()
            self._reason = reason
            self._source = source

        # DB
        if self.store:
            try:
                self.store.set_killswitch(True, reason, source, self._activated_at)
            except Exception as e:
                logger.error(f"kill switch 写 DB 失败: {e}")

        # 文件
        try:
            os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
            with open(self._file_path, "w") as f:
                f.write(f"{self._activated_at}|{reason}|{source}\n")
        except Exception as e:
            logger.error(f"kill switch 写文件失败: {e}")

        logger.critical(f"⚡ KILL SWITCH 激活: {reason} (source={source})")

        # 通知(多通道,M4)
        if self.notify:
            self.notify.kill_switch_activated(reason, source)
        return True

    def deactivate(self) -> bool:
        """解除(必须人工确认)。

        即使内存 flag 为 False,也清文件 + DB(幂等 no-op),防 DB-only 残留:
        场景 activate 时 DB 写成功但文件写失败 → 重启 __init__ 只读文件不读 DB →
        内存 flag=False → 若提前 return 则 DB 残留清不掉,重新启用时无新触发却复活急停。
        返回值:True=原先内存态激活且已解除;False=原先内存态未激活(文件/DB 仍照常清理)。
        """
        with self._lock:
            was_active = self._memory_flag
            self._memory_flag = False
            self._activated_at = None
            self._reason = ""
            self._source = ""

        if self.store:
            try:
                self.store.set_killswitch(False)
            except Exception as e:
                logger.error(f"kill switch 写 DB 失败: {e}")

        try:
            if os.path.exists(self._file_path):
                os.remove(self._file_path)
        except Exception as e:
            logger.error(f"kill switch 删文件失败: {e}")

        if not was_active:
            return False
        logger.info("kill switch 已解除(人工确认)")
        return True

    def is_active(self) -> bool:
        """检查是否激活(检查三重状态)。主开关禁用时恒返回 False。"""
        if not self.is_enabled():
            return False
        with self._lock:
            if self._memory_flag:
                return True
        # 检查文件(可能外部 touch 创建)
        if os.path.exists(self._file_path):
            self._load_from_file()
            with self._lock:
                if self._memory_flag:
                    return True
        # 检查 DB
        if self.store:
            try:
                state = self.store.get_killswitch()
                if state.get("activated"):
                    with self._lock:
                        self._memory_flag = True
                        self._activated_at = state.get("activated_at")
                        self._reason = state.get("reason", "")
                        self._source = state.get("source", "")
                    return True
            except Exception:
                pass
        return False

    def _load_from_file(self) -> None:
        """从文件加载状态"""
        try:
            if os.path.exists(self._file_path):
                with open(self._file_path, "r") as f:
                    content = f.read().strip()
                parts = content.split("|", 2)
                with self._lock:
                    self._memory_flag = True
                    self._activated_at = datetime.fromisoformat(parts[0]) if parts[0] else datetime.now()
                    self._reason = parts[1] if len(parts) > 1 else ""
                    self._source = parts[2] if len(parts) > 2 else "file"
                logger.warning(f"检测到 kill switch 文件,已加载: {self._reason}")
        except Exception as e:
            logger.error(f"加载 kill switch 文件失败: {e}")

    def status(self) -> dict:
        return {
            "activated": self.is_active(),
            "enabled": self.is_enabled(),
            "reason": self._reason,
            "activated_at": self._activated_at.isoformat() if self._activated_at else None,
            "source": self._source,
        }
