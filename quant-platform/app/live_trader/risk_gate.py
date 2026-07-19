"""RiskGate 10 闸门事前风控(v1.2.2 §5.2)

事前检查(不是事后),任意一闸失败即拒绝本次下单 + audit。
闸门 2/3/4/10 只对 buy(止损卖出不应被误拒)。
闸门 10:同股冷却 20 天(buy-only,查 live_deals 最近卖出记录)。
C1:闸门 3/4 含在途买入预扣,防异步撮合超买。
H1:闸门5a fail-safe 仅限"无资产数据";基准缺失走兜底链(今日快照→昨收→本金)。
H4:闸门7 5分钟时间窗 + risk/broker 分类。
H6:闸门9 T+1 can_use_volume。
2026-07-16:闸门5a 基准缺失兜底(今日无快照时 QMT 开盘未连 → 不再全天误禁买)。
2026-07-18:闸门5c 涨停封板拒买(buy-only,缺价 fail-safe 拒买,不计入连续拒绝计数)。
"""
import time
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from core.logger import get_logger

from app.utils.limit_up import _is_valid_price, is_limit_up

from .config import LiveTraderConfig
from .schemas import OrderIntent

logger = get_logger("live_trader.risk_gate")


class RiskGate:
    """9 闸门事前风控"""

    def __init__(self, config: LiveTraderConfig, store=None, kill_switch=None,
                 qmt_wrapper=None):
        self.config = config
        self.store = store
        self.kill_switch = kill_switch
        self.qmt = qmt_wrapper

        # 闸门7 连续拒绝计数(H4:5分钟时间窗)
        self._rejections: List[Tuple[float, str]] = []  # (timestamp, category)
        self._rejection_lock = __import__("threading").Lock()

    def check(self, intent: OrderIntent, asset: Optional[Dict] = None,
              positions: Optional[List[Dict]] = None,
              quote: Optional[Dict] = None) -> Tuple[bool, List[Dict], str]:
        """事前 9 闸门检查

        Returns:
            (passed, gate_statuses, reject_reason)
        """
        gates: List[Dict] = []
        is_buy = (intent.direction == "buy")

        # 闸门 8:kill switch(最先检查)
        if self.kill_switch and self.kill_switch.is_active():
            gates.append(self._gate(8, "kill switch", False, "激活时全拒", "已激活"))
            self._record_rejection("risk")
            return (False, gates, "kill switch 已激活")
        gates.append(self._gate(8, "kill switch", True, "激活时全拒", "未激活"))

        # 闸门 6:启动自检(简化:检查 qmt 连接)
        if not (self.qmt and self.qmt.connected):
            gates.append(self._gate(6, "启动自检", False, "QMT连接+参数+DB", "QMT未连接"))
            self._record_rejection("risk")
            return (False, gates, "QMT 未连接")
        gates.append(self._gate(6, "启动自检", True, "QMT连接+参数+DB", "通过"))

        # 闸门 10:同股冷却(buy-only,在闸门1之前检查,§10.5)
        if is_buy:
            cooldown_ok, cooldown_reason = self._check_same_stock_cooldown(intent.code)
            if not cooldown_ok:
                gates.append(self._gate(10, "同股冷却20天", False, "20天内无同股交易", cooldown_reason))
                self._record_rejection("risk")
                return (False, gates, cooldown_reason)
            gates.append(self._gate(10, "同股冷却20天", True, "20天内无同股交易", "OK"))

        # 闸门 1:单笔金额(buy+sell)
        single_amt = intent.volume * intent.price if intent.price > 0 else intent.volume * 100  # 估算
        max_single = self.config.live_capital * self.config.max_single_trade_pct
        if single_amt > max_single:
            gates.append(self._gate(1, "单笔金额", False, f"≤{max_single:.0f}", f"{single_amt:.0f}"))
            self._record_rejection("risk")
            return (False, gates, f"单笔金额 {single_amt:.0f} > {max_single:.0f}")
        gates.append(self._gate(1, "单笔金额", True, f"≤{max_single:.0f}", f"{single_amt:.0f}"))

        if is_buy:
            # 闸门 2:现金保留(仅 buy)
            cash = asset.get("cash", 0) if asset else 0
            reserve = self.config.live_capital * self.config.cash_reserve_pct
            if cash - single_amt < reserve:
                gates.append(self._gate(2, "现金保留", False, f"留{reserve:.0f}", f"{cash-single_amt:.0f}"))
                self._record_rejection("broker")
                return (False, gates, f"买入后现金 {cash-single_amt:.0f} < 保留 {reserve:.0f}")
            gates.append(self._gate(2, "现金保留", True, f"留{reserve:.0f}", f"{cash-single_amt:.0f}"))

            # 闸门 3:单只集中度(含在途预扣,C1)
            existing_pos = self._find_position(intent.code, positions)
            existing_value = (existing_pos.get("volume", 0) + existing_pos.get("pending_buy_volume", 0)) * (quote.get("lastPrice", 0) if quote else 0)
            new_value = existing_value + single_amt
            max_pos = self.config.live_capital * self.config.max_position_pct
            if new_value > max_pos:
                gates.append(self._gate(3, "单只集中度(含在途)", False, f"≤{max_pos:.0f}", f"{new_value:.0f}"))
                self._record_rejection("risk")
                return (False, gates, f"单只 {new_value:.0f} > {max_pos:.0f}")
            gates.append(self._gate(3, "单只集中度(含在途)", True, f"≤{max_pos:.0f}", f"{new_value:.0f}"))

            # 闸门 4:总仓位(含在途预扣,C1)
            total_managed = self._total_managed_value(positions, quote)
            new_total = total_managed + single_amt
            max_total = self.config.live_capital * self.config.max_total_position_pct
            if new_total > max_total:
                gates.append(self._gate(4, "总仓位(含在途)", False, f"≤{max_total:.0f}", f"{new_total:.0f}"))
                self._record_rejection("risk")
                return (False, gates, f"总仓 {new_total:.0f} > {max_total:.0f}")
            gates.append(self._gate(4, "总仓位(含在途)", True, f"≤{max_total:.0f}", f"{new_total:.0f}"))

            # 闸门 5a:日亏软熔断(2026-07-16:基准缺失已走兜底链,此处 fail-safe 仅限无资产数据)
            daily_loss_pct = self._calc_daily_loss_pct(asset, positions, quote)
            if daily_loss_pct is None:
                # asset 缺失(无任何资产数据),无法算日亏 → fail-safe 禁买
                gates.append(self._gate("5a", "日亏软熔断", False, f"≥-{self.config.daily_loss_halt_pct*100:.1f}%", "缺价fail-safe"))
                self._record_rejection("risk")
                return (False, gates, "无资产数据无法算日亏,fail-safe禁买")
            if daily_loss_pct <= -self.config.daily_loss_halt_pct:
                gates.append(self._gate("5a", "日亏软熔断", False, f"≥-{self.config.daily_loss_halt_pct*100:.1f}%", f"{daily_loss_pct*100:.2f}%"))
                self._record_rejection("risk")
                return (False, gates, f"日亏 {daily_loss_pct*100:.2f}% 触发熔断")
            gates.append(self._gate("5a", "日亏软熔断", True, f"≥-{self.config.daily_loss_halt_pct*100:.1f}%", f"{daily_loss_pct*100:.2f}%"))

            # 闸门 5b:单笔最大亏损
            if existing_pos and quote:
                pos_price = existing_pos.get("avg_cost", 0)
                last = quote.get("lastPrice", 0)
                if pos_price > 0 and last > 0:
                    single_loss = (last - pos_price) / pos_price
                    if single_loss <= -self.config.max_single_loss_pct:
                        gates.append(self._gate("5b", "单笔最大亏损", False, f"≥-{self.config.max_single_loss_pct*100:.1f}%", f"{single_loss*100:.2f}%"))
                        self._record_rejection("risk")
                        return (False, gates, f"单只浮亏 {single_loss*100:.2f}% 禁该只再买")
            gates.append(self._gate("5b", "单笔最大亏损", True, f"≥-{self.config.max_single_loss_pct*100:.1f}%", "OK"))

            # 闸门 5c:涨停封板拒买(故意不 _record_rejection:涨停是常态市场状态,不计入连续拒绝)
            if is_buy and self.config.limit_up_gate_enabled:
                prev_close, price = None, None
                if quote:
                    prev_close = quote.get("lastClose") or quote.get("preClose")
                    price = quote.get("lastPrice")

                if not (_is_valid_price(prev_close) and _is_valid_price(price)):
                    # 优先复用 quote 失败,尝试 quote_source 降级
                    try:
                        from app.data_manager.quote_source import get_realtime_quotes
                        df = get_realtime_quotes([intent.code])
                        if df is not None and not df.empty:
                            row = df.iloc[0]
                            prev_close = row.get("last_close")
                            price = row.get("price")
                    except Exception as e:
                        logger.warning(f"闸门5c quote_source 降级失败: {e}")

                if not (_is_valid_price(prev_close) and _is_valid_price(price)):
                    gates.append(self._gate("5c", "涨停拒买", False, "有效行情", "缺价fail-safe"))
                    return (False, gates, "涨停判断缺行情,fail-safe拒买")

                is_limit, reason = is_limit_up(intent.code, prev_close, price, strict=True)
                if is_limit:
                    gates.append(self._gate("5c", "涨停拒买", False, "未涨停", f"涨停{reason}"))
                    return (False, gates, f"涨停封板拒买: {reason}")
                gates.append(self._gate("5c", "涨停拒买", True, "未涨停", "OK"))

        # 闸门 9:T+1 可卖校验(仅 sell,H6)
        if not is_buy:
            existing_pos = self._find_position(intent.code, positions)
            if existing_pos:
                can_use = existing_pos.get("can_use_volume", 0)
                if intent.volume > can_use:
                    gates.append(self._gate(9, "T+1可卖", False, f"≤can_use={can_use}", f"{intent.volume}"))
                    self._record_rejection("risk")
                    return (False, gates, f"卖出 {intent.volume} > 可卖 {can_use}(T+1)")
                # T+1 日历校验(双保险)
                entry_date = existing_pos.get("entry_date")
                if entry_date and hasattr(entry_date, "date"):
                    entry_d = entry_date.date() if hasattr(entry_date, "date") else entry_date
                else:
                    entry_d = entry_date
                from app.backtest.execution import can_sell_today
                if entry_d and not can_sell_today(entry_d, date.today()):
                    gates.append(self._gate(9, "T+1可卖", False, "today>entry_date", "当日买入不可卖"))
                    self._record_rejection("risk")
                    return (False, gates, "当日买入不可卖(T+1)")
                if entry_d is None:
                    # M4(审计):entry_date 缺失则日历双保险失效,仅靠 can_use_volume 兜底;
                    # 记 warning 提示运营(不强行拒卖,避免误拦 entry_date 未回填的旧持仓)
                    logger.warning(f"闸门9:持仓 {intent.code} entry_date 缺失,T+1 仅靠 can_use_volume 兜底")
            gates.append(self._gate(9, "T+1可卖", True, "≤can_use_volume", "OK"))

        # 闸门 7:连续拒绝(最后检查,本单通过则清零)
        # 主开关禁用时不熔断(kill_switch.is_enabled()==False → 跳过本闸门,仍记录拒绝但不拦单)
        if self.kill_switch and self.kill_switch.is_enabled() and self._check_consecutive_rejection():
            gates.append(self._gate(7, "连续拒绝", False, f"{self.config.max_consecutive_rejections}次/5分钟", "已达上限"))
            return (False, gates, "连续拒绝达上限,kill switch 已激活")
        gates.append(self._gate(7, "连续拒绝", True, f"{self.config.max_consecutive_rejections}次/5分钟", "OK"))

        # 全部通过,清零拒绝计数
        self._clear_rejections()
        return (True, gates, "")

    def _gate(self, num, name, passed, threshold, current) -> Dict:
        return {
            "gate": num, "name": name, "passed": passed,
            "threshold": threshold, "current": current,
            "detail": "" if passed else f"{name}拒绝"
        }

    def _find_position(self, code: str, positions: Optional[List[Dict]]) -> Dict:
        if not positions:
            return {}
        for p in positions:
            if p.get("code") == code or p.get("code", "").split(".")[0] == code.split(".")[0]:
                return p
        return {}

    def _total_managed_value(self, positions: Optional[List[Dict]], quote: Optional[Dict]) -> float:
        """策略持仓总市值(managed=true,排除 ETF)"""
        if not positions:
            return 0
        total = 0
        for p in positions:
            if not p.get("managed", True):
                continue  # 排除保留持仓(ETF)
            vol = p.get("volume", 0) + p.get("pending_buy_volume", 0)
            price = p.get("last_price", 0) or p.get("avg_cost", 0)
            total += vol * price
        return total

    def _calc_daily_loss_pct(self, asset: Optional[Dict], positions: Optional[List[Dict]],
                             quote: Optional[Dict]) -> Optional[float]:
        """日亏率 = (当前总资产 - 日亏基准) / 日亏基准(§16.4)

        2026-07-16:基准缺失已走兜底链(今日快照→昨收→本金),仅 asset 缺失时返回 None 触发 fail-safe。
        """
        if not asset:
            return None
        total = asset.get("total_asset", 0)
        open_asset = self._get_open_asset()
        if open_asset is None or open_asset <= 0:
            return None  # 无基准则 fail-safe 禁买
        return (total - open_asset) / open_asset

    def _get_open_asset(self) -> Optional[float]:
        """从 live_assets_backup 取日亏基准(§16.4 闸门5a 基准)

        2026-07-16 根因修复:今日无快照(QMT 开盘未连)时不再直接 fail-safe,
        走兜底链:今日首条快照 → 昨收快照 → live_capital(首日无历史)。
        仅 store 缺失/查询异常且 live_capital 无效时才返回 None。
        """
        try:
            if self.store:
                baseline = self.store.get_daily_baseline()
                if baseline is not None and baseline > 0:
                    return baseline
                logger.warning("闸门5a:今日+历史均无资产快照,用 live_capital 兜底基准")
        except Exception as e:
            logger.warning(f"获取开盘资产失败: {e}")
        # 兜底(无快照或查询异常):用 live_capital,不阻断交易(异常已在日志留痕)
        if self.config.live_capital > 0:
            return float(self.config.live_capital)
        return None

    # ===== 闸门7 拒绝计数(H4:5分钟窗+分类)=====

    def _record_rejection(self, category: str) -> None:
        """记录拒绝(risk/broker 才计入,market/code 只记 audit)"""
        import threading
        with self._rejection_lock:
            now = time.time()
            self._rejections.append((now, category))
            # 清理窗口外
            window = self.config.rejection_window_sec
            self._rejections = [(t, c) for t, c in self._rejections if now - t <= window]
            # 只算 risk/broker
            risk_broker = [c for t, c in self._rejections if c in ("risk", "broker")]
            if len(risk_broker) >= self.config.max_consecutive_rejections:
                if self.kill_switch and not self.kill_switch.is_enabled():
                    logger.info(f"闸门7:连续 {len(risk_broker)} 次risk/broker拒绝,但急停主开关已禁用,不激活")
                    return
                logger.critical(f"闸门7:5分钟内连续 {len(risk_broker)} 次risk/broker拒绝,激活kill switch")
                if self.kill_switch:
                    self.kill_switch.activate(
                        reason=f"连续{len(risk_broker)}次risk/broker拒绝",
                        source="gate7"
                    )

    def _check_consecutive_rejection(self) -> bool:
        """检查是否已达上限"""
        with self._rejection_lock:
            now = time.time()
            window = self.config.rejection_window_sec
            self._rejections = [(t, c) for t, c in self._rejections if now - t <= window]
            risk_broker = [c for t, c in self._rejections if c in ("risk", "broker")]
            return len(risk_broker) >= self.config.max_consecutive_rejections

    def _clear_rejections(self) -> None:
        with self._rejection_lock:
            self._rejections.clear()

    # ===== C1 在途预扣 =====

    def freeze_pending_buy(self, code: str, volume: int) -> None:
        """C1:下单成功后冻在途买入预扣(原子 SQL 更新,防 TOCTOU 竞态)"""
        if not self.store:
            return
        from app.utils.xtquant_compat import format_code
        code_fmt = format_code(code) if '.' not in code else code
        # 原子更新:pending_buy_volume += volume(避免读-改-写竞态)
        if not self.store.atomic_add_pending_buy(code_fmt, volume):
            # 新持仓:不存在则创建
            self.store.upsert_position({
                "code": code_fmt,
                "volume": 0, "can_use_volume": 0, "frozen_volume": 0,
                "pending_buy_volume": volume,
                "avg_cost": 0, "last_price": 0, "market_value": 0,
                "float_profit": 0, "profit_rate": 0, "peak_price": 0,
                "sell_count": 0, "managed": True, "strategy_name": "",
            })
        logger.info(f"C1 冻结在途预扣: {code_fmt} +{volume}")

    # ===== 闸门 10:同股冷却 =====

    def _check_same_stock_cooldown(self, code: str) -> Tuple[bool, str]:
        """闸门 10:同股 20 天冷却(buy-only)

        检查:
        1. live_deals 最近 20 天是否有同 code 的 sell 记录(主要)
        2. live_positions 是否有 volume=0 但 entry_date 在 20 天内的记录(已清仓但该冷却)

        Returns:
            (ok, reason) — ok=True=允许买入, ok=False=冷却中
        """
        if not self.store:
            return (True, "")

        from app.utils.xtquant_compat import format_code, strip_code_suffix

        code_fmt = format_code(code) if '.' not in code else code
        bare = strip_code_suffix(code_fmt)
        cooldown_days = 20
        cutoff_date = date.today() - timedelta(days=cooldown_days)

        # 检查 1:live_deals 最近 20 天的 sell 记录
        try:
            deals = self.store.get_deals(code=code_fmt, limit=50)
            for deal in deals:
                deal_code = deal.get("code", "")
                deal_bare = strip_code_suffix(deal_code) if deal_code else ""
                # 兼容两种 code 格式(漏洞B修复后统一了,但历史数据可能有裸代码)
                if deal_bare != bare:
                    continue
                if deal.get("direction") != "sell":
                    continue
                traded_at = deal.get("traded_at")
                if not traded_at:
                    continue
                # 处理各种日期格式
                if hasattr(traded_at, "date"):
                    trade_date = traded_at.date()
                elif isinstance(traded_at, str):
                    try:
                        trade_date = date.fromisoformat(traded_at[:10])
                    except (ValueError, IndexError):
                        continue
                else:
                    continue
                if trade_date >= cutoff_date:
                    return (False, f"同股冷却中: {code_fmt} 于 {trade_date} 卖出,需满 {cooldown_days} 天")
        except Exception as e:
            logger.warning(f"闸门10 检查 live_deals 异常: {e}")

        # 检查 2:live_positions 中 volume=0 但 entry_date 在 20 天内(已清仓但该冷却)
        try:
            pos = self.store.get_position(code_fmt)
            if pos:
                if pos.get("volume", 0) == 0 and pos.get("managed", True):
                    entry_date = pos.get("entry_date")
                    if entry_date:
                        if hasattr(entry_date, "date"):
                            entry_d = entry_date.date()
                        elif isinstance(entry_date, str):
                            try:
                                entry_d = date.fromisoformat(str(entry_date)[:10])
                            except (ValueError, IndexError):
                                entry_d = None
                        else:
                            entry_d = entry_date
                        if entry_d and entry_d >= cutoff_date:
                            return (False, f"同股冷却中: {code_fmt} 于 {entry_d} 清仓,需满 {cooldown_days} 天")
        except Exception as e:
            logger.warning(f"闸门10 检查 live_positions 异常: {e}")

        return (True, "OK")
