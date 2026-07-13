import sys
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from core.settings import settings
from core.logger import get_logger
from core.sync_logger import info as sync_info, ok as sync_ok, warn as sync_warn, error as sync_error
from app.data_manager.tushare_sync import tushare_sync_manager
from app.data_manager.parquet_pipeline import parquet_pipeline

log = get_logger("CronScheduler")

class DataPipelineScheduler:
    def __init__(self):
        self._scheduler = AsyncIOScheduler()
        self._job_id = "daily_tushare_sync"
        
    def start(self):
        if not self._scheduler.running:
            self._scheduler.start()
            self._schedule_jobs()
            self._init_monitor()
            log.info("CronScheduler | 自动调度服务(APScheduler) 已启动。")

    def _init_monitor(self):
        """根据配置启停盘中监控"""
        from app.api.sim_trader import get_engine
        engine = get_engine()
        # 从 config 重新读取（不能用 import 捕获的旧值）
        import app.sim_trader.config as _sc2
        if _sc2.MONITOR_ENABLED and engine.monitor:
            engine.monitor.mode = _sc2.MONITOR_MODE
            engine.monitor.start()
            log.info(f"CronScheduler | 盘中监控已自动启动，模式={_sc2.MONITOR_MODE}")
        else:
            log.info(f"CronScheduler | 盘中监控未自动启动（MONITOR_ENABLED={_sc2.MONITOR_ENABLED}）")

    def stop(self):
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            log.info("CronScheduler | 自动调度服务已关闭。")

    def _schedule_jobs(self):
        """根据当前的配置时间安排任务"""
        # 如果已经存在任务，先移除，支持平滑修改时间
        for job in self._scheduler.get_jobs():
            if job.id.startswith(self._job_id):
                self._scheduler.remove_job(job.id)
            
        if not settings.cron_enabled:
            log.info("CronScheduler | 数据自动同步暂未在配置中开启。")
            return
            
        sync_times = settings.cron_sync_times # e.g. ["08:30", "15:45"]
        for idx, sync_time in enumerate(sync_times):
            try:
                hour, minute = sync_time.split(":")
                self._scheduler.add_job(
                    self.run_daily_pipeline,
                    'cron',
                    day_of_week='mon-fri',  # 仅工作日
                    hour=int(hour),
                    minute=int(minute),
                    id=f"{self._job_id}_{idx}",
                    replace_existing=True
                )
                log.info(f"CronScheduler | 每日数据收割机被安排在每个工作日的 {sync_time} 执行。")
            except Exception as e:
                log.error(f"CronScheduler | cron_sync_times 格式错误 ({sync_time}): {e}")

        # Phase 6.4: Redis→DuckDB 持久化收割机
        # 每天收盘后 15:30 执行，把盘中 Redis 缓存的最高价同步回 DuckDB
        self._scheduler.add_job(
            self.redis_harvest_to_duckdb,
            'cron',
            day_of_week='mon-fri',
            hour=15,
            minute=30,
            id="redis_harvest_job",
            replace_existing=True
        )
        log.info("CronScheduler | Redis→DuckDB 收割机已安排在每个工作日 15:30 执行。")

        # ── 模拟盘尾盘交易 ──────────────────────────
        self._scheduler.add_job(
            self.run_sim_trader_daily,
            'cron',
            day_of_week='mon-fri',
            hour=14,
            minute=52,
            id="sim_trader_daily",
            replace_existing=True
        )
        log.info("CronScheduler | 模拟盘尾盘交易已安排在 14:52 执行。")

        # ── 盘中监控全量扫描兜底（无 QMT 时） ──
        self._scheduler.add_job(
            self.monitor_full_scan,
            'cron',
            day_of_week='mon-fri',
            hour='9-14',
            minute='*/1',
            id="monitor_full_scan",
            replace_existing=True
        )
        log.info("CronScheduler | 盘中监控全量扫描已安排（每分钟，兜底）。")

        # ── 热点板块调度 ────────────────────────────────────────
        if settings.hot_sector_enabled:
            # 每日开市前同步概念映射
            self._scheduler.add_job(
                self.sync_concepts_daily,
                'cron',
                day_of_week='mon-fri',
                hour=9,
                minute=0,
                id="concept_daily_sync",
                replace_existing=True
            )
            log.info("CronScheduler | 概念数据同步已安排在每日 09:00 执行。")

            # 盘中每 10 分钟重算热度
            refresh_min = settings.hot_sector_refresh_minutes
            self._scheduler.add_job(
                self.recalc_hotness,
                'cron',
                day_of_week='mon-fri',
                hour='9-14',
                minute=f'*/{refresh_min}',
                id="hotness_recalc",
                replace_existing=True
            )
            log.info(f"CronScheduler | 板块热度重算已安排在每个交易日 {refresh_min} 分钟/次。")

            # 收盘终版快照
            self._scheduler.add_job(
                self.finalize_hotness,
                'cron',
                day_of_week='mon-fri',
                hour=15,
                minute=30,
                id="hotness_finalize",
                replace_existing=True
            )
            log.info("CronScheduler | 收盘热度终版快照已安排在 15:30 执行。")

        # ── 指数日线自动更新（盘后） ──
        self._scheduler.add_job(
            self.sync_index_daily,
            'cron',
            day_of_week='mon-fri',
            hour=15,
            minute=35,
            id="index_daily_sync",
            replace_existing=True
        )
        log.info("CronScheduler | 指数日线更新已安排在 15:35 执行。")

        # ── 启动时补执行：如果已是交易日且过了14:52，立即跑一次 ──
        # 用「最后执行日期」而非布尔标志，避免长驻进程下次日起永久跳过
        self._daily_ran_date = None
        self._pipeline_last_run = None  # 数据-C4: (日期, 时段) 幂等键
        loop = asyncio.get_event_loop()
        loop.create_task(self._catch_up_daily())

    async def _catch_up_daily(self):
        """启动时检查：如果当前是交易日且已过14:52，补执行尾盘交易

        注意：run_sim_trader_daily() 含大量同步IO，即使部分已用
        run_in_executor，整体仍可能耗时较长导致启动后服务卡顿。
        因此将延迟从1秒改为30秒，给服务更多初始化时间。
        """
        import asyncio as _asyncio
        await _asyncio.sleep(30)  # 30秒：等服务完全就绪后再补执行
        try:
            from datetime import datetime, date
            now = datetime.now()
            today = date.today()
            if now.hour < 14 or (now.hour == 14 and now.minute < 52):
                return  # 还没到14:52，等cron正常触发

            from app.api.sim_trader import get_trading_dates
            trading_dates = get_trading_dates()
            if today not in trading_dates:
                return  # 非交易日

            log.info(f"CronScheduler | [启动补执行] 已过14:52，立即执行尾盘交易: {today}")
            await self.run_sim_trader_daily()
        except Exception as e:
            log.warning(f"CronScheduler | [启动补执行] 失败: {e}")

    def reload_config(self):
        """配置被修改后重新挂载任务"""
        self._schedule_jobs()

    async def monitor_full_scan(self):
        """盘中监控全量扫描（无 QMT tick 时的兜底方案）

        run_full_scan() 含 HTTP 行情请求，是同步阻塞操作，
        必须在线程池中执行，否则会卡死事件循环导致所有 API 无响应。
        """
        try:
            from app.api.sim_trader import get_engine
            engine = get_engine()
            if engine.monitor_enabled:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, engine.monitor.run_full_scan)
        except Exception as e:
            log.warning(f"CronScheduler | [监控扫描] 异常: {e}")

    async def sync_index_daily(self):
        """盘后自动更新指数日线数据（Tushare优先，与个股数据源一致）"""
        try:
            from app.data_manager.tushare_sync import tushare_sync_manager
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, tushare_sync_manager.sync_index_daily)
            if ok:
                log.info("CronScheduler | [指数更新] Tushare 指数日线同步完成")
                return
        except Exception as e:
            log.warning(f"CronScheduler | [指数更新] Tushare失败: {e}，尝试QMT")
        try:
            from app.data_manager.qmt_index_sync import sync_index_daily_qmt
            if sync_index_daily_qmt():
                log.info("CronScheduler | [指数更新] QMT dispatch 成功")
                return
        except Exception as e:
            log.warning(f"CronScheduler | [指数更新] QMT失败: {e}，回退akshare")
        try:
            from app.data_manager.index_updater import update_all_indices
            result = update_all_indices()
            added = sum(result.values())
            if added > 0:
                log.info(f"CronScheduler | [指数更新] 更新了 {len(result)} 个指数，共 {added} 条")
        except Exception as e:
            log.warning(f"CronScheduler | [指数更新] 异常: {e}")

    async def redis_harvest_to_duckdb(self):
        """
        Phase 6.4: 盘后缓存→DuckDB 持久化收割机
        将 IntradayMonitor 盘中写入 Redis/内存缓存的持仓最高价和止盈激活状态,
        统一持久化回 DuckDB, 防止重启后数据丢失.
        支持 Redis 不可用时从内存缓存收割。
        """
        log.info("CronScheduler | [缓存收割] 开始将盘中最高价缓存同步回 DuckDB...")
        try:
            from core.redis_manager import redis_manager
            from database.duckdb_manager import db

            # 扫描所有 pos:highest:* 键（Redis 优先，fallback 内存缓存）
            keys = redis_manager.cache_keys("pos:highest:*")
            if not keys:
                log.info("CronScheduler | [缓存收割] 无缓存数据，跳过。")
                return

            success_count = 0
            fail_count = 0
            for key in keys:
                try:
                    data = redis_manager.hget_all(key)
                    if not data:
                        continue
                    pos_id = int(key.split(":")[-1])
                    highest_price = float(data.get("price", 0))
                    activated = data.get("activated") == "1"

                    if highest_price > 0:
                        db.update_position_highest(pos_id, highest_price, activated)
                        # 收割成功后删除缓存键，防止次日重复加载过期价格
                        redis_manager.cache_delete(key)
                        success_count += 1
                except Exception as e:
                    fail_count += 1
                    log.error(f"CronScheduler | [缓存收割] 处理 {key} 失败: {e}")

            log.info(
                f"CronScheduler | [缓存收割] 完成！"
                f"成功同步 {success_count} 条持仓最高价，"
                f"失败 {fail_count} 条。"
            )
        except Exception as e:
            log.error(f"CronScheduler | [缓存收割] 严重异常: {e}", exc_info=True)

    async def run_daily_pipeline(self, force: bool = False):
        """后台自动化清洗全部管线的主串行流。

        数据-C4 幂等: 同一天同一时段(盘前<12 / 盘后>=12)只跑一次，避免
        08:30+17:30 双时段 + 手动触发的纯重复(重复消耗 Tushare 配额)。
        force=True (手动触发) 绕过幂等。
        """
        from datetime import datetime as _dt
        now = _dt.now()
        session = "am" if now.hour < 12 else "pm"
        run_key = (now.date(), session)
        if not force and getattr(self, "_pipeline_last_run", None) == run_key:
            log.info(f"CronScheduler | [管道] {run_key} 本时段已执行过，跳过(幂等)")
            sync_info(f"=== 本时段({session})数据已同步过，跳过重复执行 ===")
            return
        self._pipeline_last_run = run_key

        log.info("CronScheduler ========= [全自动日线/面料收取管道] 起步 =========")
        sync_info("=== 全量数据洗盘开始 ===")
        loop = asyncio.get_running_loop()
        try:
            # 0. QMT 股票列表同步（新股/退市检测）
            try:
                from app.data_manager.qmt_stock_sync import qmt_stock_sync
                sync_info("[1/5] 正在同步股票列表（新股/退市检测）...")
                result = await loop.run_in_executor(None, qmt_stock_sync.sync)
                if result.get("status") == "ok":
                    msg = f"股票列表同步: 新增 {result['added']}, 退市 {result['delisted']}"
                    log.info(msg)
                    sync_ok(f"[1/5] {msg}")
                else:
                    sync_warn(f"[1/5] 股票列表同步: {result.get('message')}")
            except Exception as e:
                sync_warn(f"[1/5] QMT 股票列表同步跳过: {e}")
                log.warning(f"QMT 股票列表同步失败（非致命）: {e}")

            # 1. 基础日线和财务信息快照（在线程池中执行，避免阻塞事件循环）
            sync_info("[2/5] 正在同步股票基础信息...")
            await loop.run_in_executor(None, tushare_sync_manager.sync_stock_basic)
            sync_info("[3/5] 正在同步财务快照...")
            await loop.run_in_executor(None, tushare_sync_manager.sync_fundamentals_snapshot)
            sync_ok("[3/5] 财务快照已完成")

            # 2. 全市场 Parquet K线清洗 (默认最近7日增量或补齐)
            sync_info("[4/5] 正在拉取个股日 K 线并写入 Parquet...")
            await loop.run_in_executor(None, parquet_pipeline.sync_daily_klinesto_parquet)
            sync_ok("[4/5] 个股日 K 线已更新")

            # 3. 指数日线同步（与个股数据源一致，Tushare）
            sync_info("[5/5] 正在同步指数日线数据...")
            await loop.run_in_executor(None, tushare_sync_manager.sync_index_daily)
            sync_ok("[5/5] 指数日线已更新")

            sync_ok("=== 全量数据洗盘完成 ===")
            log.info("CronScheduler ========= [收取管道] 完美结束 =========")
        except Exception as e:
            sync_error(f"全量数据洗盘异常: {e}")
            log.error(f"CronScheduler | 后台管道同步出现严重异常: {e}", exc_info=True)

    async def trigger_manual_run(self):
        """暴露给 API，允许强制手工起步 (异步安全)。force=True 绕过幂等。"""
        if self._scheduler.running:
            await self.run_daily_pipeline(force=True)
        else:
            log.warning("CronScheduler 未在运行状态，直接触发一次管道同步")
            await self.run_daily_pipeline(force=True)

    async def trigger_redis_harvest(self):
        """暴露给 API，允许手工触发 Redis→DuckDB 收割"""
        if self._scheduler.running:
            await self.redis_harvest_to_duckdb()
        else:
            log.warning("CronScheduler 未在运行状态，无法触发 Redis 收割")

    # ─── 热点板块调度 ────────────────────────────────────────

    async def sync_concepts_daily(self):
        """每日概念数据同步"""
        log.info("CronScheduler | [概念同步] 开始同步 Tushare 概念数据...")
        try:
            from app.hot_sector.concept_sync import concept_syncer
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, concept_syncer.sync_all)
            log.info(f"CronScheduler | [概念同步] 完成: {result.get('total_concepts', 0)} 概念, "
                     f"{result.get('total_mappings', 0)} 映射")
        except Exception as e:
            log.error(f"CronScheduler | [概念同步] 失败: {e}", exc_info=True)

    async def recalc_hotness(self):
        """盘中板块热度定时重算"""
        log.info("CronScheduler | [热度计算] 开始刷新板块热度...")
        try:
            from app.hot_sector.engine import hot_sector_engine
            loop = asyncio.get_running_loop()
            summary = await loop.run_in_executor(None, hot_sector_engine.refresh_hotness)
            log.info(f"CronScheduler | [热度计算] 完成: {summary}")
        except Exception as e:
            log.error(f"CronScheduler | [热度计算] 失败: {e}", exc_info=True)

    async def finalize_hotness(self):
        """收盘终版热度快照+清缓存"""
        log.info("CronScheduler | [热度终版] 开始执行收盘快照...")
        try:
            from app.hot_sector.engine import hot_sector_engine
            loop = asyncio.get_running_loop()
            summary = await loop.run_in_executor(None, hot_sector_engine.refresh_hotness)
            log.info(f"CronScheduler | [热度终版] 完成: {summary}")
        except Exception as e:
            log.error(f"CronScheduler | [热度终版] 失败: {e}", exc_info=True)

    async def run_sim_trader_daily(self):
        """每日 14:52 执行模拟盘尾盘交易"""
        from datetime import date
        today = date.today()

        # 防重复：同一天只跑一次（按日期判断，跨日自动复位）
        if getattr(self, '_daily_ran_date', None) == today:
            log.info(f"CronScheduler | [模拟盘] {today} 已执行过，跳过")
            return
        self._daily_ran_date = today

        log.info(f"CronScheduler | [模拟盘] 14:52 开始执行尾盘交易: {today}")

        try:
            from app.api.sim_trader import get_engine, get_trading_dates
            from app.sim_trader.data_loader import (
                load_all_bars, get_daily_snapshot, generate_today_signals,
                augment_bars_with_realtime
            )
            from app.sim_trader.config import SAME_STOCK_COOLDOWN, STRATEGY_NAME
            from server.websocket.manager import sync_broadcast

            engine = get_engine()
            trading_dates = get_trading_dates()

            if today not in trading_dates:
                log.info(f"CronScheduler | [模拟盘] {today} 非交易日，跳过")
                return

            # load_all_bars / augment_bars_with_realtime 是同步重 IO，
            # 放入线程池避免阻塞事件循环（卡死所有 API 请求）
            loop = asyncio.get_running_loop()
            bars = await loop.run_in_executor(None, load_all_bars)
            bars, snapshot = await loop.run_in_executor(
                None, augment_bars_with_realtime, bars, today
            )

            # ── 选股：通过 TDX 桥接获取当前配置的公式信号 ──
            signals = []
            if engine.auto_scan:
                try:
                    from app.tqsdk.bridge import TdxBridge, _get_formula_name

                    def _do_tdx_screen():
                        formula_name = _get_formula_name()
                        bridge = TdxBridge()
                        return bridge.execute_screen(
                            end_time=today.strftime('%Y%m%d'),
                            lookback_days=500,
                            formula_name=formula_name,
                        ), formula_name

                    sig_result, formula_name = await loop.run_in_executor(None, _do_tdx_screen)
                    if sig_result.get('status') == 'ok':
                        matched = sig_result.get('matched', [])
                        log.info(f'CronScheduler | [模拟盘] {formula_name}选股: {len(matched)}只')
                        for code in matched:
                            code_num = code.split('.')[0] if '.' in code else code
                            px = snapshot.get(code_num, {}).get('close', 0)
                            if px > 0:
                                signals.append((code_num, px))
                except Exception as e:
                    log.warning(f'CronScheduler | [模拟盘] TDX选股失败: {e}')

            if not engine.auto_sell and not engine.auto_buy:
                # 全部告警：检查但不执行（但仍记录快照，防止净值曲线断档）
                sell_list = engine.check_stops(today, snapshot, trading_dates, readonly=True) if signals or engine.positions else []
                sync_broadcast({
                    'type': 'risk_alert',
                    'today': str(today),
                    'reason': f'开关关闭，应卖出{len(sell_list)}笔 应买入{len(signals)}笔（未执行）',
                    'sell_count': len(sell_list), 'buy_count': len(signals),
                })
                log.info(f"CronScheduler | [模拟盘] 全部告警（应卖{len(sell_list)}笔 应买{len(signals)}笔）")
                # 不 return，继续执行后面的 record() 保存当日净值快照

            # ── 卖出 ──
            sell_count = 0
            if engine.auto_sell:
                await loop.run_in_executor(None, engine.sell_phase, today, snapshot, trading_dates)
                sell_count = len([t for t in engine._today_trades if t.exit_date == today])
            else:
                sell_list = engine.check_stops(today, snapshot, trading_dates, readonly=True)
                sell_count = len(sell_list)
                if sell_list:
                    sync_broadcast({
                        'type': 'risk_alert',
                        'today': str(today),
                        'reason': f'卖出开关关闭，应卖出{sell_count}笔（未执行）',
                        'sell_count': sell_count,
                    })

            # ── 买入 ──
            buy_count = 0
            if engine.auto_buy and signals:
                paused = engine.pause_until is not None and today <= engine.pause_until
                if not paused:
                    max_new = int(engine.cash / engine.max_buy_amount()) + 1
                    for code, price in signals[:max_new]:
                        if any(t.code == code and (today - t.entry_date).days <= SAME_STOCK_COOLDOWN
                               for t in engine.trades):
                            continue
                        if engine.execute_buy(today, code, price, strategy_name=STRATEGY_NAME):
                            buy_count += 1
            elif signals and not engine.auto_buy:
                sync_broadcast({
                    'type': 'risk_alert',
                    'today': str(today),
                    'reason': f'买入开关关闭，应买入{len(signals)}笔（未执行）',
                    'buy_count': len(signals),
                })

            # ── 净值快照：始终执行，保证曲线不断档 ──
            await loop.run_in_executor(None, engine.record, today, snapshot)
            eq = engine.total_equity(snapshot)

            sync_broadcast({
                'type': 'sim_trader_daily',
                'today': str(today),
                'buy_count': buy_count,
                'sell_count': sell_count,
                'equity': round(eq, 2),
                'cash': round(engine.cash, 2),
                'positions': engine.position_count,
                'signals': len(signals),
            })

            log.info(f"CronScheduler | [模拟盘] 完成: 卖出{sell_count}笔 买入{buy_count}笔 "
                     f"净值{eq:,.0f} 持仓{engine.position_count}")

        except Exception as e:
            log.error(f"CronScheduler | [模拟盘] 执行失败: {e}", exc_info=True)



# 全局单例
pipeline_scheduler = DataPipelineScheduler()


if __name__ == "__main__":
    """
    Task Worker 独立启动入口
    用法: python -m app.scheduler.cron_jobs
    在多服务架构中由后台任务调用
    """
    import signal

    log.info("TaskWorker | 独立任务调度进程启动...")
    pipeline_scheduler.start()

    loop = asyncio.get_event_loop()

    def _shutdown(signum, frame):
        log.info("TaskWorker | 收到停止信号，正在优雅退出...")
        pipeline_scheduler.stop()
        loop.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        loop.run_forever()
    finally:
        log.info("TaskWorker | 已退出。")

