"""PnlEngine 盈亏闭环(v5.4 §5.8)

移植 MQ buildSimpleCycles(trade-logs-service.ts:48-221)。
以买入为基准,净仓归零闭合周期。用 order_type(23买/24卖)判方向。
渐进式:与 sim_trader total_equity 并行,阶段3 切换为准。
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("live_trader.pnl")


class PnlEngine:
    """buildSimpleCycles 盈亏闭环"""

    def __init__(self, store=None):
        self.store = store

    def build_cycles(self, deals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """构建交易闭环

        算法(移植 MQ):
        1. 按 code 分组,按时间升序
        2. 遍历成交,用 order_type/direction 判方向(23=买,24=卖)
        3. 累积净仓 net
        4. 净仓归零 = 周期闭合
        5. 加权均价 = sum(price*vol)/sum(vol)

        Returns:
            cycles: [{cycle_id, code, status, buy_avg, sell_avg, pnl, pnl_pct}]
        """
        if not deals:
            return []

        # v2审计H2/F8: 只算 live 成交(dry-run mock 不污染真实盈亏闭环)
        deals = [d for d in deals if d.get("mode") == "live"]
        if not deals:
            return []

        # 按 code 分组
        by_code: Dict[str, List[Dict]] = {}
        for d in deals:
            code = d.get("code", "")
            if not code:
                continue
            by_code.setdefault(code, []).append(d)

        cycles = []
        cycle_id = 0
        for code, code_deals in by_code.items():
            # 按时间排序
            code_deals.sort(key=lambda x: x.get("traded_at") or datetime.min)

            net = 0  # 净仓
            current_cycle: Optional[Dict] = None
            sum_buy_amount = 0.0
            sum_buy_volume = 0
            sum_sell_amount = 0.0
            sum_sell_volume = 0

            for d in code_deals:
                direction = d.get("direction", "")
                vol = int(d.get("filled_volume", 0))
                price = float(d.get("filled_price", 0))
                amount = float(d.get("filled_amount", price * vol))

                if direction == "buy":
                    if net == 0:
                        # 开新周期
                        if current_cycle:
                            cycles.append(current_cycle)
                        cycle_id += 1
                        current_cycle = {
                            "cycle_id": cycle_id, "code": code, "status": "ongoing",
                            "buy_avg_price": 0, "sell_avg_price": 0,
                            "pnl": 0, "pnl_pct": 0,
                        }
                        sum_buy_amount = 0.0
                        sum_buy_volume = 0
                        sum_sell_amount = 0.0
                        sum_sell_volume = 0
                    net += vol
                    sum_buy_amount += amount
                    sum_buy_volume += vol
                elif direction == "sell":
                    net -= vol
                    sum_sell_amount += amount
                    sum_sell_volume += vol

                # 净仓归零 = 闭合
                if net == 0 and current_cycle:
                    current_cycle["status"] = "closed"
                    current_cycle["buy_avg_price"] = (
                        sum_buy_amount / sum_buy_volume if sum_buy_volume > 0 else 0
                    )
                    current_cycle["sell_avg_price"] = (
                        sum_sell_amount / sum_sell_volume if sum_sell_volume > 0 else 0
                    )
                    current_cycle["pnl"] = sum_sell_amount - sum_buy_amount
                    if sum_buy_amount > 0:
                        current_cycle["pnl_pct"] = current_cycle["pnl"] / sum_buy_amount
                    cycles.append(current_cycle)
                    current_cycle = None
                    net = 0

            # 收尾:未闭合周期
            if current_cycle:
                current_cycle["buy_avg_price"] = (
                    sum_buy_amount / sum_buy_volume if sum_buy_volume > 0 else 0
                )
                current_cycle["sell_avg_price"] = (
                    sum_sell_amount / sum_sell_volume if sum_sell_volume > 0 else 0
                )
                cycles.append(current_cycle)

        return cycles

    def recompute(self, code: str) -> None:
        """卖出成交后重算盈亏(§5.3 callback 调用)"""
        if not self.store:
            return
        try:
            deals = self.store.get_deals(code=code, limit=500)
            cycles = self.build_cycles(deals)
            # 写 live_cycles 表
            for c in cycles:
                self._upsert_cycle(c)
            logger.info(f"盈亏重算 {code}: {len(cycles)} 个周期")
        except Exception as e:
            logger.error(f"盈亏重算失败 {code}: {e}")

    def _upsert_cycle(self, cycle: Dict) -> None:
        if not self.store:
            return
        try:
            assert self.store._conn is not None
            self.store._conn.execute("""
                INSERT INTO live_cycles
                (cycle_id, code, status, buy_avg_price, sell_avg_price, pnl, pnl_pct)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(cycle_id, code) DO UPDATE SET
                    status=excluded.status, buy_avg_price=excluded.buy_avg_price,
                    sell_avg_price=excluded.sell_avg_price, pnl=excluded.pnl, pnl_pct=excluded.pnl_pct
            """, [
                cycle["cycle_id"], cycle["code"], cycle["status"],
                cycle["buy_avg_price"], cycle["sell_avg_price"],
                cycle["pnl"], cycle["pnl_pct"],
            ])
        except Exception as e:
            logger.error(f"写 cycle 失败: {e}")
