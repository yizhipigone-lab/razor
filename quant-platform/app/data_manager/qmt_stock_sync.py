"""
QMT 股票列表同步模块
通过 Proxy 从 QMT 获取最新股票列表，与本地 DuckDB 比对，
自动处理新股添加、退市标记等日常维护。
"""
import pandas as pd
from core.logger import get_logger
from database.duckdb_manager import db
from app.trader.gateways.qmt import qmt_gateway

log = get_logger("QMT-StockSync")


class QMTStockSync:
    """基于 QMT 行情源的股票元数据同步器"""

    def sync(self) -> dict:
        """执行一次全量同步，返回变更摘要"""
        # 1. 从 Proxy 获取 QMT 全部股票代码（快速，无详情）
        codes = qmt_gateway.get_stock_list(details=False)
        if not codes:
            return {"status": "error", "message": "无法从 QMT Proxy 获取股票列表，请检查 Windows 端代理服务"}

        qmt_codes = {c.split(".")[0] for c in codes}
        log.info(f"QMT 返回 {len(qmt_codes)} 只股票代码")

        # 2. 获取当前 DB 中状态为 active 的股票
        df_active = db.get_all_stocks(status="active")
        db_codes = set(df_active["code"].tolist()) if not df_active.empty else set()
        # 也考虑 status='delisted' 的股票，避免重复处理已退市的
        df_all = db.conn.execute("SELECT code, status FROM stocks").df()
        all_db_codes = set(df_all["code"].tolist()) if not df_all.empty else set()

        # 3. 标记退市股票（DB active 但 QMT 已不存在）
        delisted = db_codes - qmt_codes
        if delisted:
            log.warning(f"检测到 {len(delisted)} 只退市/摘牌股票，正在标记...")
            db.mark_delisted(list(delisted))

        # 4. 检测并添加新股（QMT 有但 DB 全集中没有）
        new_codes = qmt_codes - all_db_codes
        added = []
        if new_codes:
            log.info(f"检测到 {len(new_codes)} 只新股，正在获取详情...")
            # 只请求新股的详情（带后缀的完整代码）
            new_qmt_codes = [c for c in codes if c.split(".")[0] in new_codes]
            details = qmt_gateway.get_stock_list(details=True, codes=new_qmt_codes)
            rows = []
            for s in details:
                clean = s["code"].split(".")[0]
                exchange = "SH" if s["code"].endswith(".SH") else "SZ" if s["code"].endswith(".SZ") else ""
                list_date_str = s.get("list_date", "")
                try:
                    list_date = pd.Timestamp(list_date_str).date() if list_date_str else None
                except Exception:
                    list_date = None
                rows.append({
                    "code": clean,
                    "name": (s.get("name") or clean)[:20],
                    "exchange": exchange,
                    "sector": s.get("sector", ""),
                    "concepts": "",
                    "list_date": list_date,
                    "status": "active",
                })
                added.append(clean)
            if rows:
                df_new = pd.DataFrame(rows)
                db.upsert_stocks(df_new)
                log.info(f"成功添加 {len(rows)} 只新股")

        summary = {
            "status": "ok",
            "total_qmt": len(qmt_codes),
            "total_db_active": len(db_codes),
            "added": len(added),
            "delisted": len(delisted),
        }
        log.info(
            f"股票同步完成: QMT {summary['total_qmt']} 只, "
            f"DB活跃 {summary['total_db_active']} 只, "
            f"新增 {summary['added']}, 退市 {summary['delisted']}"
        )
        return summary


qmt_stock_sync = QMTStockSync()
