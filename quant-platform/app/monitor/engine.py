"""
盘中实时监控引擎 & 多维度止盈止损风控
由 QMT 实时 Tick 数据驱动，执行亚秒级风控计算。
所有决策均从 settings 动态读取，所有触发均记录审计日志。
"""
import time
import threading
from datetime import datetime, date
import math
import pandas as pd

from core.event_engine import event_engine, EVENT_TIMER, EVENT_RISK, EVENT_TRADE, EVENT_TICK
from core.logger import get_logger, get_audit_logger
from core.settings import settings
from database.duckdb_manager import db
from app.data_manager.engine import get_realtime_quote, get_index_realtime

log = get_logger("Monitor")
audit = get_audit_logger("RiskControl")


class MonitorEngine:
    """
    事件驱动型盘中实时监控引擎。
    订阅 EVENT_TICK 事件（由 QMT 提供），实现亚秒级风控响应。
    
    风控决策顺序：
      1. 分阶止盈：达到阶梯利润则卖出对应比例
      2. 移动止盈（高位回撤）：当利润 ≥ trailing_activate_pct，记录历史最高价，
         回撤超过 trailing_drawdown_pct 时清仓
      3. 利润保护（保本）：当最高利润达标后，硬止损上移，确立不亏损逻辑
      4. 硬止损：跌破 hard_stop_loss_pct 时清仓
      5. 时间止损：持有第 7 天或更久，且当日收益达标，清仓离场
    """

    def __init__(self, order_callback=None):
        self._running = False
        self._order_callback = order_callback
        self._staged_sold: dict = {}  # {position_id: set of staged levels triggered}

        # 核心：订阅实时行情事件，取代定时器轮询
        event_engine.register(EVENT_TICK, self._on_tick)

    def start(self):
        self._running = True
        log.info("实时监控引擎启动，监控窗口：09:25 - 15:00")

    def stop(self):
        self._running = False
        log.info("实时监控引擎已停止")

    def _on_tick(self, event):
        """接收实时行情 Tick (QMT 链路) 进行毫秒级分析"""
        if not self._running:
            return
            
        now = datetime.now()
        # 仅在交易时间内执行（9:25-15:00，避开 9:15-9:25 集合竞价假单影响）
        if not (9 * 60 + 25 <= now.hour * 60 + now.minute <= 15 * 60):
            return

        tick_data = event.data # Expected: {code: ..., price: ...}
        if not tick_data or 'code' not in tick_data:
            return
            
        self.run_risk_check_single(tick_data)

    def run_risk_check_single(self, tick):
        """针对单个标的的实时风控检测"""
        code = tick['code']
        # 只检查有持仓的
        positions = db.get_open_positions()
        if positions.empty:
            return
            
        pos = positions[positions["code"] == code]
        if pos.empty:
            return
            
        try:
            self._check_position(pos.iloc[0], tick['price'])
        except Exception as e:
            log.error(f"实时风控检查异常 [{code}]: {e}")

    def run_risk_check(self):
        """全量扫描（备用或初始化用）"""
        positions = db.get_open_positions()
        if positions.empty:
            return

        codes = positions["code"].tolist()
        quotes = get_realtime_quote(codes)
        for _, pos in positions.iterrows():
            q = quotes[quotes['code'] == pos['code']]
            if not q.empty:
                self._check_position(pos, float(q.iloc[0]['price']))

    def _check_position(self, pos, current_price: float):
        """单只持仓的风控核心逻辑"""
        from core.redis_manager import redis_client
        
        code = pos["code"]
        pos_name = pos["name"] or code
        pos_id = int(pos["id"])
        open_price = float(pos["open_price"])
        remain_vol = int(pos["remain_volume"])
        open_time = pd.to_datetime(pos["open_time"])
        
        # --- Redis 缓存接入点 ---
        # 优先从 Redis 获取缓存的最高价和激活状态，减少对 DuckDB 的读写次数
        redis_key = f"pos:highest:{pos_id}"
        cached_data = redis_client.hgetall(redis_key) if redis_client else {}
        
        if cached_data:
            highest = float(cached_data.get("price", pos["highest_price"]))
            trailing_activated = cached_data.get("activated") == "1"
        else:
            highest = float(pos["highest_price"])
            trailing_activated = bool(pos["trailing_activated"])
        
        # 计算开仓至今的自然天数（含周末）
        hold_days = (datetime.now().date() - open_time.date()).days

        # 获取当前收益率
        current_price = float(current_price)
        profit_pct = (current_price - open_price) / open_price * 100

        # 更新最高价逻辑：优先写 Redis
        new_highest = max(highest, current_price)
        triggered_activation = (profit_pct >= settings.trailing_activate_pct and not trailing_activated)
        
        if new_highest != highest or triggered_activation:
            activated = trailing_activated or triggered_activation
            if redis_client:
                # 盘中仅写入 Redis，由 TaskWorker 盘后统一同步回 DuckDB
                redis_client.hset(redis_key, mapping={
                    "price": str(new_highest),
                    "activated": "1" if activated else "0",
                    "code": code,
                    "update_time": datetime.now().isoformat()
                })
                # 设置 24 小时过期，防止内存积压
                redis_client.expire(redis_key, 86400)
            else:
                # 降级方案：若 Redis 挂了，回退到直接写 DB（虽有锁风险但保数据）
                db.update_position_highest(pos_id, new_highest, activated)
            
            if activated and not trailing_activated:
                log.info(f"[{code}] 移动止盈已激活！当前利润 {profit_pct:.2f}%，历史最高 {new_highest:.2f}")

        # --- 风控抢单逻辑（按优先级执行）---

        # 1. 分阶止盈 (Partial Take Profit)
        staged_done = self._staged_sold.get(pos_id, set())
        for i, stage in enumerate(settings.staged_take_profit):
            if i in staged_done:
                continue
            if profit_pct >= stage["profit_pct"]:
                if stage.get("sell_all"):
                    sell_vol = remain_vol
                    reason_label = "全仓清仓"
                else:
                    # 向上取整到100的倍数 (A股卖出规则)
                    target_vol = remain_vol * float(stage.get("sell_ratio", 0))
                    sell_vol = math.ceil(target_vol / 100) * 100
                    sell_vol = min(sell_vol, remain_vol)
                    reason_label = f"卖出{stage.get('sell_ratio', 0)*100}%比例"

                if sell_vol > 0:
                    reason = (
                        f"[策略卖出] 分阶止盈第{i+1}档: "
                        f"利润达{profit_pct:.2f}%(≥{stage['profit_pct']}%), "
                        f"{reason_label}={sell_vol}股"
                    )
                    self._send_sell(pos_id, code, pos_name, current_price, sell_vol, reason)
                    if pos_id not in self._staged_sold:
                        self._staged_sold[pos_id] = set()
                    self._staged_sold[pos_id].add(i)
                return

        # 2. 移动止盈（高位回撤保护）
        if trailing_activated and new_highest > 0:
            drawdown_pct = (new_highest - current_price) / new_highest * 100
            if drawdown_pct >= settings.trailing_drawdown_pct:
                reason = (
                    f"[策略卖出] 移动止盈清仓: "
                    f"波段最高利润曾达{((new_highest-open_price)/open_price*100):.2f}%, "
                    f"当前从高位{new_highest:.2f}回落{drawdown_pct:.2f}%(≥{settings.trailing_drawdown_pct}%), "
                    f"触发移动止盈全仓"
                )
                self._send_sell(pos_id, code, pos_name, current_price, remain_vol, reason)
                return

        # 3. 利润保护 (Profit Shield / Breakeven)
        # 当最高利润触及阈值后，硬止损线自动上移，确保保本
        max_profit_pct = (new_highest - open_price) / open_price * 100
        active_hard_sl = settings.hard_stop_loss_pct
        if max_profit_pct >= settings.breakeven_threshold_pct:
            active_hard_sl = settings.breakeven_stop_pnl_pct
            # log.debug(f"[{code}] 利润保护生效中，当前止损位已调至 {active_hard_sl}%")

        # 4. 硬止损 (含动态保本上移)
        if profit_pct <= active_hard_sl:
            is_breakeven = active_hard_sl > settings.hard_stop_loss_pct
            reason_type = "利润保护止盈" if is_breakeven else "硬止损"
            reason = (
                f"[策略卖出] {reason_type}: "
                f"当前利润{profit_pct:.2f}%(触及{active_hard_sl}%阈值), "
                f"触发全仓{'离场' if is_breakeven else '止损'}"
            )
            self._send_sell(pos_id, code, pos_name, current_price, remain_vol, reason)
            return

        # 5. 强制时间到期（无条件离场，与回测仿真逻辑一致）
        if hold_days >= settings.time_exit_days:
            reason = (
                f"[策略卖出] 时间止盈(到期): "
                f"已持仓{hold_days}天(>= {settings.time_exit_days}天), "
                f"当前利润{profit_pct:.2f}%, 强制全仓离场"
            )
            self._send_sell(pos_id, code, pos_name, current_price, remain_vol, reason)

    def _send_sell(self, position_id, code, name, price, volume, reason):
        """触发卖出指令并记录审计日志"""
        audit.warning(f"SELL | {code} {name} | 价格={price:.2f} | 数量={volume} | {reason}")
        db.record_trade(
            position_id=position_id,
            code=code,
            name=name,
            direction="SELL",
            price=price,
            volume=volume,
            trade_type="strategy",
            reason=reason,
        )
        db.reduce_position(position_id, volume)
        event_engine.emit(EVENT_RISK, {
            "code": code, "price": price, "volume": volume, "reason": reason
        })
        # 调用实际下单接口
        if self._order_callback:
            self._order_callback(code=code, volume=volume, reason=reason, trade_type="strategy")


# 全局实例
monitor_engine = MonitorEngine()
