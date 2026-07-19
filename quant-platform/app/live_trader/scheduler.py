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
import os
import re
import threading
import time
from datetime import date, datetime
from typing import Optional

from core.logger import get_logger
from core.settings import settings, CONFIG_FILE

from app.scheduler.safe_task import TaskRunner

logger = get_logger("live_trader.scheduler")


def _inc_minute(time_str: str) -> str:
    """将 HH:MM 时间字符串加 1 分钟，返回新的 HH:MM 字符串。"""
    h, m = int(time_str[:2]), int(time_str[3:])
    m += 1
    if m >= 60:
        h += 1
        m = 0
    return f"{h:02d}:{m:02d}"


class LiveScheduler:
    """实盘定时调度(单 asyncio Task)"""

    def __init__(self, config, store=None, qmt=None, exit_monitor=None,
                 reconciler=None, kill_switch=None, notifier=None, audit=None,
                 runtime_state=None):
        self.config = config
        self.store = store
        self.qmt = qmt
        self.exit_monitor = exit_monitor
        self.reconciler = reconciler
        self.kill_switch = kill_switch
        self.notifier = notifier
        self.audit = audit
        self.runtime_state = runtime_state  # 读 auto_buy_enabled / mode(2026-07-14 自给自足)

        self._task: Optional[asyncio.Task] = None
        self._auto_buy_task: Optional[asyncio.Task] = None  # 存引用防 GC(auto_buy 任务最长 600s)
        self._last_scan_time: float = 0.0
        self._last_asset_backup_time: float = 0.0
        self._last_quotes_refresh_time: float = 0.0
        self._today: str = ""
        self._executed_today: set = set()
        # 进程启动日期(2026-07-15):用于 _check_signal_heartbeat 区分"今日启动的服务"vs"跨日冷启动",
        # 后者心跳不存在是预期(14:55 早已过去),不该刷告警
        self._process_start_date = date.today()

        self._last_cfg_mtime: Optional[float] = None  # app_setting.json 上次 mtime, 供 _maybe_reload_settings 判变化

        # 离场扫描间隔(可运行时修改,默认从 config 读取)
        self._exit_scan_interval: float = getattr(config, 'exit_scan_interval_sec', 60.0)

        # 持仓行情刷新间隔(固定 3s,与 QMT 客户端推送频率一致)
        self._quotes_refresh_interval: float = 3.0

        # 线程安全锁(保护 _exit_scan_interval / _auto_buy_time)
        self._lock = threading.Lock()

        # auto_buy 触发时点(2026-07-14):用 getattr 兜底,兼容 MockConfig(测试用简版 mock 无新字段)
        self._auto_buy_time: str = getattr(config, 'auto_buy_time', '14:50')

        # 候选④:每个 _run_* 方法配独立 TaskRunner,统一 try/except + exc_info logging
        self._runner = {
            "exit_scan": TaskRunner("live.exit_scan"),
            "asset_backup": TaskRunner("live.asset_backup"),
            "quotes_refresh": TaskRunner("live.quotes_refresh"),
            "eod_archive": TaskRunner("live.eod_archive"),
            "signal_heartbeat": TaskRunner("live.signal_heartbeat"),
            "reconcile": TaskRunner("live.reconcile"),
            "auto_buy": TaskRunner("live.auto_buy"),
        }

    def start(self) -> None:
        """启动调度任务"""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("调度服务启动")

    def stop(self) -> None:
        """停止调度任务"""
        if self._auto_buy_task and not self._auto_buy_task.done():
            self._auto_buy_task.cancel()
            logger.info("auto_buy 任务已取消")
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("调度服务停止")

    def set_scan_interval(self, seconds: float) -> float:
        """运行时修改离场扫描间隔(前端保存后立即生效,不阻塞)"""
        seconds = max(10.0, min(300.0, float(seconds)))  # 限制 [10, 300]
        with self._lock:
            old = self._exit_scan_interval
            self._exit_scan_interval = seconds
        logger.info(f"离场扫描间隔: {old} -> {seconds}s")
        return seconds

    def get_scan_interval(self) -> float:
        """获取当前离场扫描间隔"""
        with self._lock:
            return self._exit_scan_interval

    # ── auto_buy_time 热配置 ────────────────────────────────

    def set_auto_buy_time(self, t: str) -> str:
        """运行时修改 auto_buy 触发时点。格式 HH:MM，保存后当日已触发则次日生效。"""
        t = str(t).strip()
        if not re.match(r"^[0-2]\d:[0-5]\d$", t):
            raise ValueError(f"非法时间格式: {t}，需 HH:MM（如 14:50）")
        with self._lock:
            old = self._auto_buy_time
            self._auto_buy_time = t
        logger.info(f"auto_buy_time: {old} -> {t}")
        return t

    def get_auto_buy_time(self) -> str:
        """获取当前 auto_buy 触发时点"""
        with self._lock:
            return self._auto_buy_time

    def _maybe_reload_settings(self) -> None:
        """app_setting.json 被改写则 reload(盘中改参数热生效, 免重启 live_trader 进程)。

        背景(2026-07-19 主流程审计 FAIL-B): 实盘(8001)与主API(8888)是两个独立进程,
        Settings._data 启动时缓存一次, 主API 改配置落盘后实盘无感知。此处每秒 stat mtime,
        变化则 settings.reload()。

        生效范围(口径): 仅对"使用时现读 settings.get() 的参数"生效(如止盈止损 risk params,
        exit_monitor 每次扫描现读); holder 背书的运行时可变参数(scan-interval / auto-buy-time
        / buy-ratio)须走对应 PUT 端点热更新, 改文件本身不会热生效。
        """
        try:
            mtime = os.path.getmtime(str(CONFIG_FILE))
        except OSError:
            return
        prev = self._last_cfg_mtime
        self._last_cfg_mtime = mtime
        if prev is None or mtime == prev:
            return  # 首次 tick(记录基准) 或无变化, 不 reload
        logger.info(f"检测到 app_setting.json 变化(mtime {prev} -> {mtime}), reload settings")
        settings.reload()

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
        # 配置热加载(每秒检查 mtime, 变化则 reload): 止损止盈等参数盘中改了立即生效
        self._maybe_reload_settings()
        now = datetime.now()
        today_key = now.date().isoformat()
        current_time = now.strftime("%H:%M")

        # 新的一天,重置已执行记录
        if today_key != self._today:
            self._executed_today.clear()
            self._today = today_key
            logger.info(f"调度:新交易日 {today_key}")

        # ===== 非交易日检测(§17.3)=====
        # kill switch 激活不改 return——daily_summary/cleanup 在非交易日也照常运行
        if now.weekday() >= 5:  # 周六=5, 周日=6
            self._handle_non_trading_day()
            # 不 return:周末也要发每日概览和清理通知

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

        # ===== 实盘自给自足尾盘选股(14:50,2026-07-14)=====
        # 替代被动 /live/buy-signal:scheduler 自己选股+下单。
        # 先 _executed_today.add 占位防 1s 内重入(真钱:宁漏买不重买)。
        # auto_buy_enabled 关闭时 _run_auto_buy_scan 内部 silent return,这里照常触发无副作用。
        if (self.get_auto_buy_time() <= current_time < self.config.buy_signal_cutoff
                and "auto_buy_screen" not in self._executed_today):
            self._executed_today.add("auto_buy_screen")  # 先占位防 1s 内重入(真钱:宁漏买不重买)
            # 存引用防 GC(任务最长 600s;与 _loop 的 self._task 同款约定)
            self._auto_buy_task = asyncio.create_task(
                self._runner["auto_buy"].run_async(self._run_auto_buy_scan)
            )

        # ===== 信号心跳看门狗(14:55, v1.2.2 §5.2) =====
        if current_time >= "14:55" and "signal_heartbeat_check" not in self._executed_today:
            self._check_signal_heartbeat()
            self._executed_today.add("signal_heartbeat_check")

        # ===== 每日账户概览(15:30,不受 weekday 影响,周末也发)=====
        # 在交易日和非交易日都发，方便周末确认账户状态。
        if self.config.daily_summary_enabled:
            summary_time = self.config.daily_summary_time  # "15:30"
            # 窗口扩到 5 分钟(15:30~15:35)，防 tick 延迟漏触
            if (current_time >= summary_time
                    and current_time < "15:35"
                    and "daily_summary" not in self._executed_today):
                self._run_daily_summary()
                self._executed_today.add("daily_summary")

        # ===== 通知历史清理(15:35,不受 weekday 影响)=====
        if current_time >= "15:35" and current_time < "15:36" \
                and "cleanup_notif" not in self._executed_today:
            self._cleanup_notifications()
            self._executed_today.add("cleanup_notif")

    # ===== 各调度任务 =====

    def _handle_non_trading_day(self) -> None:
        """非交易日:自动激活 kill switch"""
        if "non_trading_check" not in self._executed_today:
            if self.kill_switch and not self.kill_switch.is_active():
                # 主开关禁用时 activate 为空操作(返回 False),此时不发"已激活"日志/通知,
                # 防每个周末幽灵告警。
                if self.kill_switch.activate(
                    reason="非交易日自动激活(周末/节假日)",
                    source="scheduler"
                ):
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
        # 候选④:try/except + log 委托给 TaskRunner(避免重复样板)
        actions = self._runner["exit_scan"].run_sync(self.exit_monitor.scan_once)
        if actions:
            logger.info(f"离场扫描:执行 {len(actions)} 笔卖出")
        self._last_scan_time = time.time()

    def _run_asset_backup(self) -> None:
        """资产备份(5 分钟间隔,闸门5a 基准 + EOD 备份)"""
        if time.time() - self._last_asset_backup_time < 300:
            return
        if not self.qmt or not self.qmt.connected:
            return
        if not self.store:
            return
        # 候选④:委托 TaskRunner
        self._runner["asset_backup"].run_sync(
            lambda: self.store.backup_asset(self.qmt.query_asset())
        )
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
        # 候选④:内部 lambda 含前置守卫 + QMT 拉取 + 持久化,委托 TaskRunner 统一异常处理
        def _do_refresh():
            positions = self.store.get_positions()
            if not positions:
                return
            codes = [p.get("code", "") for p in positions if p.get("code")]
            if not codes:
                return
            quotes = self.qmt.get_realtime_quotes(codes)
            if not quotes:
                return
            updated = self.store.refresh_quotes(quotes)
            if updated > 0:
                logger.debug(f"持仓行情刷新: {updated} 条")
        self._runner["quotes_refresh"].run_sync(_do_refresh)
        self._last_quotes_refresh_time = time.time()

    def _run_reconcile(self, current_time: str) -> None:
        """对账(4 时点)"""
        for rt in self.config.reconcile_times:
            task_key = f"reconcile_{rt}"
            if current_time >= rt and task_key not in self._executed_today:
                if self.reconciler:
                    # 候选④:委托 TaskRunner
                    result = self._runner["reconcile"].run_sync(self.reconciler.reconcile)
                    if result:
                        logger.info(f"对账({rt}): {result.get('summary', 'done')}")
                self._executed_today.add(task_key)

    def _run_eod_archive(self) -> None:
        """EOD 归档(15:01,§6)"""
        if not self.store:
            return
        # 候选④:委托 TaskRunner
        self._runner["eod_archive"].run_sync(lambda: self.store.eod_archive(qmt_wrapper=self.qmt))
        logger.info("EOD 归档完成")

    def _check_signal_heartbeat(self) -> None:
        """14:55 信号心跳看门狗(v1.2.2 §5.2 + §10.6)

        检查当日是否有 docker_tdx 心跳记录(历史命名,实际为 API 服务端信号):
        - 无心跳 → 告警(可能是 API 服务端选股失败或网络不通)
        - 有心跳但 scan_status=error → 告警
        - 有心跳且 status=ok → 正常

        冷启动保护(2026-07-15):若进程启动日期 != 今日(跨日重启),14:55 早已过去,
        当日无心跳是预期,跳过告警只 info。否则 23:28 重启会刷假阳性 WARNING。
        """
        if not self.store:
            return
        # 冷启动保护:进程启动日期不是今日 → 14:55 已过,心跳不存在是预期
        if self._process_start_date != date.today():
            logger.info(
                f"14:55 看门狗:进程启动日期 {self._process_start_date} != 今日 "
                f"{date.today()}(跨日冷启动),心跳不存在属预期,跳过检查"
            )
            return
        # 候选④:心跳读 + 分支告警委托 TaskRunner(异常吞掉,不阻塞主循环)
        def _do_check():
            hb = self.store.get_latest_heartbeat("docker_tdx")
            if hb is None:
                if self.runtime_state and self.runtime_state.auto_buy_enabled:
                    # auto_buy 已启用却无心跳 = 该跑没跑(选股未执行/异常/未到点),需告警
                    msg = "14:55 看门狗:auto_buy 已启用但当日无信号心跳(选股可能未执行或失败)"
                    logger.warning(msg)
                    if self.notifier:
                        self.notifier.send(f"⚠ {msg}")
                    if self.audit:
                        self.audit.log("signal_heartbeat_missing", reason=msg)
                else:
                    # auto_buy 未启用:实盘无主动交易,无信号属预期,只 info 不刷飞书
                    logger.info("14:55 看门狗:当日无信号心跳(auto_buy 未启用,属预期)")
            elif hb.get("scan_status") == "error":
                msg = f"14:55 看门狗:信号心跳异常 scan_status=error, count={hb.get('signal_count', 0)}"
                logger.error(msg)
                if self.notifier:
                    self.notifier.send(f"⚠ {msg}")
            elif hb.get("scan_status") in ("ok", "no_signal"):
                logger.info(f"14:55 看门狗:信号心跳正常 status={hb.get('scan_status')} count={hb.get('signal_count', 0)}")
            else:
                logger.info(f"14:55 看门狗:信号心跳 status={hb.get('scan_status')} count={hb.get('signal_count', 0)}")
        self._runner["signal_heartbeat"].run_sync(_do_check)

    async def _run_auto_buy_scan(self) -> None:
        """实盘自给自足选股→下单(14:50 触发,阻塞选股丢线程池)。

        替代被动 /live/buy-signal:scheduler 自己调 TdxBridge 选股 + QMT 配价,
        再调 main.process_buy_signals 下单(复用 kill_switch/cutoff/去重/并发/心跳/幂等)。

        安全闸门(前置):
          - runtime_state.auto_buy_enabled 为 False → silent return(默认关闭,真钱安全)
          - kill_switch 激活 → 告警 + return
        阻塞:execute_screen 是 subprocess(最长 600s),必须 asyncio.to_thread,
              绝不在事件循环线程同步调(否则卡死离场扫描/行情/对账)。
        失败不抛(被 run_async 吞),仅日志 + 告警,不影响其他调度任务。
        """
        # 1. 前置闸门
        if not self.runtime_state or not self.runtime_state.auto_buy_enabled:
            logger.info("auto_buy 未启用,跳过自给自足选股")
            return
        if self.kill_switch and self.kill_switch.is_active():
            logger.warning("auto_buy: kill_switch 激活,跳过")
            if self.notifier:
                try:
                    self.notifier.send("⚠ auto_buy 跳过:kill switch 激活")
                except Exception:
                    pass
            return

        # 2. 阻塞选股丢线程池(绝不阻塞事件循环)
        from .signal_picker import SignalPicker
        from .services.signal_service import process_buy_signals
        from datetime import date as _date

        picker = SignalPicker(self.qmt, self.config)
        end_time = _date.today().strftime("%Y%m%d")
        try:
            signals, formula_name, meta = await asyncio.to_thread(
                picker.screen_and_price, end_time,
                self.config.auto_buy_lookback_days, None,
            )
        except Exception as e:
            logger.exception("auto_buy 选股失败")
            if self.notifier:
                try:
                    self.notifier.send(f"⚠ auto_buy 选股失败: {e}")
                except Exception:
                    pass
            return

        # 3. 0 命中:写 no_signal 心跳(看门狗读 no_signal 不告警)+ 飞书通知
        if not signals:
            msg = (f"auto_buy 选股 0 命中(formula={formula_name}, "
                   f"matched={meta.get('matched_count', 0)}, "
                   f"skipped={len(meta.get('skipped', []))})")
            logger.info(msg)
            if self.store:
                try:
                    self.store.record_heartbeat("docker_tdx", 0, "no_signal")
                except Exception:
                    pass
            if self.notifier:
                try:
                    self.notifier.send(msg)
                except Exception:
                    pass
            return

        # 4. 调共享函数(复用 kill_switch/cutoff/去重/并发/心跳/幂等)
        try:
            result = await process_buy_signals(
                signals, strategy=formula_name or "QUANTQQ", source="TDX",
            )
        except Exception as e:
            logger.exception("auto_buy 下单流程异常")
            if self.notifier:
                try:
                    self.notifier.send(f"⚠ auto_buy 下单流程异常: {e}")
                except Exception:
                    pass
            return

        logger.info(
            f"auto_buy 完成: accepted={result.accepted} rejected={result.rejected}"
        )
        # 全拒 → 告警(成功不刷屏,成交通知由 callback_handler.order_traded 发)
        if not result.accepted:
            if self.notifier:
                try:
                    reasons = "; ".join({d.reason for d in result.details if not d.ok})[:200]
                    self.notifier.send(
                        f"⚠ auto_buy 全部被拒: {result.rejected} | {reasons}"
                    )
                except Exception:
                    pass

    # ===== 每日账户概览(v6.0 Phase 2) =====

    def _run_daily_summary(self) -> None:
        """15:30 发送每日账户概览到飞书(不受 weekday 影响)"""
        if not self.notifier:
            return
        if not self.qmt or not self.qmt.connected:
            logger.warning("daily_summary 跳过:QMT 未连接")
            return

        def _do_summary():
            try:
                # 总资产
                asset_data = self.qmt.query_asset() if self.qmt else {}
            except Exception as e:
                logger.warning(f"daily_summary: query_asset 失败: {e}")
                asset_data = {}

            try:
                # 持仓(从 store,含市值/浮盈)
                positions = self.store.get_positions() if self.store else []
            except Exception as e:
                logger.warning(f"daily_summary: get_positions 失败: {e}")
                positions = []

            # 当日盈亏:用 equity_curve 或从资产备份算
            today_pnl = 0.0
            try:
                if self.store:
                    today_asset = asset_data.get("total_asset", 0)
                    open_asset = self.store.get_open_asset()
                    if open_asset and today_asset:
                        today_pnl = today_asset - open_asset
            except Exception:
                pass

            # 今日成交笔数
            try:
                from .notifications import calc_today_deal_count
                deal_count = calc_today_deal_count(self.store) if self.store else 0
            except Exception:
                deal_count = 0

            self.notifier.daily_summary(asset_data, positions, today_pnl, deal_count)
            logger.info(f"daily_summary 发送完成: total={asset_data.get('total_asset', 0):.0f} pnl={today_pnl:+.0f}")

        # 委托 TaskRunner 执行,异常不阻塞
        if "daily_summary" not in self._runner:
            self._runner["daily_summary"] = TaskRunner("live.daily_summary")
        self._runner["daily_summary"].run_sync(_do_summary)

    def _cleanup_notifications(self) -> None:
        """15:35 清理 7 天前通知历史（复用 _state 中的 notif_store）"""
        try:
            from ._state import state as _state
            notif_store = _state.get("notif_store")
            if not notif_store:
                logger.warning("_cleanup_notifications: notif_store 未初始化")
                return
            deleted = notif_store.cleanup(retention_days=7)
            logger.info(f"通知历史清理: 删除 {deleted} 条")
        except Exception as e:
            logger.warning(f"_cleanup_notifications 失败: {e}")
