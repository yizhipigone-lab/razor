"""
DuckDB + Parquet 数据持久化管理器
负责：
1. 股票元数据（代码、名称、板块、题材）的存储与查询
2. K 线数据的按股票分片 Parquet 存储
3. 高效的批量查询（全市场日线数据拉取秒级返回）
4. 新增股票、退市处理
"""

import threading
import duckdb
import pandas as pd
import time
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Optional

from core.logger import get_logger
from core.settings import settings
from app.data_manager.indicators import enrich_with_indicators

log = get_logger("DuckDB")

ROOT_DIR = Path(__file__).parent.parent
DATA_DIR = ROOT_DIR / "data"
PARQUET_DAILY_DIR = DATA_DIR / "parquet" / "daily"
PARQUET_MIN5_DIR = DATA_DIR / "parquet" / "min5"
META_DIR = DATA_DIR / "meta"
DB_PATH = META_DIR / "meta.db"

_db_lock = threading.Lock()

class DatabaseManager:
    """
    DuckDB 数据库管理器（单例模式，线程安全）
    """
    _instance = None

    def __new__(cls):
        with _db_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
        return cls._instance

    def _init(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._connections: dict = {}
        self._conn_lock = threading.Lock()
        self._create_tables()
        log.info(f"DuckDB 核心就绪: {DB_PATH}")

    @property
    def conn(self):
        """线程私有连接，带有重试逻辑捕获残留锁"""
        tid = threading.current_thread().ident
        if tid not in self._connections:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            retries = 5
            while retries > 0:
                try:
                    conn = duckdb.connect(str(DB_PATH))
                    with self._conn_lock:
                        self._connections[tid] = conn
                    break
                except Exception as e:
                    retries -= 1
                    if retries == 0:
                        log.error(f"DuckDB | 无法连接到数据库: {e}")
                        raise e
                    log.warning(f"DuckDB | 数据库占用中，准备重试... ({5-retries}/5)")
                    time.sleep(0.5)
        return self._connections[tid]

    def _create_tables(self):
        """初始化底层表结构 (全量恢复)"""
        try:
            c = self.conn
            # 1. 基础股票元数据
            c.execute("""
                CREATE TABLE IF NOT EXISTS stocks (
                    code      VARCHAR PRIMARY KEY,
                    name      VARCHAR,
                    exchange  VARCHAR,
                    sector    VARCHAR,
                    concepts  VARCHAR,
                    list_date DATE,
                    delist_date DATE,
                    status    VARCHAR DEFAULT 'active',
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS stock_fundamentals (
                    code      VARCHAR PRIMARY KEY,
                    pe_ttm    DOUBLE,
                    pb        DOUBLE,
                    total_mv  DOUBLE,
                    circ_mv   DOUBLE,
                    roe       DOUBLE,
                    gross_margin DOUBLE,
                    net_profit_yoy DOUBLE,
                    debt_to_assets DOUBLE,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            # 2. 策略库与扫描历史
            c.execute("""
                CREATE TABLE IF NOT EXISTS strategies (
                    id           INTEGER PRIMARY KEY,
                    name         VARCHAR UNIQUE,
                    description  VARCHAR,
                    code_path    VARCHAR,
                    code_content VARCHAR,
                    is_active    BOOLEAN DEFAULT TRUE,
                    params       JSON,
                    metadata     JSON,
                    created_at   TIMESTAMP DEFAULT NOW()
                )
            """)
            # 同步序列值：先删再建以兼容各版本 DuckDB
            max_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM strategies").fetchone()[0]
            c.execute("DROP SEQUENCE IF EXISTS seq_strategies")
            c.execute(f"CREATE SEQUENCE seq_strategies START {max_id + 1}")

            c.execute("""
                CREATE TABLE IF NOT EXISTS scan_history (
                    id           INTEGER PRIMARY KEY,
                    strategy_id  INTEGER,
                    strategy_name VARCHAR,
                    scan_time    TIMESTAMP,
                    params       JSON,
                    stock_codes  JSON,
                    result_count INTEGER
                )
            """)
            # 同步序列值：先删再建
            max_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM scan_history").fetchone()[0]
            c.execute("DROP SEQUENCE IF EXISTS seq_scan")
            c.execute(f"CREATE SEQUENCE seq_scan START {max_id + 1}")

            # 3. 交易、持仓与 AI 报告
            c.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id           INTEGER PRIMARY KEY,
                    code         VARCHAR,
                    name         VARCHAR,
                    open_price   DOUBLE,
                    open_time    TIMESTAMP,
                    volume       INTEGER,
                    remain_volume INTEGER,
                    cost         DOUBLE,
                    highest_price DOUBLE DEFAULT 0,
                    trailing_activated BOOLEAN DEFAULT FALSE,
                    status       VARCHAR DEFAULT 'open'
                )
            """)
            # 同步序列值：先删再建
            max_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM positions").fetchone()[0]
            c.execute("DROP SEQUENCE IF EXISTS seq_position")
            c.execute(f"CREATE SEQUENCE seq_position START {max_id + 1}")
            
            c.execute("""
                CREATE TABLE IF NOT EXISTS trade_history (
                    id           INTEGER PRIMARY KEY,
                    position_id  INTEGER,
                    code         VARCHAR,
                    name         VARCHAR,
                    direction    VARCHAR,
                    price        DOUBLE,
                    volume       INTEGER,
                    amount       DOUBLE,
                    trade_time   TIMESTAMP,
                    trade_type   VARCHAR,
                    reason       VARCHAR,
                    strategy_name VARCHAR
                )
            """)
            # 同步序列值：先删再建
            max_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM trade_history").fetchone()[0]
            c.execute("DROP SEQUENCE IF EXISTS seq_trade")
            c.execute(f"CREATE SEQUENCE seq_trade START {max_id + 1}")

            c.execute("""
                CREATE TABLE IF NOT EXISTS ai_reports (
                    id           INTEGER PRIMARY KEY,
                    code         VARCHAR,
                    name         VARCHAR,
                    content      TEXT,
                    created_at   TIMESTAMP DEFAULT NOW()
                )
            """)
            # 确保序列同步到现有最大 ID 以防冲突：先删后建
            max_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM ai_reports").fetchone()[0]
            c.execute("DROP SEQUENCE IF EXISTS seq_ai_report CASCADE")
            c.execute(f"CREATE SEQUENCE seq_ai_report START {max_id + 1}")
            
            # 4. 自选股
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_watchlist (
                    code VARCHAR PRIMARY KEY,
                    name VARCHAR,
                    added_at TIMESTAMP DEFAULT NOW(),
                    source VARCHAR
                )
            """)

            # 5. 指数成分股
            c.execute("""
                CREATE TABLE IF NOT EXISTS index_members (
                    index_code VARCHAR,
                    stock_code VARCHAR,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (index_code, stock_code)
                )
            """)

            # 6. 基础回测历史
            c.execute("""
                CREATE TABLE IF NOT EXISTS backtest_history (
                    id            INTEGER PRIMARY KEY,
                    created_at    TIMESTAMP DEFAULT NOW(),
                    strategy_name VARCHAR,
                    start_date    DATE,
                    end_date      DATE,
                    exchanges     VARCHAR,
                    sectors       VARCHAR,
                    index_filter  VARCHAR,
                    min_mv        DOUBLE,
                    max_mv        DOUBLE,
                    risk_params   VARCHAR,
                    total_trades  INTEGER,
                    win_rate      DOUBLE,
                    avg_pnl_pct   DOUBLE,
                    trades_json   TEXT
                )
            """)
            max_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM backtest_history").fetchone()[0]
            c.execute("DROP SEQUENCE IF EXISTS seq_bt_hist")
            c.execute(f"CREATE SEQUENCE seq_bt_hist START {max_id + 1}")

            # 7. AI 回测历史
            c.execute("""
                CREATE TABLE IF NOT EXISTS ai_backtest_history (
                    id             INTEGER PRIMARY KEY,
                    created_at     TIMESTAMP DEFAULT NOW(),
                    strategy_name  VARCHAR,
                    start_date     DATE,
                    end_date       DATE,
                    exchanges      VARCHAR,
                    sectors        VARCHAR,
                    index_filter   VARCHAR,
                    min_mv         DOUBLE,
                    max_mv         DOUBLE,
                    use_llm        BOOLEAN,
                    n_exploration  INTEGER,
                    n_bayesian     INTEGER,
                    best_avg_pnl   DOUBLE,
                    best_win_rate  DOUBLE,
                    best_params    VARCHAR,
                    top10_json     TEXT,
                    wfo_json       TEXT,
                    llm_report     TEXT,
                    regime_summary VARCHAR
                )
            """)
            max_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM ai_backtest_history").fetchone()[0]
            c.execute("DROP SEQUENCE IF EXISTS seq_ai_bt_hist")
            c.execute(f"CREATE SEQUENCE seq_ai_bt_hist START {max_id + 1}")

            # 8. 概念↔股票映射表
            c.execute("""
                CREATE TABLE IF NOT EXISTS concept_stocks (
                    concept_name VARCHAR,
                    stock_code   VARCHAR,
                    source       VARCHAR DEFAULT 'tushare',
                    updated_at   TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (concept_name, stock_code)
                )
            """)

            # 9. 概念热度表
            c.execute("""
                CREATE TABLE IF NOT EXISTS concept_heat (
                    concept_name       VARCHAR,
                    trade_date         DATE,
                    hotness            DOUBLE DEFAULT 0.0,
                    constituent_count  INTEGER DEFAULT 0,
                    advance_count      INTEGER DEFAULT 0,
                    decline_count      INTEGER DEFAULT 0,
                    avg_change_pct     DOUBLE DEFAULT 0.0,
                    updated_at         TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (concept_name, trade_date)
                )
            """)

            # 10. 行业板块热度表
            c.execute("""
                CREATE TABLE IF NOT EXISTS sector_heat (
                    sector_name        VARCHAR,
                    trade_date         DATE,
                    hotness            DOUBLE DEFAULT 0.0,
                    constituent_count  INTEGER DEFAULT 0,
                    advance_count      INTEGER DEFAULT 0,
                    decline_count      INTEGER DEFAULT 0,
                    avg_change_pct     DOUBLE DEFAULT 0.0,
                    updated_at         TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (sector_name, trade_date)
                )
            """)

            # 11. 通达信选股历史
            c.execute("""
                CREATE TABLE IF NOT EXISTS tqsdk_screen_history (
                    id            INTEGER PRIMARY KEY,
                    formula_name  VARCHAR,
                    formula_arg   VARCHAR,
                    start_date    DATE,
                    end_date      DATE,
                    stock_count   INTEGER,
                    stock_codes   JSON,
                    stock_details JSON,
                    executed_at   TIMESTAMP DEFAULT NOW()
                )
            """)
            max_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM tqsdk_screen_history").fetchone()[0]
            c.execute("DROP SEQUENCE IF EXISTS seq_tqsdk_screen")
            c.execute(f"CREATE SEQUENCE seq_tqsdk_screen START {max_id + 1}")

            c.commit()
            log.info("DuckDB | 所有核心表结构初始化完成 (含热点板块)")
        except Exception as e:
            log.error(f"DuckDB | 初始化表结构失败: {e}")
            raise e

    # ========== 股票元数据 ==========

    def upsert_stocks(self, df: pd.DataFrame):
        """批量插入或更新股票元数据"""
        self.conn.execute("""
            INSERT INTO stocks (code, name, exchange, sector, concepts, list_date, status, updated_at)
            SELECT code, name, exchange, sector, concepts, list_date, status, NOW()
            FROM df
            ON CONFLICT (code) DO UPDATE SET
                name = excluded.name,
                sector = CASE WHEN excluded.sector IS NOT NULL AND excluded.sector != '' THEN excluded.sector ELSE stocks.sector END,
                exchange = CASE WHEN excluded.exchange IS NOT NULL AND excluded.exchange != 'UNKNOWN' THEN excluded.exchange ELSE stocks.exchange END, 
                concepts = CASE WHEN excluded.concepts IS NOT NULL AND excluded.concepts != '' THEN excluded.concepts ELSE stocks.concepts END,
                status = excluded.status,
                updated_at = excluded.updated_at
        """)
        self.conn.commit()
        log.info(f"更新股票元数据: {len(df)} 条")

    def update_stock_sectors(self, df: pd.DataFrame):
        """批量更新股票的行业信息"""
        try:
            # 使用临时表加速更新（线程唯一表名避免冲突）
            tid = threading.current_thread().ident
            tbl = f"temp_sectors_{tid}"
            self.conn.execute(f"CREATE OR REPLACE TEMPORARY TABLE {tbl} AS SELECT * FROM df")
            self.conn.execute(f"""
                UPDATE stocks
                SET sector = {tbl}.industry
                FROM {tbl}
                WHERE stocks.code = {tbl}.code
            """)
            self.conn.commit()
            log.info("DuckDB | 行业元数据更新成功")
        except Exception as e:
            log.error(f"DuckDB | 更新行业信息失败: {e}")

    def upsert_fundamentals(self, df: pd.DataFrame):
        """批量插入或更新股票基本面财务指标"""
        required_cols = ['pe_ttm', 'pb', 'total_mv', 'circ_mv', 'roe', 'gross_margin', 'net_profit_yoy', 'debt_to_assets']
        for col in required_cols:
            if col not in df.columns:
                df[col] = None

        self.conn.execute("""
            INSERT INTO stock_fundamentals (code, pe_ttm, pb, total_mv, circ_mv, roe, gross_margin, net_profit_yoy, debt_to_assets, updated_at)
            SELECT code, pe_ttm, pb, total_mv, circ_mv, roe, gross_margin, net_profit_yoy, debt_to_assets, NOW()
            FROM df
            ON CONFLICT (code) DO UPDATE SET
                pe_ttm = excluded.pe_ttm,
                pb = excluded.pb,
                total_mv = excluded.total_mv,
                circ_mv = excluded.circ_mv,
                roe = excluded.roe,
                gross_margin = excluded.gross_margin,
                net_profit_yoy = excluded.net_profit_yoy,
                debt_to_assets = excluded.debt_to_assets,
                updated_at = excluded.updated_at
        """)
        self.conn.commit()
        log.info(f"更新股票基本面数据: {len(df)} 条")

    def get_fundamentals_preview(self, limit: int = 100, search: str = "", min_roe: float = None, max_pe: float = None) -> pd.DataFrame:
        """获取基本面数据预览展示 (带搜索和过滤)"""
        try:
            query = """
                SELECT
                    f.code, s.name, s.sector, round(f.pe_ttm, 2) AS pe_ttm, round(f.pb, 2) AS pb,
                    round(f.total_mv, 2) AS total_mv, round(f.roe, 2) AS roe,
                    round(f.gross_margin, 2) AS gross_margin,
                    round(f.net_profit_yoy, 2) AS net_profit_yoy,
                    round(f.debt_to_assets, 2) AS debt_to_assets,
                    f.updated_at
                FROM stock_fundamentals f
                LEFT JOIN stocks s ON f.code = s.code
                WHERE 1=1
            """
            params = []
            if search:
                query += " AND (f.code LIKE ? OR s.name LIKE ? OR s.sector LIKE ?)"
                params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
            if min_roe is not None:
                query += " AND f.roe >= ?"
                params.append(min_roe)
            if max_pe is not None:
                query += " AND f.pe_ttm > 0 AND f.pe_ttm <= ?"
                params.append(max_pe)

            query += " ORDER BY f.total_mv DESC NULLS LAST LIMIT ?"
            params.append(limit)

            return self.conn.execute(query, params).df()
        except Exception as e:
            log.error(f"Fundamentals preview Error: {e}")
            return pd.DataFrame()

    def get_all_stocks(self, status: str = "active") -> pd.DataFrame:
        """获取所有股票清单"""
        return self.conn.execute(
            "SELECT * FROM stocks WHERE status = ?", [status]
        ).df()

    def get_stock_name_by_code(self, code: str) -> str:
        """根据股票代码获取股票简称"""
        code = code.replace(".SH", "").replace(".SZ", "").strip()
        try:
            result = self.conn.execute("SELECT name FROM stocks WHERE code = ?", [code]).fetchone()
            return result[0] if result else code
        except Exception as e:
            log.warning(f"Failed to get stock name for code {code}: {e}")
            return code

    def get_stocks_filtered(
        self,
        index_filter: list = None,   # e.g. ['HS300', 'ZZ500']
        min_mv: float = None,         # 亿元，内部转换为万元
        max_mv: float = None,
    ) -> pd.DataFrame:
        """
        统一股票池过滤入口（五维过滤）：
        1. status = 'active'
        2. ST 剔除（name 不含 ST/*ST/SST）
        3. 指数成分过滤（多选，OR 关系）
        4. 流通市值过滤（来自 stock_fundamentals.total_mv，万元）
        停牌/涨跌停在引擎信号层实时过滤。
        """
        import json
        try:
            sql = """
                SELECT s.*
                FROM stocks s
                LEFT JOIN stock_fundamentals f ON s.code = f.code
                WHERE s.status = 'active'
                  AND s.name NOT LIKE '%ST%'
            """
            params = []

            # 指数成分过滤
            if index_filter:
                placeholders = ",".join(["?"] * len(index_filter))
                sql += f"""
                  AND s.code IN (
                      SELECT stock_code FROM index_members
                      WHERE index_code IN ({placeholders})
                  )
                """
                params.extend(index_filter)

            # 市值过滤（前端输入亿元，DB存万元，×10000）
            if min_mv is not None:
                sql += " AND f.total_mv >= ?"
                params.append(min_mv * 10000)
            if max_mv is not None:
                sql += " AND f.total_mv <= ?"
                params.append(max_mv * 10000)

            df = self.conn.execute(sql, params).df()
            log.info(f"get_stocks_filtered | 过滤结果: {len(df)} 只 (index={index_filter}, mv=[{min_mv},{max_mv}]亿)")
            return df
        except Exception as e:
            log.error(f"get_stocks_filtered 异常: {e}")
            return self.get_all_stocks()

    def upsert_index_members(self, index_code: str, codes: list):
        """全量替换单个指数的成分股（先删后批量插入，分块插入支持千级条目）"""
        try:
            self.conn.execute("DELETE FROM index_members WHERE index_code = ?", [index_code])
            if codes:
                # 分块批量写入，避免单次 executemany 内存占用过加
                CHUNK = 500
                rows = [(index_code, c) for c in codes]
                for i in range(0, len(rows), CHUNK):
                    self.conn.executemany(
                        "INSERT OR IGNORE INTO index_members (index_code, stock_code) VALUES (?, ?)",
                        rows[i:i + CHUNK]
                    )
            self.conn.commit()
            log.info(f"upsert_index_members | {index_code}: {len(codes)} 只成分股已更新")
        except Exception as e:
            self.conn.rollback()
            log.error(f"upsert_index_members 失败: {e}")

    def get_index_member_status(self) -> list:
        """查询各指数在库成分股数量和最后更新时间"""
        try:
            rows = self.conn.execute("""
                SELECT index_code,
                       COUNT(*) AS count,
                       MAX(updated_at) AS last_updated
                FROM index_members
                GROUP BY index_code
                ORDER BY index_code
            """).fetchall()
            return [{"index_code": r[0], "count": r[1], "last_updated": str(r[2])} for r in rows]
        except Exception as e:
            log.error(f"get_index_member_status 失败: {e}")
            return []

    # ========== 回测历史持久化 ==========

    def save_backtest_history(
        self, strategy_name, start_date, end_date, exchanges, sectors,
        index_filter, min_mv, max_mv, risk_params,
        total_trades, win_rate, avg_pnl_pct, trades_json
    ) -> int:
        """保存基础回测历史，返回记录 ID"""
        import json
        try:
            result = self.conn.execute("""
                INSERT INTO backtest_history (
                    id, strategy_name, start_date, end_date, exchanges, sectors,
                    index_filter, min_mv, max_mv, risk_params,
                    total_trades, win_rate, avg_pnl_pct, trades_json
                ) VALUES (
                    nextval('seq_bt_hist'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                ) RETURNING id
            """, [
                strategy_name, start_date, end_date,
                json.dumps(exchanges or [], ensure_ascii=False),
                json.dumps(sectors or [], ensure_ascii=False),
                json.dumps(index_filter or [], ensure_ascii=False),
                min_mv, max_mv,
                risk_params if isinstance(risk_params, str) else json.dumps(risk_params or {}),
                total_trades, win_rate, avg_pnl_pct, trades_json
            ]).fetchone()
            self.conn.commit()
            return result[0] if result else -1
        except Exception as e:
            log.error(f"save_backtest_history 失败: {e}")
            return -1

    def list_backtest_history(self, limit: int = 20, offset: int = 0) -> pd.DataFrame:
        """分页获取基础回测历史列表（不含 trades_json）"""
        try:
            return self.conn.execute("""
                SELECT id, created_at, strategy_name, start_date, end_date,
                       index_filter, min_mv, max_mv, total_trades, win_rate, avg_pnl_pct
                FROM backtest_history
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, [limit, offset]).df()
        except Exception as e:
            log.error(f"list_backtest_history 失败: {e}")
            return pd.DataFrame()

    def get_backtest_history_detail(self, hist_id: int) -> dict:
        """获取基础回测历史详情（含 trades_json）"""
        try:
            cur = self.conn.execute(
                "SELECT * FROM backtest_history WHERE id = ?", [hist_id]
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, [str(v) if hasattr(v, 'isoformat') else v for v in row]))
        except Exception as e:
            log.error(f"get_backtest_history_detail({hist_id}) 失败: {e}")
            return None

    def delete_backtest_history(self, hist_id: int):
        """删除基础回测历史记录"""
        try:
            self.conn.execute("DELETE FROM backtest_history WHERE id = ?", [hist_id])
            self.conn.commit()
        except Exception as e:
            log.error(f"delete_backtest_history({hist_id}) 失败: {e}")

    def save_ai_backtest_history(
        self, strategy_name, start_date, end_date, exchanges, sectors,
        index_filter, min_mv, max_mv, use_llm, n_exploration, n_bayesian,
        best_avg_pnl, best_win_rate, best_params,
        top10_json, wfo_json, llm_report, regime_summary
    ) -> int:
        """保存 AI 回测历史，返回记录 ID"""
        import json
        try:
            result = self.conn.execute("""
                INSERT INTO ai_backtest_history (
                    id, strategy_name, start_date, end_date, exchanges, sectors,
                    index_filter, min_mv, max_mv, use_llm, n_exploration, n_bayesian,
                    best_avg_pnl, best_win_rate, best_params,
                    top10_json, wfo_json, llm_report, regime_summary
                ) VALUES (
                    nextval('seq_ai_bt_hist'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                ) RETURNING id
            """, [
                strategy_name, start_date, end_date,
                json.dumps(exchanges or [], ensure_ascii=False),
                json.dumps(sectors or [], ensure_ascii=False),
                json.dumps(index_filter or [], ensure_ascii=False),
                min_mv, max_mv, use_llm, n_exploration, n_bayesian,
                best_avg_pnl, best_win_rate,
                best_params if isinstance(best_params, str) else json.dumps(best_params or {}),
                top10_json, wfo_json, llm_report,
                regime_summary if isinstance(regime_summary, str) else json.dumps(regime_summary or {})
            ]).fetchone()
            self.conn.commit()
            return result[0] if result else -1
        except Exception as e:
            log.error(f"save_ai_backtest_history 失败: {e}")
            return -1

    def list_ai_backtest_history(self, limit: int = 20, offset: int = 0) -> pd.DataFrame:
        """分页获取 AI 回测历史列表（不含 top10/report 大字段）"""
        try:
            return self.conn.execute("""
                SELECT id, created_at, strategy_name, start_date, end_date,
                       exchanges, sectors, index_filter, min_mv, max_mv,
                       use_llm, n_exploration, n_bayesian,
                       best_avg_pnl, best_win_rate, best_params
                FROM ai_backtest_history
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, [limit, offset]).df()
        except Exception as e:
            log.error(f"list_ai_backtest_history 失败: {e}")
            return pd.DataFrame()

    def get_ai_backtest_history_detail(self, hist_id: int) -> dict:
        """获取 AI 回测历史详情（含 top10_json + llm_report 等大字段）"""
        try:
            cur = self.conn.execute(
                "SELECT * FROM ai_backtest_history WHERE id = ?", [hist_id]
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, [str(v) if hasattr(v, 'isoformat') else v for v in row]))
        except Exception as e:
            log.error(f"get_ai_backtest_history_detail({hist_id}) 失败: {e}")
            return None

    def delete_ai_backtest_history(self, hist_id: int):
        """删除 AI 回测历史记录"""
        try:
            self.conn.execute("DELETE FROM ai_backtest_history WHERE id = ?", [hist_id])
            self.conn.commit()
        except Exception as e:
            log.error(f"delete_ai_backtest_history({hist_id}) 失败: {e}")

    # ========== AI 分析报告持久化 ==========
    
    def save_ai_report(self, code: str, name: str, content: str) -> int:
        """保存单份分析报告到数据库，返回报告ID"""
        try:
            # 剥离代码后缀以保持数据库存储一致性
            clean_code = str(code).split('.')[0]
            result = self.conn.execute(
                "INSERT INTO ai_reports (id, code, name, content) VALUES (nextval('seq_ai_report'), ?, ?, ?) RETURNING id",
                [clean_code, name, content]
            ).fetchone()
            self.conn.commit()
            return result[0] if result else -1
        except Exception as e:
            log.error(f"Save AI report failed (Conflict?): {e}")
            # 如果冲突，可能是因为 id 手动干预过，再次尝试修复序列然后重试一次
            try:
                max_id = self.conn.execute("SELECT MAX(id) FROM ai_reports").fetchone()[0] or 0
                self.conn.execute("DROP SEQUENCE IF EXISTS seq_ai_report")
                self.conn.execute(f"CREATE SEQUENCE seq_ai_report START {max_id + 1}")
                result = self.conn.execute(
                    "INSERT INTO ai_reports (id, code, name, content) VALUES (nextval('seq_ai_report'), ?, ?, ?) RETURNING id",
                    [clean_code, name, content]
                ).fetchone()
                self.conn.commit()
                return result[0]
            except:
                log.error("AI 报告自动修复重试失败")
                return -1

    def get_ai_reports(self, limit: int = 10, offset: int = 0, search: str = "") -> dict:
        """获取AI报告列表，支持分页和标题检索。"""
        try:
            where_clause = "WHERE 1=1"
            params = []
            if search:
                # 剥离代码后缀进行匹配
                clean_search = str(search).split('.')[0]
                where_clause += " AND (code LIKE ? OR name LIKE ?)"
                params.extend([f"%{clean_search}%", f"%{search}%"])
                
            count_query = f"SELECT count(*) FROM ai_reports {where_clause}"
            total = self.conn.execute(count_query, params).fetchone()[0]
            
            query = f"""
                SELECT id, code, name, created_at
                FROM ai_reports 
                {where_clause}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """
            params.extend([limit, offset])
            df = self.conn.execute(query, params).df()
            
            # format date string
            if 'created_at' in df.columns:
                df['created_at'] = df['created_at'].astype(str)
                
            return {
                "total": total,
                "data": df.to_dict(orient="records")
            }
        except Exception as e:
            log.error(f"Get AI reports failed: {e}")
            return {"total": 0, "data": []}

    def get_ai_report_by_id(self, report_id: int) -> dict:
        """根据ID获取报告正文"""
        try:
            row = self.conn.execute(
                "SELECT id, code, name, content, created_at FROM ai_reports WHERE id = ?",
                [report_id]
            ).fetchone()
            if not row: return None
            return {
                "id": row[0], "code": row[1], "name": row[2], 
                "content": row[3], "created_at": str(row[4])
            }
        except Exception as e:
            log.error(f"Get AI report {report_id} failed: {e}")
            return None

    def get_stocks_by_sector(self, sector: str) -> pd.DataFrame:
        """按板块过滤股票"""
        return self.conn.execute(
            "SELECT * FROM stocks WHERE sector LIKE ? AND status='active'",
            [f"%{sector}%"]
        ).df()

    def mark_delisted(self, codes: list):
        """标记退市股票"""
        placeholders = ",".join(["?"] * len(codes))
        self.conn.execute(
            f"UPDATE stocks SET status='delisted', delist_date=TODAY() WHERE code IN ({placeholders})",
            codes
        )
        self.conn.commit()
        log.info(f"标记退市: {codes}")

    # ========== K 线数据（Parquet + DuckDB 扫描）==========

    def save_bars(self, code: str, df: pd.DataFrame, freq: str = "daily"):
        """将 K 线数据存为 Parquet (Pandas 版) 并附魔技术指标"""
        # 为了不循环引用，延迟在这个局部位置导入指标模块
        try:
            from app.data_manager.indicators import enrich_with_indicators
        except ImportError:
            enrich_with_indicators = lambda x: x
            
        base_dir = PARQUET_DAILY_DIR if freq == "daily" else PARQUET_MIN5_DIR
        path = base_dir / f"{code}.parquet"
        
        if path.exists():
            old_df = pd.read_parquet(path)
            merged = pd.concat([old_df, df])
            date_col = "date" if "date" in merged.columns else "datetime"
            # 去重并排序，技术指标的基础必须是完美的按时间正序的排列
            merged = merged.drop_duplicates(subset=[date_col], keep='last').sort_values(date_col)
            # 计算技术指标
            merged = enrich_with_indicators(merged)
            merged.to_parquet(path, index=False)
        else:
            date_col = "date" if "date" in df.columns else "datetime"
            df = df.drop_duplicates(subset=[date_col], keep='last').sort_values(date_col)
            # 计算技术指标
            df = enrich_with_indicators(df)
            df.to_parquet(path, index=False)

    def batch_save_bars(self, stocks_data: dict, freq: str = "daily"):
        """批量写入多只股票的 K 线数据 —— 先攒后写避免逐日 I/O 风暴

        Args:
            stocks_data: {code: DataFrame} 每只股票累积的全部新数据
            freq: "daily" 或 "min5"
        """
        try:
            from app.data_manager.indicators import enrich_with_indicators
        except ImportError:
            enrich_with_indicators = lambda x: x

        base_dir = PARQUET_DAILY_DIR if freq == "daily" else PARQUET_MIN5_DIR
        base_dir.mkdir(parents=True, exist_ok=True)

        for code, new_df in stocks_data.items():
            if new_df.empty:
                continue
            path = base_dir / f"{code}.parquet"
            date_col = "date" if "date" in new_df.columns else "datetime"

            if path.exists():
                old_df = pd.read_parquet(path)
                # 统一日期类型，避免 Timestamp vs date 比较出错
                if date_col in old_df.columns:
                    old_df[date_col] = pd.to_datetime(old_df[date_col])
                if date_col in new_df.columns:
                    new_df[date_col] = pd.to_datetime(new_df[date_col])
                merged = pd.concat([old_df, new_df])
                merged = merged.drop_duplicates(subset=[date_col], keep='last').sort_values(date_col)
                merged = enrich_with_indicators(merged)
                merged.to_parquet(path, index=False)
            else:
                new_df = new_df.drop_duplicates(subset=[date_col], keep='last').sort_values(date_col)
                new_df = enrich_with_indicators(new_df)
                new_df.to_parquet(path, index=False)

    def load_bars(
        self,
        code: str,
        freq: str = "daily",
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> pd.DataFrame:
        """加载单只股票 K 线数据 (Pandas 版)，支持指数（自动 fallback 到 index_{code}.parquet）"""
        # 兼容性处理：物理文件不带 .SH/.SZ 后缀
        clean_code = str(code).split('.')[0]
        base_dir = PARQUET_DAILY_DIR if freq == "daily" else PARQUET_MIN5_DIR
        path = base_dir / f"{clean_code}.parquet"
        if not path.exists():
            # 尝试指数文件（index_{code}.parquet）
            index_path = base_dir / f"index_{clean_code}.parquet"
            if index_path.exists():
                path = index_path
            else:
                log.warning(f"DuckDB | 数据文件不存在: {path} (also tried {index_path})")
                return pd.DataFrame()
        df = pd.read_parquet(path)
        date_col = "date" if "date" in df.columns else "datetime"
        if not df.empty:
            df[date_col] = pd.to_datetime(df[date_col])
            if start:
                df = df[df[date_col].dt.date >= start]
            if end:
                df = df[df[date_col].dt.date <= end]
        return df

    def load_all_bars(
        self,
        freq: str = "daily",
        start: Optional[date] = None,
        end: Optional[date] = None,
        codes: Optional[list] = None,
    ) -> pd.DataFrame:
        """加载批量 K 线数据 (DuckDB + Pandas 版)"""
        base_dir = PARQUET_DAILY_DIR if freq == "daily" else PARQUET_MIN5_DIR
        date_col = "date" if freq == "daily" else "datetime"

        # 如果指定了代码，生成精确路径列表，避免全市场扫描文件元数据
        if codes:
            # 过滤存在的路径
            file_paths = []
            for c in codes:
                p = base_dir / f"{c}.parquet"
                if p.exists():
                    file_paths.append(str(p)) # 移除了不必要的 .replace("\\", "/")
            
            if not file_paths:
                return pd.DataFrame()
            
            # 使用列表传参，让 DuckDB 自动解析各平台路径符
            source = str(file_paths)
        else:
            path_str = str(base_dir / '*.parquet')
            source = f"'{path_str}'"
            
        # 显式重置视图 (强制清空曾经残留的 Windows 路径关联视图)
        self.conn.execute("DROP VIEW IF EXISTS kline_daily")
        self.conn.execute("DROP VIEW IF EXISTS kline_min5")

        where_clauses = []
        params = []
        if start:
            where_clauses.append(f"{date_col} >= ?")
            params.append(start.isoformat())
        if end:
            where_clauses.append(f"{date_col} <= ?")
            params.append(end.isoformat())

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        
        # 使用 filename=true 来提取股票代码
        sql = f"""
            SELECT filename, * 
            FROM read_parquet({source}, filename=true, union_by_name=True)
            {where_sql}
        """
        try:
            df = self.conn.execute(sql, params).df()
            if not df.empty:
                # 提取代码：从路径中截取文件名部分
                df["code"] = df["filename"].str.extract(r"([^\\/]+)\.parquet$")
                df = df.drop(columns=["filename"])
                # 统一日期格式：确保 date 列是 datetime 类型方便 Pandas 处理
                df[date_col] = pd.to_datetime(df[date_col])
            return df
        except Exception as e:
            log.error(f"DuckDB | 批量加载 K 线失败: {e}")
            return pd.DataFrame()

    def get_last_date(self, code: str, freq: str = "daily") -> Optional[pd.Timestamp]:
        """获取最后一条日期，用于增量更新判断"""
        base_dir = PARQUET_DAILY_DIR if freq == "daily" else PARQUET_MIN5_DIR
        path = base_dir / f"{code}.parquet"
        if not path.exists():
            return None
        try:
            date_col = 'date' if freq == 'daily' else 'datetime'
            path_str = str(path).replace('\\', '/')
            sql = f"SELECT MAX({date_col}) FROM read_parquet(?, union_by_name=True)"
            res = self.conn.execute(sql, [path_str]).fetchone()
            if res and res[0]:
                return pd.to_datetime(res[0])
        except Exception as e:
            log.debug(f"DuckDB | 读取最后日期失败 {code}: {e}")
        return None

    def create_kline_view(self, freq: str = "daily"):
        """将目录下所有的 Parquet 映射为一个 DuckDB 虚拟视图，支持极限速度的跨标的全量聚合查询"""
        if freq not in ("daily", "min5"):
            log.warning(f"DuckDB | 不支持的频率: {freq}")
            return
        base_dir = PARQUET_DAILY_DIR if freq == "daily" else PARQUET_MIN5_DIR
        view_name = f"kline_{freq}_view"
        path_str = str(base_dir / '*.parquet').replace('\\', '/')

        try:
            self.conn.execute(f"DROP VIEW IF EXISTS {view_name}")
            sql = f"""
                CREATE VIEW {view_name} AS
                SELECT filename, *
                FROM read_parquet('{path_str}', filename=true, union_by_name=True)
            """
            self.conn.execute(sql)
            log.info(f"DuckDB | 创建虚拟视图成功: {view_name} -> {path_str}")
        except Exception as e:
            log.error(f"DuckDB | 创建虚拟视图失败: {e}")

    # ========== 策略管理 ==========

    def upsert_strategy(self, name: str, description: str, code_path: str, code_content: str = None, params: dict = None, is_active: bool = True):
        """插入或更新策略元数据"""
        self.conn.execute("""
            INSERT INTO strategies (id, name, description, code_path, code_content, params, is_active)
            VALUES (nextval('seq_strategies'), ?, ?, ?, ?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET
                description = excluded.description,
                code_path = excluded.code_path,
                code_content = excluded.code_content,
                params = excluded.params,
                is_active = excluded.is_active
        """, [name, description, code_path, code_content, str(params or {}), is_active])
        self.conn.commit()

    def get_strategies(self, active_only: bool = False) -> pd.DataFrame:
        """获取策略清单"""
        sql = "SELECT * FROM strategies"
        if active_only:
            sql += " WHERE is_active=TRUE"
        return self.conn.execute(sql + " ORDER BY id").df()

    def set_strategy_status(self, strategy_id: int, is_active: bool):
        """切换策略状态（有效/废弃）"""
        self.conn.execute("UPDATE strategies SET is_active=? WHERE id=?", [is_active, strategy_id])
        self.conn.commit()

    def delete_strategy(self, strategy_id: int):
        """彻底删除（物理删除）"""
        self.conn.execute("DELETE FROM strategies WHERE id=?", [strategy_id])
        self.conn.commit()

    # ========== 舆情与热点 ==========

    def save_sentiment_analysis(self, raw_news: list, ai_analysis: str, extracted_concepts: list):
        import json
        self.conn.execute("""
            INSERT INTO sentiment_history (id, trigger_time, raw_news, ai_analysis, extracted_concepts)
            VALUES (nextval('seq_sentiment'), NOW(), ?, ?, ?)
        """, [json.dumps(raw_news, ensure_ascii=False), ai_analysis, json.dumps(extracted_concepts, ensure_ascii=False)])
        self.conn.commit()

    def get_latest_sentiment(self) -> dict:
        import json
        row = self.conn.execute("""
            SELECT id, trigger_time, raw_news, ai_analysis, extracted_concepts 
            FROM sentiment_history ORDER BY trigger_time DESC LIMIT 1
        """).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "trigger_time": row[1].strftime("%Y-%m-%d %H:%M:%S") if hasattr(row[1], 'strftime') else row[1],
            "raw_news": json.loads(row[2]) if row[2] else [],
            "ai_analysis": row[3],
            "extracted_concepts": json.loads(row[4]) if row[4] else []
        }

    def get_sentiment_history(self, limit: int = 50) -> pd.DataFrame:
        return self.conn.execute(
            "SELECT id, trigger_time, json_array_length(extracted_concepts) as concepts_count FROM sentiment_history ORDER BY trigger_time DESC LIMIT ?", [limit]
        ).df()

    def get_sentiment_by_id(self, sid: int) -> dict:
        import json
        row = self.conn.execute("""
            SELECT id, trigger_time, raw_news, ai_analysis, extracted_concepts 
            FROM sentiment_history WHERE id = ?
        """, [sid]).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "trigger_time": row[1].strftime("%Y-%m-%d %H:%M:%S") if hasattr(row[1], 'strftime') else row[1],
            "raw_news": json.loads(row[2]) if row[2] else [],
            "ai_analysis": row[3],
            "extracted_concepts": json.loads(row[4]) if row[4] else []
        }

    # ========== 选股历史 ==========

    def save_scan_result(self, strategy_id: int, strategy_name: str, codes: list, params: dict):
        import json
        self.conn.execute("""
            INSERT INTO scan_history (id, strategy_id, strategy_name, scan_time, params, stock_codes, result_count)
            VALUES (nextval('seq_scan'), ?, ?, NOW(), ?, ?, ?)
        """, [strategy_id, strategy_name, json.dumps(params), json.dumps(codes), len(codes)])
        self.conn.commit()

    def get_scan_history(self, limit: int = 50) -> pd.DataFrame:
        return self.conn.execute(
            "SELECT * FROM scan_history ORDER BY scan_time DESC LIMIT ?", [limit]
        ).df()

    # ========== 持仓与交易记录 ==========

    def open_position(self, code, name, price, volume, source, strategy_id=None) -> int:
        result = self.conn.execute("""
            INSERT INTO positions (id, code, name, open_price, open_time, volume, remain_volume, cost, source, strategy_id)
            VALUES (nextval('seq_position'), ?, ?, ?, NOW(), ?, ?, ?, ?, ?) RETURNING id
        """, [code, name, price, volume, volume, price * volume, source, strategy_id]).fetchone()
        self.conn.commit()
        return result[0]

    def update_position_highest(self, position_id: int, highest: float, activated: bool):
        self.conn.execute("""
            UPDATE positions SET highest_price=?, trailing_activated=? WHERE id=?
        """, [highest, activated, position_id])
        self.conn.commit()

    def reduce_position(self, position_id: int, volume_sold: int):
        self.conn.execute("""
            UPDATE positions SET remain_volume = remain_volume - ?
            WHERE id = ?
        """, [volume_sold, position_id])
        self.conn.execute("""
            UPDATE positions SET status = 'closed'
            WHERE id = ? AND remain_volume <= 0
        """, [position_id])
        self.conn.commit()

    def get_open_positions(self) -> pd.DataFrame:
        return self.conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY open_time DESC"
        ).df()

    def record_trade(self, position_id, code, name, direction, price, volume, trade_type, reason, strategy_name=None):
        self.conn.execute("""
            INSERT INTO trade_history (id, position_id, code, name, direction, price, volume, amount, trade_time, trade_type, reason, strategy_name)
            VALUES (nextval('seq_trade'), ?, ?, ?, ?, ?, ?, ?, NOW(), ?, ?, ?)
        """, [position_id, code, name, direction, price, volume, price * volume, trade_type, reason, strategy_name])
        self.conn.commit()

    def get_trade_history(self, limit: int = 200) -> pd.DataFrame:
        return self.conn.execute(
            "SELECT * FROM trade_history ORDER BY trade_time DESC LIMIT ?", [limit]
        ).df()

    # ========== 自选股 (Watchlist) ==========

    def add_to_watchlist(self, code: str, name: str = None, source: str = "manual"):
        """添加至自选股库"""
        try:
            self.conn.execute("""
                INSERT INTO user_watchlist (code, added_at, source)
                VALUES (?, NOW(), ?)
                ON CONFLICT (code) DO UPDATE SET 
                    added_at = NOW(), 
                    source = excluded.source
            """, [code, source])
            self.conn.commit()
            log.info(f"Watchlist | [+加入] {code} ({name}) [来源: {source}]")
        except Exception as e:
            log.error(f"Watchlist | 加入失败 {code}: {e}")

    def remove_from_watchlist(self, code: str):
        try:
            # 提取纯数字部分 (如 600519.SH -> 600519) 进行模糊删除，确保彻底移除
            naked_code = str(code).split('.')[0]
            self.conn.execute("DELETE FROM user_watchlist WHERE code = ? OR code = ?", [code, naked_code])
            self.conn.commit()
            log.info(f"Watchlist | [-移除] {code}")
        except Exception as e:
            log.error(f"Watchlist | 移除失败 {code}: {e}")

    def get_watchlist(self, limit: int = 50, offset: int = 0) -> pd.DataFrame:
        """支持分页获取自选股伴随元数据 (兼容带后缀的代码关联)"""
        # 使用 split_part(w.code, '.', 1) 剥离后缀与 stocks.code (裸代码) 关联
        query = """
            SELECT w.code, s.name AS name, s.sector, w.added_at, w.source
            FROM user_watchlist w
            LEFT JOIN stocks s ON split_part(w.code, '.', 1) = s.code
            ORDER BY w.added_at DESC
            LIMIT ? OFFSET ?
        """
        try:
            return self.conn.execute(query, [limit, offset]).df()
        except Exception as e:
            log.error(f"get_watchlist 异常: {e}")
            return pd.DataFrame()

    # ========== 通达信选股历史 ==========

    def save_tqsdk_screen_history(self, formula_name: str, formula_arg: str,
                                   start_date: str, end_date: str,
                                   stock_count: int, stock_codes: list,
                                   stock_details: list) -> int:
        import json
        result = self.conn.execute("""
            INSERT INTO tqsdk_screen_history (id, formula_name, formula_arg, start_date, end_date,
                                             stock_count, stock_codes, stock_details)
            VALUES (nextval('seq_tqsdk_screen'), ?, ?, ?, ?, ?, ?, ?) RETURNING id
        """, [formula_name, formula_arg, start_date, end_date,
              stock_count, json.dumps(stock_codes), json.dumps(stock_details)])
        self.conn.commit()
        return result.fetchone()[0]

    def list_tqsdk_screen_history(self, limit: int = 50, offset: int = 0) -> pd.DataFrame:
        return self.conn.execute("""
            SELECT id, formula_name, formula_arg, start_date, end_date,
                   stock_count, executed_at
            FROM tqsdk_screen_history
            ORDER BY executed_at DESC
            LIMIT ? OFFSET ?
        """, [limit, offset]).df()

    def get_tqsdk_screen_history_detail(self, hist_id: int) -> dict:
        row = self.conn.execute("""
            SELECT * FROM tqsdk_screen_history WHERE id = ?
        """, [hist_id]).fetchone()
        if not row:
            return None
        import json
        col_names = [desc[0] for desc in self.conn.description]
        d = dict(zip(col_names, row))
        for k in ('stock_codes', 'stock_details'):
            val = d.get(k)
            if isinstance(val, str):
                d[k] = json.loads(val)
        return d

    def delete_tqsdk_screen_history(self, hist_id: int):
        self.conn.execute("DELETE FROM tqsdk_screen_history WHERE id = ?", [hist_id])
        self.conn.commit()

    def close_all(self):
        """关闭所有线程的数据库连接"""
        with self._conn_lock:
            for tid, conn in self._connections.items():
                try:
                    conn.close()
                except Exception as e:
                    log.warning(f"关闭线程 {tid} 连接失败: {e}")
            self._connections.clear()

    # ========== 热点板块 & 概念 ==========

    def upsert_concept_stocks(self, df: pd.DataFrame):
        """批量写入概念↔股票映射"""
        source = df['source'].iloc[0] if 'source' in df.columns else 'tushare'
        self.conn.execute("DELETE FROM concept_stocks WHERE source=?", [source])
        self.conn.executemany(
            "INSERT INTO concept_stocks (concept_name, stock_code, source, updated_at) VALUES (?, ?, ?, NOW())",
            df[['concept_name', 'stock_code', 'source']].values.tolist()
        )
        self.conn.commit()

    def upsert_concept_heat(self, df: pd.DataFrame):
        """批量写入概念热度"""
        self.conn.executemany("""
            INSERT INTO concept_heat (concept_name, trade_date, hotness, constituent_count, advance_count, decline_count, avg_change_pct, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NOW())
            ON CONFLICT (concept_name, trade_date) DO UPDATE SET
                hotness = excluded.hotness,
                constituent_count = excluded.constituent_count,
                advance_count = excluded.advance_count,
                decline_count = excluded.decline_count,
                avg_change_pct = excluded.avg_change_pct,
                updated_at = NOW()
        """, df[['concept_name', 'trade_date', 'hotness', 'constituent_count', 'advance_count', 'decline_count', 'avg_change_pct']].values.tolist())
        self.conn.commit()

    def upsert_sector_heat(self, df: pd.DataFrame):
        """批量写入行业板块热度"""
        self.conn.executemany("""
            INSERT INTO sector_heat (sector_name, trade_date, hotness, constituent_count, advance_count, decline_count, avg_change_pct, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NOW())
            ON CONFLICT (sector_name, trade_date) DO UPDATE SET
                hotness = excluded.hotness,
                constituent_count = excluded.constituent_count,
                advance_count = excluded.advance_count,
                decline_count = excluded.decline_count,
                avg_change_pct = excluded.avg_change_pct,
                updated_at = NOW()
        """, df[['sector_name', 'trade_date', 'hotness', 'constituent_count', 'advance_count', 'decline_count', 'avg_change_pct']].values.tolist())
        self.conn.commit()

    def get_concept_stocks(self, concept_name: str = None) -> pd.DataFrame:
        """获取概念成分股，可选按概念名筛选"""
        if concept_name:
            return self.conn.execute(
                "SELECT stock_code FROM concept_stocks WHERE concept_name = ?", [concept_name]
            ).df()
        return self.conn.execute("SELECT * FROM concept_stocks").df()

    def get_stock_concepts(self, stock_code: str) -> list:
        """获取单只股票所属的所有概念名称列表"""
        rows = self.conn.execute(
            "SELECT concept_name FROM concept_stocks WHERE stock_code = ?", [stock_code]
        ).fetchall()
        return [r[0] for r in rows]

    def get_distinct_concepts(self) -> list:
        """获取所有去重概念名"""
        rows = self.conn.execute("SELECT DISTINCT concept_name FROM concept_stocks ORDER BY concept_name").fetchall()
        return [r[0] for r in rows]

    def get_distinct_sectors(self) -> list:
        """获取所有去重行业板块名（来自 stocks 表）"""
        rows = self.conn.execute(
            "SELECT DISTINCT sector FROM stocks WHERE status = 'active' AND sector IS NOT NULL AND sector != '' ORDER BY sector"
        ).fetchall()
        return [r[0] for r in rows]

    def close(self):
        """关闭当前线程的数据库连接（兼容旧调用方）"""
        tid = threading.current_thread().ident
        if tid in self._connections:
            try:
                self._connections[tid].close()
            except Exception as e:
                log.warning(f"关闭当前线程连接失败: {e}")
            finally:
                with self._conn_lock:
                    self._connections.pop(tid, None)


# 全局单例
db = DatabaseManager()
