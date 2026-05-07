import asyncio
import time
from datetime import datetime, date
from typing import List, Dict
from core.settings import settings, calc_buy_volume
from core.logger import get_logger
from database.duckdb_manager import db

log = get_logger("AutoBuyer")

class AutoBuyerDaemon:
    def __init__(self):
        self._queue: List[Dict] = []  # {code, price, name, added_time, reason}
        self._running = False
        self._task = None
        self._sub_task = None

    def start(self):
        """启动后台自动买入守护线程"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._daemon_loop())
        self._sub_task = asyncio.create_task(self._redis_sub_loop())
        log.info("AutoBuyerDaemon | 后台自动买入守护线程已启动 (含 Redis 订阅模式)")

    def stop(self):
        """停止守护线程"""
        self._running = False
        if self._task:
            self._task.cancel()
        if self._sub_task:
            self._sub_task.cancel()
        self._task = None
        self._sub_task = None
        log.info("AutoBuyerDaemon | 后台自动买入守护线程已停止")

    async def _redis_sub_loop(self):
        """异步监听 Redis 信号频道"""
        from core.redis_manager import redis_manager
        import json
        
        client = redis_manager.get_client()
        if not client:
            log.error("AutoBuyerDaemon | Redis 客户端未连接，Pub/Sub 监听将失效")
            return

        pubsub = client.pubsub()
        channels = ["radar_signals", "strategy_signals"]
        pubsub.subscribe(*channels)
        log.info(f"AutoBuyerDaemon | 已启动 Redis 订阅，监听频道: {channels}")

        try:
            while self._running:
                # 使用 get_message 配合 sleep 避免阻塞导致的无法 cancel
                message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message:
                    try:
                        data = json.loads(message['data'])
                        # 转换信号格式并推入本地队列
                        # 期待格式: {"code": "000001.SZ", "price": 10.5, "reason": "Radar"}
                        if isinstance(data, dict):
                            self.put_signals([data], data.get('reason', 'RedisSignal'))
                    except Exception as e:
                        log.error(f"AutoBuyerDaemon | 解析 Redis 信号异常: {e}")
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            log.info("AutoBuyerDaemon | Redis 订阅协程已取消")
        except Exception as e:
            log.error(f"AutoBuyerDaemon | Redis 订阅循环崩溃: {e}")
        finally:
            pubsub.unsubscribe()
            pubsub.close()

    def put_signals(self, signals: List[Dict], strategy_name: str):
        """
        接收策略引擎扫描出的信号。
        支持两种信号格式：
          - 选股引擎格式: {code, close, name, ...}
        如果在系统设置中开启了"自动买入"，则加入延迟处理队列。
        """
        if not settings.auto_trade_enabled:
            log.info(f"AutoBuyerDaemon | 收到 {len(signals)} 个信号, 但自动交易未开启, 已丢弃。")
            return

        now = time.time()
        for sig in signals:
            code = sig.get('code')
            if not code:
                continue

            # 兼容两种信号格式：选股引擎用 close，Redis 信号用 price
            price = sig.get('close') or sig.get('price', 0)
            name = sig.get('name', code)

            # 如果价格为 0，尝试从 QMT 实时拉取
            if price <= 0:
                try:
                    from core.gateway import get_gateway
                    gw = get_gateway()
                    quotes = gw.get_realtime_quotes([code])
                    if quotes and code in quotes:
                        price = quotes[code].get('lastPrice', 0)
                except Exception:
                    pass

            if price <= 0:
                log.warning(f"AutoBuyerDaemon | {code} 无法获取实时价格，信号已跳过。")
                continue

            # 防重复检查：如果数据库中已经存在持仓且有剩余 volume，则不重复买入
            pos_df = db.get_positions(status='open')
            if not pos_df.empty and code in pos_df['code'].values:
                if pos_df[pos_df['code'] == code].iloc[0]['remain_volume'] > 0:
                    log.info(f"AutoBuyerDaemon | {code} ({name}) 当前仍有持仓，自动买手跳过加入队列")
                    continue

            # 检查队列是否已有
            if any(item['code'] == code for item in self._queue):
                continue

            self._queue.append({
                "code": code,
                "name": name,
                "price": price,
                "added_time": now,
                "reason": strategy_name
            })
            log.info(f"AutoBuyerDaemon | {code} ({name}) 已加入自动买入延迟队列，价格: {price}，触发策略: {strategy_name}")

    async def _daemon_loop(self):
        while self._running:
            try:
                now = time.time()
                delay_sec = settings.auto_trade_delay_seconds
                max_amt = settings.auto_trade_max_amount

                ready_items = [item for item in self._queue if now - item["added_time"] >= delay_sec]

                for item in ready_items:
                    self._queue.remove(item)
                    code = item['code']
                    price = float(item['price'])
                    name = item['name']
                    reason = item['reason']

                    # 再次检查当时开关是否依然开启
                    if not settings.auto_trade_enabled:
                        continue

                    # 计算买入数量
                    volume = calc_buy_volume(price, override_max_amount=max_amt)
                    if volume <= 0:
                        log.warning(
                            f"AutoBuyerDaemon | [{code}] 计划买入，"
                            f"但按照价格 {price} 与自动买入上限 {max_amt} 计算所得股数为 0，被拦截。"
                        )
                        continue

                    try:
                        # Step 1: 先落库记录意图（模拟持仓）
                        position_id = db.save_position(
                            code=code,
                            name=name,
                            open_price=price,
                            volume=volume,
                            source=reason
                        )
                        # 写入交易流水
                        db.conn.execute(
                            '''
                            INSERT INTO trades
                                (trade_time, code, name, direction, price, volume, amount, trade_type, reason, position_id)
                            VALUES (?, ?, ?, 'BUY', ?, ?, ?, 'strategy', ?, ?)
                            ''',
                            (datetime.now().isoformat(), code, name, price, volume, price * volume, reason, position_id)
                        )
                        log.info(
                            f"AutoBuyerDaemon | ✅ 持仓记录已落库: {code} ({name}), "
                            f"数量={volume}, 单价={price:.2f}"
                        )

                        # =====================================================
                        # Step 2: QMT 实盘委托接入点（预留，待 QMT 代理稳定后开启）
                        # =====================================================
                        # TODO: 开启实盘时，取消下方注释并确认宿主机 qmt_proxy_server.py 已启动
                        #
                        # from core.gateway import get_gateway
                        # gw = get_gateway()
                        # ok = gw.buy(code, price=price, volume=volume, reason=reason)
                        # if not ok:
                        #     log.error(
                        #         f"AutoBuyerDaemon | ❌ QMT 实单发送失败: {code} - "
                        #         "持仓已落库但委托未发出，请人工核查!"
                        #     )
                        # else:
                        #     log.info(f"AutoBuyerDaemon | 🚀 QMT 实单已发出: {code}")
                        # =====================================================

                    except Exception as e:
                        log.error(f"AutoBuyerDaemon | 买入落库失败 {code}: {e}")

            except Exception as e:
                log.error(f"AutoBuyerDaemon loop 异常: {e}")

            await asyncio.sleep(2)  # 轮询间隔


auto_buyer = AutoBuyerDaemon()
