"""通知(v5.4 §10 / §19.5 M4)

飞书 + 企业微信 + 多通道告警(CRITICAL 级别)。
频控:企业微信 errcode=45009 重试 5 次后丢弃(非 CRITICAL);飞书 130102 仅日志。
CRITICAL 多通道:飞书/企业微信 + 桌面弹窗 + 日志标红。

通道选择(self.channel):
- "feishu":仅飞书
- "wework":仅企业微信
- "both"  :两者都发
- ""      :自动——飞书优先(配了 feishu_webhook 用飞书,否则企业微信,都空仅日志)
"""
import json
import threading
from datetime import datetime
from typing import Optional

import requests

from core.logger import get_logger

logger = get_logger("live_trader.notify")


class Notifier:
    """通知(飞书 + 企业微信 + 多通道)"""

    def __init__(self, webhook: str = "", multi_channel: bool = True,
                 feishu_webhook: str = "", channel: str = "",
                 notif_store=None):
        # webhook 为企业微信;保留 self.webhook 字段名向后兼容(现有代码可能 .webhook 访问)
        self.webhook = webhook
        self.feishu_webhook = feishu_webhook
        self.multi_channel = multi_channel
        # channel: "" 自动(feishu优先) / "feishu" / "wework" / "both"
        self.channel = channel or self._auto_channel()
        self._lock = threading.Lock()
        self._last_notify: dict = {}  # 频控
        self._notif_store = notif_store  # 通知历史存储(可选)

    def _auto_channel(self) -> str:
        """未显式指定 channel 时自动选:飞书优先,其次企业微信,都空则仅日志"""
        if self.feishu_webhook:
            return "feishu"
        if self.webhook:
            return "wework"
        return ""

    # ===== 通知历史(v6.0 Phase 1) =====

    def _record_history(self, level: str, title: str, content: str = "",
                        source: str = "") -> None:
        """写通知历史(仅写,不重试,失败仅日志)

        store 可能未注入或连接已关,try/except 兜底。
        """
        if not self._notif_store:
            return
        try:
            self._notif_store.record(level, title, content, source,
                                    channel=self.channel)
        except Exception as e:
            logger.warning(f"通知历史写失败(不影响发送): {e}")

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

    def _send_feishu(self, content: str, mentioned: bool = False) -> bool:
        """发飞书自定义机器人(text 消息)。

        payload: {"msg_type":"text","content":{"text": content}}
        @all: text 前置 <at user_id="all"></at>(机器人需开启 @所有人权限)。
        成功判定:StatusCode==0 或 code==0(飞书两种返回格式都判)。
        频控:飞书限 5 条/分钟,返回错误码(如 130102)→ 仅日志告警,不重试(与企业微信 45009 区分)。
        """
        if not self.feishu_webhook:
            logger.warning(f"飞书 webhook 未配置,告警仅日志: {content[:80]}")
            return False
        text = content
        if mentioned:
            text = '<at user_id="all"></at>\n' + content
        payload = {"msg_type": "text", "content": {"text": text}}
        try:
            resp = requests.post(self.feishu_webhook, json=payload, timeout=5)
            data = resp.json()
            # 飞书成功:{StatusCode:0,StatusMessage:"success"} 或 {code:0,msg:"success"}
            if data.get("StatusCode") == 0 or data.get("code") == 0:
                return True
            logger.error(f"飞书发送失败: {data}")
            return False
        except Exception as e:
            logger.error(f"飞书异常: {e}")
            return False

    def _dispatch(self, content: str, mentioned: bool = False) -> bool:
        """统一路由:按 self.channel 选通道。
        - 'feishu' → _send_feishu
        - 'wework' → _send_wework
        - 'both'   → 两个都发(任一成功即 True)
        - ''       → 仅日志(告警降级,与现状一致)
        """
        ch = self.channel
        if ch == "feishu":
            return self._send_feishu(content, mentioned=mentioned)
        if ch == "wework":
            return self._send_wework(content, mentioned=mentioned)
        if ch == "both":
            ok_f = self._send_feishu(content, mentioned=mentioned)
            ok_w = self._send_wework(content, mentioned=mentioned)
            return ok_f or ok_w
        # channel 为空:仅日志
        logger.warning(f"告警通道未配置,仅日志: {content[:80]}")
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
        """通用告警(警告级,仅当前通道 + 日志)

        供看门狗/信号跳过等非领域场景调用;CRITICAL 级请用 kill_switch_activated。
        返回告警是否投递成功(未配置通道时仅日志)。
        """
        with self._lock:
            ok = self._dispatch(content, mentioned=mentioned)
        self._record_history("INFO", "通用告警", content[:200], source="manual")
        return ok

    def order_traded(self, code: str, direction: str, volume: int,
                     price: float, mode: str) -> None:
        """成交通知(保留向后兼容,推荐用 order_traded_with_tag)"""
        arrow = "买入" if direction == "buy" else "卖出"
        mode_tag = "(测试单)" if mode == "dry-run" else ""
        content = (
            f"**实盘成交{mode_tag}**\n"
            f"> {code} {arrow} {volume}股 @{price:.2f}\n"
            f"> 金额:{volume*price:.2f}\n"
            f"> 时间:{datetime.now().strftime('%H:%M:%S')}"
        )
        with self._lock:
            self._dispatch(content)
        self._record_history("INFO", f"{arrow} {code}", content[:200],
                            source="order")

    def order_traded_with_tag(self, code: str, direction: str, volume: int,
                              price: float, mode: str, tag: str) -> None:
        """成交通知(带标签)

        Args:
            tag: "信号买入" / "止损" / "止盈" / "时间退出" / "强制退出" / "手动" / "其他"
        """
        arrow = "买入" if direction == "buy" else "卖出"
        mode_tag = "(测试单)" if mode == "dry-run" else ""
        content = (
            f"**实盘成交{mode_tag} [{tag}]**\n"
            f"> {code} {arrow} {volume}股 @{price:.2f}\n"
            f"> 金额:{volume*price:.2f}\n"
            f"> 时间:{datetime.now().strftime('%H:%M:%S')}"
        )
        with self._lock:
            self._dispatch(content)
        self._record_history("INFO", f"{tag} {arrow} {code}", content[:200],
                            source=f"order.{tag}")

    def order_error(self, order_id: int, error_msg: str) -> None:
        """废单告警"""
        content = f"**⚠ 废单告警**\n> 订单 {order_id}\n> 原因:{error_msg}"
        with self._lock:
            self._dispatch(content)
        self._record_history("WARN", f"废单 {order_id}", error_msg[:200],
                            source="order")

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
            self._dispatch(content, mentioned=True)
        if self.multi_channel:
            self._desktop_alert("KILL SWITCH 激活", f"{reason}\n来源:{source}")
        logger.critical(f"🔴🔴🔴 KILL SWITCH: {reason} 🔴🔴🔴")
        self._record_history("CRITICAL", "Kill Switch 激活", reason[:200],
                            source=source)

    def reconcile_diff(self, code: str, diff_vol: int, diff_value: float, level: str) -> None:
        """对账偏差告警"""
        content = (
            f"**⚠ 对账偏差 {level}**\n"
            f"> {code} 偏差 {diff_vol}股 / {diff_value:.2f}元\n"
            f"> 时间:{datetime.now().strftime('%H:%M:%S')}"
        )
        hist_level = "CRITICAL" if level.startswith("CRITICAL") else "WARN"
        with self._lock:
            self._dispatch(content)
        if level.startswith("CRITICAL") and self.multi_channel:
            self._desktop_alert(f"对账偏差 {level}", f"{code} 偏差 {diff_vol}股")
        self._record_history(hist_level, f"对账偏差 {code}",
                            content[:200], source="reconcile")

    def daily_summary(self, asset_data: dict, positions: list,
                      today_pnl: float, deal_count: int) -> None:
        """每日账户概览(15:30 定时发送)

        Args:
            asset_data: {"total_asset", "market_value", "cash", "frozen_cash"}
            positions: 持仓列表(含 code/name/market_value/float_profit)
            today_pnl: 当日盈亏(含正负号)
            deal_count: 今日成交笔数
        """
        from .notifications import format_daily_summary
        content = format_daily_summary(asset_data, positions, today_pnl, deal_count)
        with self._lock:
            self._dispatch(content)
        self._record_history("INFO", "每日账户概览",
                            content[:200], source="scheduler.daily_summary")
