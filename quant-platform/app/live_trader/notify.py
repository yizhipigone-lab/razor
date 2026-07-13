"""通知(v5.4 §10 / §19.5 M4)

企业微信 + 多通道告警(CRITICAL 级别)。
频控:errcode=45009 重试 5 次后丢弃(非 CRITICAL)。
CRITICAL 多通道:企业微信 + 桌面弹窗 + 日志标红。
"""
import json
import threading
from datetime import datetime
from typing import Optional

import requests

from core.logger import get_logger

logger = get_logger("live_trader.notify")


class Notifier:
    """通知(企业微信 + 多通道)"""

    def __init__(self, webhook: str = "", multi_channel: bool = True):
        self.webhook = webhook
        self.multi_channel = multi_channel
        self._lock = threading.Lock()
        self._last_notify: dict = {}  # 频控

    def _send_wework(self, content: str, mentioned: bool = False) -> bool:
        """发企业微信(频控 45009 重试5次)"""
        if not self.webhook:
            logger.warning(f"企业微信 webhook 未配置,告警仅日志: {content[:80]}")
            return False
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": content},
        }
        if mentioned:
            payload["markdown"]["mentioned_list"] = "@all"

        for attempt in range(5):
            try:
                resp = requests.post(self.webhook, json=payload, timeout=5)
                data = resp.json()
                if data.get("errcode") == 0:
                    return True
                if data.get("errcode") == 45009:
                    logger.warning(f"企业微信频控,1分钟后重试 ({attempt+1}/5)")
                    import time
                    time.sleep(60)
                    continue
                logger.error(f"企业微信发送失败: {data}")
                return False
            except Exception as e:
                logger.error(f"企业微信异常: {e}")
                return False
        logger.error(f"企业微信频控丢弃: {content[:80]}")
        return False

    def _desktop_alert(self, title: str, content: str) -> None:
        """桌面弹窗(Windows,CRITICAL 用)"""
        if not self.multi_channel:
            return
        try:
            # Windows MessageBox(非阻塞,子线程)
            import ctypes
            import threading
            def _show():
                ctypes.windll.user32.MessageBoxW(0, content, title, 0x30 | 0x40)  # MB_ICONWARNING|MB_TOPMOST
            threading.Thread(target=_show, daemon=True).start()
        except Exception:
            pass  # 非 Windows 无桌面弹窗

    def send(self, content: str, mentioned: bool = False) -> bool:
        """通用告警(警告级,仅企业微信 + 日志)

        供看门狗/信号跳过等非领域场景调用;CRITICAL 级请用 kill_switch_activated。
        返回企业微信是否投递成功(未配置 webhook 时仅日志)。
        """
        with self._lock:
            return self._send_wework(content, mentioned=mentioned)

    def order_traded(self, code: str, direction: str, volume: int,
                     price: float, mode: str) -> None:
        """成交通知"""
        arrow = "买入" if direction == "buy" else "卖出"
        mode_tag = "(测试单)" if mode == "dry-run" else ""
        content = (
            f"**实盘成交{mode_tag}**\n"
            f"> {code} {arrow} {volume}股 @{price:.2f}\n"
            f"> 金额:{volume*price:.2f}\n"
            f"> 时间:{datetime.now().strftime('%H:%M:%S')}"
        )
        with self._lock:
            self._send_wework(content)

    def order_error(self, order_id: int, error_msg: str) -> None:
        """废单告警"""
        content = f"**⚠ 废单告警**\n> 订单 {order_id}\n> 原因:{error_msg}"
        with self._lock:
            self._send_wework(content)

    def kill_switch_activated(self, reason: str, source: str, hint: str = "需人工介入") -> None:
        """kill switch 激活(CRITICAL,多通道)

        hint: 末尾行动提示, 默认"需人工介入"。自动可解除的场景(如 scheduler 非交易日
              激活的残留)传"交易日 09:20 自动解除, 无需人工"等, 避免误导人去手动操作。
        """
        content = (
            f"**🔴 KILL SWITCH 激活**\n"
            f"> 原因:{reason}\n"
            f"> 来源:{source}\n"
            f"> 时间:{datetime.now().strftime('%H:%M:%S')}\n"
            f"> **{hint}**"
        )
        with self._lock:
            self._send_wework(content, mentioned=True)
        if self.multi_channel:
            self._desktop_alert("KILL SWITCH 激活", f"{reason}\n来源:{source}")
        logger.critical(f"🔴🔴🔴 KILL SWITCH: {reason} 🔴🔴🔴")

    def reconcile_diff(self, code: str, diff_vol: int, diff_value: float, level: str) -> None:
        """对账偏差告警"""
        content = (
            f"**⚠ 对账偏差 {level}**\n"
            f"> {code} 偏差 {diff_vol}股 / {diff_value:.2f}元\n"
            f"> 时间:{datetime.now().strftime('%H:%M:%S')}"
        )
        with self._lock:
            self._send_wework(content)
        if level.startswith("CRITICAL") and self.multi_channel:
            self._desktop_alert(f"对账偏差 {level}", f"{code} 偏差 {diff_vol}股")
