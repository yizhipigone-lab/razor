"""实盘定时调度服务(§5.7 / §5.9 / §17.3 / §6 EOD 归档)

后台 asyncio 任务,负责:
- 离场扫描(可配置间隔,默认60s,交易时段)
- 对账(4 时点:09:35/11:30/14:55/15:05)
- EOD 归档(15:01)
- 资产备份(5 分钟间隔,交易时段)
- 非交易日自动激活 kill switch(§17.3)
- 交易日 09:20 自动解除 scheduler 激活的 kill switch
"""
import asyncio
import time
from datetime import date, datetime
from typing import Optional

from core.logger import get_logger

logger = get_logger("live_trader.scheduler")


class LiveScheduler:
    """实盘定时调度(单 asyncio Task)"""

    def __init__(self, config, store=None, qmt=None, exit_monitor=None,
                 reconciler=None, kill_switch=None, notifier=None, audit=None):
        self.config = config
        self.store = store
        self.qmt = qmt
        self.exit_monitor = exit_monitor
        self.reconciler = reconciler
        self.kill_switch = kill_switch
        self.notifier = notifier
        self.audit = audit

        self._task: Optional[asyncio.Task] = None
        self._last_scan_time: float = 0.0
        self._last_asset_backup_time: float = 0.0
        self._last_quotes_refresh_time: float = 0.0
        self._today: str = ""
        self._executed_today: set = set()

        # 离场扫描间隔(可运行时修改,默认从 config 读取)
        self._exit_scan_interval: float = getattr(config, 'exit_scan_interval_sec', 60.0)

        # 持仓行情刷新间隔(固定 3s,与 QMT 客户端推送频率一致)
        self._quotes_refresh_interval: float = 3.0

    def start(self) -> None:
        """启动调度任务"""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("调度服务启动")

    def stop(self) -> None:
        """停止调度任务"""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("调度服务停止")

    def set_scan_interval(self, seconds: float) -> None:
        """运行时修改离场扫描间隔(前端保存后立即生效,不阻塞)"""
        seconds = max(10.0, min(300.0, float(seconds)))  # 限制 [10, 300]
        self._exit_scan_interval = seconds
        logger.info(f"离场扫描间隔已更新: {seconds}s")

    def get_scan_interval(self) -> float:
        """获取当前离场扫描间隔"""
        return self._exit_scan_interval

    async def _loop(self) -> None:
        """主循环(细粒度调度,1s 一次 tick 内部按子任务间隔节流)"""
        while True:
            try:
                self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"调度异常: {e}")
            await asyncio.sleep(1)

    def _tick(self) -> None:
        """单次调度检查"""
        now = datetime.now()
        today_key = now.date().isoformat()
        current_time = now.strftime("%H:%M")

        # 新的一天,重置已执行记录
        if today_key != self._today:
            self._executed_today.clear()
            self._today = today_key
            logger.info(f"调度:新交易日 {today_key}")

        # ===== 非交易日检测(§17.3)=====
        if now.weekday() >= 5:  # 周六=5, 周日=6
            self._handle_non_trading_day()
            return

        # ===== 交易日 09:20 自动解除(§17.3)=====
        if current_time >= "09:20" and "trading_day_activate" not in self._executed_today:
            self._handle_trading_day_activate()
            self._executed_today.add("trading_day_activate")

        # ===== 交易时段(09:25 ~ 15:05)=====
        if "09:25" <= current_time <= "15:05":
            # 持仓行情刷新(3s 间隔,与 QMT 客户端推送频率一致)
            self._run_quotes_refresh()

            # 离场扫描(60s 间隔)
            self._run_exit_scan()

            # 资产备份(5 分钟间隔)
            self._run_asset_backup()

            # 对账(4 时点)
            self._run_reconcile(current_time)

            # EOD 归档(15:01)
            if current_time >= "15:01" and "eod_archive" not in self._executed_today:
                self._run_eod_archive()
                self._executed_today.add("eod_archive")

        # ===== 信号心跳看门狗(14:55, v1.2.2 §5.2) =====
        if current_time >= "14:55" and "signal_heartbeat_check" not in self._executed_today:
            self._check_signal_heartbeat()
            self._executed_today.add("signal_heartbeat_check")

    # ===== 各调度任务 =====

    def _handle_non_trading_day(self) -> None:
        """非交易日:自动激活 kill switch"""
        if "non_trading_check" not in self._executed_today:
            if self.kill_switch and not self.kill_switch.is_active():
                self.kill_switch.activate(
                    reason="非交易日自动激活(周末/节假日)",
                    source="scheduler"
                )
                logger.info("调度:非交易日,kill switch 已激活")
                if self.notifier:
                    self.notifier.kill_switch_activated(
                        "非交易日,实盘监控暂停", "scheduler"
                    )
            self._executed_today.add("non_trading_check")

    def _handle_trading_day_activate(self) -> None:
        """交易日 09:20:自动解除由 scheduler 激活的 kill switch"""
        if not self.kill_switch or not self.kill_switch.is_active():
            return
        ks_status = self.kill_switch.status()
        if ks_status.get("source") == "scheduler":
            self.kill_switch.deactivate()
            logger.info("调度:交易日 09:20 自动解除 kill switch")
            if self.audit:
                self.audit.log("scheduler_activate", reason="交易日自动解除 kill switch")

    def _run_exit_scan(self) -> None:
        """离场扫描(可配置间隔,默认60s)"""
        if time.time() - self._last_scan_time < self._exit_scan_interval:
            return
        if not self.exit_monitor:
            return
        if self.kill_switch and self.kill_switch.is_active():
            return
        try:
            actions = self.exit_monitor.scan_once()
            if actions:
                logger.info(f"离场扫描:执行 {len(actions)} 笔卖出")
        except Exception as e:
            logger.error(f"离场扫描异常: {e}")
        self._last_scan_time = time.time()

    def _run_asset_backup(self) -> None:
        """资产备份(5 分钟间隔,闸门5a 基准 + EOD 备份)"""
        if time.time() - self._last_asset_backup_time < 300:
            return
        if not self.qmt or not self.qmt.connected:
            return
        if not self.store:
            return
        try:
            asset_data = self.qmt.query_asset()
            if asset_data:
                self.store.backup_asset(asset_data)
        except Exception as e:
            logger.error(f"资产备份异常: {e}")
        self._last_asset_backup_time = time.time()

    def _run_quotes_refresh(self) -> None:
        """持仓行情刷新(3s 间隔)

        把 QMT 实时行情写回 live_positions 表的 last_price/market_value/float_profit,
        让前端展示与 QMT 客户端保持一致。

        实现要点:
        - 3s 节流(与 QMT 客户端推送频率一致)
        - 仅交易时段(09:25-15:05)执行
        - QMT 断开/持仓为空/无行情 → 直接跳过(不写脏)
        - QMT 调用 3s 超时(qmt_wrapper 内置)失败由 try/except 兜底
        - 仅写现价/市值/浮盈,不触碰 volume/avg_cost(防误改关键数据)
        """
        if time.time() - self._last_quotes_refresh_time < self._quotes_refresh_interval:
            return
        if not self.qmt or not self.qmt.connected:
            return
        if not self.store:
            return
        try:
            positions = self.store.get_positions()
            if not positions:
                return
            # 本地代码格式与 QMT 保持一致(已带 .SH/.SZ 后缀,get_realtime_quotes 内部会再 format_code 兜底)
            codes = [p.get("code", "") for p in positions if p.get("code")]
            if not codes:
                return
            quotes = self.qmt.get_realtime_quotes(codes)
            if not quotes:
                return
            updated = self.store.refresh_quotes(quotes)
            if updated > 0:
                logger.debug(f"持仓行情刷新: {updated} 条")
        except Exception as e:
            logger.error(f"持仓行情刷新异常: {e}")
        self._last_quotes_refresh_time = time.time()

    def _run_reconcile(self, current_time: str) -> None:
        """对账(4 时点)"""
        for rt in self.config.reconcile_times:
            task_key = f"reconcile_{rt}"
            if current_time >= rt and task_key not in self._executed_today:
                if self.reconciler:
                    try:
                        result = self.reconciler.reconcile()
                        logger.info(f"对账({rt}): {result.get('summary', 'done')}")
                    except Exception as e:
                        logger.error(f"对账异常({rt}): {e}")
                self._executed_today.add(task_key)

    def _run_eod_archive(self) -> None:
        """EOD 归档(15:01,§6)"""
        if not self.store:
            return
        try:
            self.store.eod_archive(qmt_wrapper=self.qmt)
            logger.info("EOD 归档完成")
        except Exception as e:
            logger.error(f"EOD 归档异常: {e}")

    def _check_signal_heartbeat(self) -> None:
        """14:55 信号心跳看门狗(v1.2.2 §5.2 + §10.6)

        检查当日是否有 docker_tdx 心跳记录(历史命名,实际为 API 服务端信号):
        - 无心跳 → 告警(可能是 API 服务端选股失败或网络不通)
        - 有心跳但 scan_status=error → 告警
        - 有心跳且 status=ok → 正常
        """
        if not self.store:
            return
        try:
            hb = self.store.get_latest_heartbeat("docker_tdx")
            if hb is None:
                msg = "14:55 看门狗:当日无信号心跳(可能 API 服务端选股未执行或网络不通)"
                logger.warning(msg)
                if self.notifier:
                    self.notifier.send(f"⚠ {msg}")
                if self.audit:
                    self.audit.log("signal_heartbeat_missing", reason=msg)
            elif hb.get("scan_status") == "error":
                msg = f"14:55 看门狗:信号心跳异常 scan_status=error, count={hb.get('signal_count', 0)}"
                logger.error(msg)
                if self.notifier:
                    self.notifier.send(f"⚠ {msg}")
            elif hb.get("scan_status") in ("ok", "no_signal"):
                logger.info(f"14:55 看门狗:信号心跳正常 status={hb.get('scan_status')} count={hb.get('signal_count', 0)}")
            else:
                logger.info(f"14:55 看门狗:信号心跳 status={hb.get('scan_status')} count={hb.get('signal_count', 0)}")
        except Exception as e:
            logger.error(f"信号心跳看门狗异常: {e}")
