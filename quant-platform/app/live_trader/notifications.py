"""通知历史持久化(v6.0 Phase 1)

独立 DuckDB 连接(不走 LiveTraderStore 的 L1/L2 buffer),
sync 写盘防崩溃丢数据。

表: live_notifications(id/ts/level/channel/title/content/source)
索引: ts DESC + level
轮转: 保留最近 7 天(DELETE WHERE ts < now()-7days)
"""
import duckdb
import os
import threading
from datetime import datetime, date
from typing import Any, Optional

from core.logger import get_logger

logger = get_logger("live_trader.notifications")


class NotificationStore:
    """通知历史存储(独立 DuckDB, sync 写盘)"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = duckdb.connect(db_path, read_only=False)
        self._init_schema()

    def _init_schema(self) -> None:
        """建表 + 索引(幂等)"""
        con = self._conn
        con.execute("CREATE SEQUENCE IF NOT EXISTS notifications_seq START 1")
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_notifications (
                id BIGINT PRIMARY KEY DEFAULT nextval('notifications_seq'),
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                level TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'feishu',
                title TEXT NOT NULL,
                content TEXT,
                source TEXT
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_notifications_ts
            ON live_notifications(ts DESC)
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_notifications_level
            ON live_notifications(level)
        """)
        logger.info("live_notifications 表初始化完成")

    def record(
        self,
        level: str,
        title: str,
        content: str = "",
        source: str = "",
        channel: str = "feishu",
    ) -> int:
        """写入一条通知历史,返回新 id。失败仅日志不抛。

        Args:
            level: INFO / WARN / CRITICAL
            title: 短标题
            content: 正文(最多 500 字符截断)
            source: 触发来源 order/reconcile/kill_switch/scheduler/manual/test
            channel: feishu / wework / desktop / log

        Returns:
            新记录 id，失败返回 -1
        """
        if len(content) > 500:
            content = content[:500]
        with self._lock:
            try:
                # DuckDB 没有 result.last_id (那是 SQLite/MySQL API)。
                # 用 RETURNING id + fetchone() 取自增主键。
                row = self._conn.execute(
                    """
                    INSERT INTO live_notifications (level, title, content, source, channel)
                    VALUES (?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    [level, title, content, source, channel],
                ).fetchone()
                return int(row[0]) if row else -1
            except Exception as e:
                logger.warning(f"通知历史写入失败: {e}")
                return -1

    def recent(
        self,
        limit: int = 50,
        level: Optional[str] = None,
        since_iso: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """查询最近通知。

        Args:
            limit: 最大返回条数
            level: 可选过滤 INFO / WARN / CRITICAL
            since_iso: 可选 ISO 时间字符串下限

        Returns:
            list[dict]，按 ts DESC 排列
        """
        with self._lock:
            try:
                conditions = []
                params = []
                if level in ("INFO", "WARN", "CRITICAL"):
                    conditions.append("level = ?")
                    params.append(level)
                if since_iso:
                    conditions.append("ts >= ?")
                    params.append(since_iso)
                where = " AND ".join(conditions) if conditions else "1=1"
                rows = self._conn.execute(
                    f"""
                    SELECT id, ts, level, channel, title, content, source
                    FROM live_notifications
                    WHERE {where}
                    ORDER BY ts DESC
                    LIMIT ?
                    """,
                    [*params, limit],
                ).fetchall()
                cols = ["id", "ts", "level", "channel", "title", "content", "source"]
                return [dict(zip(cols, r)) for r in rows]
            except Exception as e:
                logger.warning(f"通知历史查询失败: {e}")
                return []

    def count_by_level(
        self, since_iso: Optional[str] = None
    ) -> dict[str, int]:
        """按 level 统计计数。

        Returns:
            {"INFO": N, "WARN": N, "CRITICAL": N}
        """
        with self._lock:
            try:
                if since_iso:
                    rows = self._conn.execute(
                        """
                        SELECT level, COUNT(*) FROM live_notifications
                        WHERE ts >= ? GROUP BY level
                        """,
                        [since_iso],
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        """
                        SELECT level, COUNT(*) FROM live_notifications GROUP BY level
                        """
                    ).fetchall()
                return {r[0]: int(r[1]) for r in rows}
            except Exception as e:
                logger.warning(f"通知统计失败: {e}")
                return {"INFO": 0, "WARN": 0, "CRITICAL": 0}

    def cleanup(self, retention_days: int = 7) -> int:
        """删除 retention_days 天之前的记录，返回删除条数。

        Args:
            retention_days: 保留天数，默认 7

        Returns:
            删除条数
        """
        with self._lock:
            try:
                # H1 修复(审计):DuckDB DELETE 的 rowcount 恒返回 -1,
                # 改为先 COUNT 再 DELETE(两次查询但跨版本安全,返回真实删除条数)
                cnt_row = self._conn.execute(
                    "SELECT COUNT(*) FROM live_notifications "
                    "WHERE ts < CURRENT_TIMESTAMP - INTERVAL '1 day' * ?",
                    [retention_days],
                ).fetchone()
                deleted = int(cnt_row[0]) if cnt_row else 0
                if deleted:
                    self._conn.execute(
                        """
                        DELETE FROM live_notifications
                        WHERE ts < CURRENT_TIMESTAMP - INTERVAL '1 day' * ?
                        """,
                        [retention_days],
                    )
                    logger.info(f"通知历史清理: 删除 {deleted} 条")
                return deleted
            except Exception as e:
                logger.warning(f"通知历史清理失败: {e}")
                return 0

    def close(self) -> None:
        """关闭连接"""
        if self._conn:
            self._conn.close()
            logger.info("NotificationStore 关闭")


# =============================================================================
# 独立工具函数(不依赖 self，仅用于格式生成)
# =============================================================================

def format_daily_summary(
    asset_data: dict[str, Any],
    positions: list[dict[str, Any]],
    today_pnl: float,
    deal_count: int,
) -> str:
    """生成每日账户概览 markdown 文本。

    Args:
        asset_data: {"total_asset", "market_value", "cash", "frozen_cash"}
        positions: 持仓列表(含 code/name/market_value/float_profit)
        today_pnl: 当日盈亏(含正负号)
        deal_count: 今日成交笔数

    Returns:
        markdown 格式字符串
    """
    total = asset_data.get("total_asset", 0)
    mv = asset_data.get("market_value", 0)
    cash = asset_data.get("cash", 0)
    sign = "+" if today_pnl >= 0 else "-"
    pnl_str = f"{sign}¥{abs(today_pnl):,.0f}"

    content = (
        f"**📊 每日账户概览 {datetime.now().strftime('%Y-%m-%d')}**\n"
        f"> 总资产: ¥{total:,.0f}\n"
        f"> 持仓市值: ¥{mv:,.0f}\n"
        f"> 可用现金: ¥{cash:,.0f}\n"
        f"> 当日盈亏: {pnl_str}\n"
        f"> 今日成交: {deal_count} 笔\n"
    )
    if positions:
        top5 = sorted(
            positions, key=lambda p: p.get("market_value", 0), reverse=True
        )[:5]
        content += "> **前 5 大持仓:**\n"
        for p in top5:
            fp = p.get("float_profit", 0)
            name = (p.get("name") or p.get("code") or "")
            if len(name) > 4:
                name = name[:4] + "…"
            code = p.get("code", "")
            code = p.get("code", "")
            content += (
                f"> - {code} {name} ¥{p.get('market_value', 0):,.0f} "
                f"({'+' if fp >= 0 else ''}¥{fp:,.0f})\n"
            )
    return content


# =============================================================================
# 每日概览数据汇总工具(给 scheduler 调用,算当日值)
# =============================================================================

def calc_today_deal_count(store) -> int:
    """从 live_deals 表计算今日成交笔数（用公共 API，防私有成员访问）"""
    try:
        from datetime import datetime
        today_date = date.today()
        deals = store.get_deals(limit=10000)
        today_str = today_date.isoformat()
        count = sum(1 for d in deals
                     if d.get("traded_at", "").startswith(today_str))
        return count
    except Exception:
        return 0
