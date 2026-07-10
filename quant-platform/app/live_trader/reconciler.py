"""对账引擎(v5.4 §5.9 / §18.2 / §19.4)

三方比对: A(QMT实时持仓) vs B(本地live_positions) vs C(live_deals还原)。
关键:v5.3 修复——不回写 live_positions,只写 live_positions_audit 表。
偏差双门限:max(100股, 市值×0.5%)。
扣除时点间正常成交量(H5)。
ETF 保留持仓(managed=false):偏差只告警不触发 kill switch。
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.logger import get_logger

from .config import LiveTraderConfig

logger = get_logger("live_trader.reconciler")


class Reconciler:
    """三方比对对账引擎"""

    def __init__(self, config: LiveTraderConfig, store=None, qmt_wrapper=None,
                 kill_switch=None, notify=None, pnl_engine=None):
        self.config = config
        self.store = store
        self.qmt = qmt_wrapper
        self.kill_switch = kill_switch
        self.notify = notify
        self.pnl_engine = pnl_engine
        self._last_reconcile_time: Optional[datetime] = None

    def reconcile(self) -> Dict[str, Any]:
        """执行一次三方比对

        A = QMT 实时持仓
        B = 本地 live_positions 缓存
        C = live_deals 还原(buildSimpleCycles)

        Returns:
            {timestamp, total, critical, warnings, infos, details}
        """
        ts = datetime.now().isoformat()
        result = {"timestamp": ts, "total": 0, "critical": 0,
                  "warnings": 0, "infos": 0, "details": [],
                  "summary": ""}

        if not self.qmt or not self.qmt.connected:
            logger.warning("对账:QMT 未连接,跳过")
            result["error"] = "QMT 未连接"
            return result

        # A = QMT 实时持仓
        qmt_positions = self.qmt.query_positions()
        if qmt_positions is None:
            qmt_positions = []

        # B = 本地 live_positions
        local_positions = self.store.get_positions() if self.store else []

        # C = live_deals 还原(三方比对的第三源)
        deals_volume = self._restore_volume_from_deals()

        # 时点间正常成交量(从 callback/deals 记录,§19.4 H5)
        inflight_volume = self._get_inflight_volume_since(self._last_reconcile_time)

        # 按代码聚合三方数据
        qmt_by_code = {p.get("code", ""): p for p in qmt_positions}
        local_by_code = {p.get("code", ""): p for p in local_positions}
        all_codes = set(qmt_by_code.keys()) | set(local_by_code.keys()) | set(deals_volume.keys())

        for code in all_codes:
            qmt_pos = qmt_by_code.get(code, {})
            local_pos = local_by_code.get(code, {})
            managed = local_pos.get("managed", True)

            qmt_vol = int(qmt_pos.get("volume", 0))
            local_vol = int(local_pos.get("volume", 0))
            deals_vol = deals_volume.get(code, 0)

            # 扣除时点间正常成交量(H5)
            # inflight_volume 是上次对账后到现在的成交调整量
            inflight_adj = inflight_volume.get(code, 0)
            adjusted_diff = (local_vol - qmt_vol) - inflight_adj

            # 三方偏差汇总
            # A vs B: QMT vs 本地
            diff_ab = local_vol - qmt_vol
            # A vs C: QMT vs deals还原(净持仓)
            diff_ac = qmt_vol - deals_vol if deals_vol > 0 else 0
            # B vs C: 本地 vs deals还原
            diff_bc = local_vol - deals_vol if deals_vol > 0 else 0

            # 主要偏差用 A vs B(扣除时点间成交量后)
            diff_vol = adjusted_diff

            # 偏差判定(双门限,H5)
            last_price = float(qmt_pos.get("last_price", 0) or local_pos.get("last_price", 0))
            diff_value = abs(diff_vol * last_price)
            market_value = max(qmt_vol, local_vol) * last_price if last_price > 0 else 0

            min_shares = self.config.reconcile_diff_min_shares
            pct_threshold = market_value * self.config.reconcile_diff_pct
            threshold = max(min_shares, pct_threshold)

            if abs(diff_vol) <= threshold:
                level = "INFO"
                result["infos"] += 1
            elif diff_value <= market_value * self.config.reconcile_critical_pct:
                level = "WARN"
                result["warnings"] += 1
            else:
                level = "CRITICAL"
                result["critical"] += 1

            result["total"] += 1

            # 写 audit 表(不回写 live_positions)
            if abs(diff_vol) > 0 and self.store:
                self._write_audit(code, local_vol, qmt_vol, diff_vol,
                                  diff_value, level, managed,
                                  deals_vol=deals_vol, inflight_adj=inflight_adj)

            # CRITICAL 处理
            if level == "CRITICAL":
                if not managed:
                    # ETF 保留持仓:只告警不 kill switch
                    logger.warning(f"对账 CRITICAL(ETF保留,不kill): {code} "
                                   f"local={local_vol} qmt={qmt_vol} deals={deals_vol}")
                    if self.notify:
                        self.notify.reconcile_diff(code, diff_vol, diff_value, "CRITICAL(ETF)")
                else:
                    logger.critical(f"对账 CRITICAL: {code} local={local_vol} qmt={qmt_vol} "
                                    f"deals={deals_vol} diff={diff_vol}")
                    if self.notify:
                        self.notify.reconcile_diff(code, diff_vol, diff_value, "CRITICAL")
                    if self.kill_switch:
                        self.kill_switch.activate(
                            reason=f"对账偏差CRITICAL: {code} diff={diff_vol}",
                            source="reconciler"
                        )

            result["details"].append({
                "code": code, "local_volume": local_vol, "qmt_volume": qmt_vol,
                "deals_volume": deals_vol, "diff_volume": diff_vol,
                "diff_value": round(diff_value, 2), "level": level,
                "managed": managed,
            })

        result["summary"] = (f"total={result['total']} CRITICAL={result['critical']} "
                              f"WARN={result['warnings']} INFO={result['infos']}")
        logger.info(f"对账完成: {result['summary']}")

        # 记录本次对账时间(下次用,扣除成交量)
        self._last_reconcile_time = datetime.now()
        return result

    def _restore_volume_from_deals(self) -> Dict[str, int]:
        """从 live_deals 还原净持仓(三方比对的 C 源)

        按 code 分组,买加卖减,得到净持仓量。
        """
        if not self.store:
            return {}
        try:
            deals = self.store.get_deals(limit=5000)
            volume_by_code: Dict[str, int] = {}
            for d in deals:
                code = d.get("code", "")
                direction = d.get("direction", "")
                vol = int(d.get("filled_volume", 0))
                if direction == "buy":
                    volume_by_code[code] = volume_by_code.get(code, 0) + vol
                elif direction == "sell":
                    volume_by_code[code] = volume_by_code.get(code, 0) - vol
            return volume_by_code
        except Exception as e:
            logger.error(f"还原 deals 持仓失败: {e}")
            return {}

    def _get_inflight_volume_since(self, since: Optional[datetime]) -> Dict[str, int]:
        """获取上次对账以来的成交调整量(扣除正常成交量,H5)

        两时点间的成交(callback 已记录)先从 diff 中剔除,剩余才算真偏差。
        """
        if not self.store or since is None:
            return {}
        try:
            # 查询 since 之后的成交
            assert self.store._conn is not None
            rows = self.store._conn.execute(
                "SELECT code, direction, filled_volume FROM live_deals "
                "WHERE traded_at > ? ORDER BY traded_at",
                [since]
            ).fetchall()
            volume_by_code: Dict[str, int] = {}
            for row in rows:
                code, direction, vol = row[0], row[1], int(row[2] or 0)
                # 买入增加本地持仓,卖出减少
                if direction == "buy":
                    volume_by_code[code] = volume_by_code.get(code, 0) + vol
                elif direction == "sell":
                    volume_by_code[code] = volume_by_code.get(code, 0) - vol
            return volume_by_code
        except Exception as e:
            logger.error(f"获取时点间成交量失败: {e}")
            return {}

    def _write_audit(self, code: str, local_vol: int, qmt_vol: int,
                     diff_vol: int, diff_value: float, level: str,
                     managed: bool, deals_vol: int = 0,
                     inflight_adj: int = 0) -> None:
        """写 live_positions_audit(不回写 live_positions)"""
        if not self.store:
            return
        try:
            assert self.store._conn is not None
            self.store._conn.execute("""
                INSERT INTO live_positions_audit
                (timestamp, code, local_volume, qmt_volume, diff_volume,
                 diff_value, source, resolved)
                VALUES (?,?,?,?,?,?,?,FALSE)
            """, [datetime.now(), code, local_vol, qmt_vol,
                  diff_vol, diff_value,
                  f"3way|deals={deals_vol}|inflight={inflight_adj}|{level}"])
        except Exception as e:
            logger.error(f"写对账 audit 失败: {e}")
