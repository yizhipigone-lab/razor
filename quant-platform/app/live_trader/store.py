"""实盘交易模块数据层(v5.4 §6 / §18.6 两层 buffer)

独立 DuckDB 文件 data/live_trader/live_trader.duckdb,与 quant.duckdb 物理隔离。
两层 buffer:L1 deque(maxlen=2000) + L2 单线程 flusher(100ms)。
终态订单(53/54/56/57)同步落盘不走 buffer(H2 防丢)。
"""
import json
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional

import duckdb

from core.logger import get_logger

from .config import LiveTraderConfig
from .schemas import LiveDeal, LiveOrder, LivePosition
from app.utils.xtquant_compat import format_code

logger = get_logger("live_trader.store")


class LiveTraderStore:
    """实盘数据存储(DuckDB + 两层 buffer)"""

    def __init__(self, config: LiveTraderConfig):
        self.config = config
        os.makedirs(os.path.dirname(config.db_path), exist_ok=True)

        # L1 buffer(callback 写入,< 100μs)
        self._buffer: deque = deque(maxlen=config.buffer_maxlen)
        self._buffer_lock = threading.Lock()

        # L2 单线程 flusher
        self._flusher = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-flusher")
        self._stop_event = threading.Event()
        self._db_lock = threading.Lock()  # DuckDB 写串行化

        # WAL 文件(终态同步落盘 + 强退前紧急 flush,H2)
        self._wal_lock = threading.Lock()

        self._conn: Optional[duckdb.DuckDBPyConnection] = None
        self._connect()
        self._init_schema()
        self._start_flusher()

    def _connect(self) -> None:
        """连接 DuckDB(单写多读)"""
        self._conn = duckdb.connect(self.config.db_path, read_only=False)
        logger.info(f"DuckDB 连接: {self.config.db_path}")

    def _init_schema(self) -> None:
        """建表(对应 §6 数据模型)"""
        assert self._conn is not None
        con = self._conn
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_orders (
                order_id BIGINT PRIMARY KEY,
                client_order_id VARCHAR UNIQUE,
                code VARCHAR, direction VARCHAR, volume BIGINT, price DOUBLE,
                price_type INTEGER, status INTEGER, status_msg VARCHAR, seq BIGINT,
                mode VARCHAR, strategy_name VARCHAR, order_remark VARCHAR, terminal VARCHAR,
                created_at TIMESTAMP, updated_at TIMESTAMP, finished_at TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_deals (
                trade_id BIGINT PRIMARY KEY,
                order_id BIGINT, code VARCHAR, direction VARCHAR,
                filled_volume BIGINT, filled_price DOUBLE, filled_amount DOUBLE,
                commission DOUBLE, mode VARCHAR, traded_at TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_positions (
                code VARCHAR PRIMARY KEY,
                volume BIGINT, can_use_volume BIGINT, frozen_volume BIGINT,
                pending_buy_volume BIGINT, avg_cost DOUBLE, last_price DOUBLE,
                market_value DOUBLE, float_profit DOUBLE, profit_rate DOUBLE,
                peak_price DOUBLE, sell_count INTEGER, entry_date DATE,
                managed BOOLEAN, strategy_name VARCHAR
            )
        """)
        con.execute("CREATE SEQUENCE IF NOT EXISTS audit_seq START 1")
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_positions_audit (
                id BIGINT PRIMARY KEY DEFAULT nextval('audit_seq'),
                timestamp TIMESTAMP, code VARCHAR,
                local_volume BIGINT, qmt_volume BIGINT,
                diff_volume BIGINT, diff_value DOUBLE,
                source VARCHAR, resolved BOOLEAN DEFAULT FALSE
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_holdings_backup (
                backup_date DATE, code VARCHAR, volume BIGINT, can_use_volume BIGINT,
                avg_cost DOUBLE, last_price DOUBLE, market_value DOUBLE,
                daily_high DOUBLE, daily_low DOUBLE, managed BOOLEAN,
                PRIMARY KEY (backup_date, code)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_deals_backup (
                backup_date DATE, trade_id BIGINT, order_id BIGINT, code VARCHAR,
                direction VARCHAR, filled_volume BIGINT, filled_price DOUBLE,
                filled_amount DOUBLE, commission DOUBLE, mode VARCHAR, traded_at TIMESTAMP,
                PRIMARY KEY (backup_date, trade_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_assets_backup (
                backup_date DATE, backup_time VARCHAR, cash DOUBLE, frozen_cash DOUBLE,
                market_value DOUBLE, total_asset DOUBLE,
                PRIMARY KEY (backup_date, backup_time)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_audit (
                id TIMESTAMP PRIMARY KEY DEFAULT current_timestamp,
                action VARCHAR, code VARCHAR, order_id BIGINT,
                gate_result VARCHAR, reason VARCHAR, snapshot VARCHAR
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_killswitch (
                id INTEGER PRIMARY KEY DEFAULT 1,
                activated BOOLEAN DEFAULT FALSE,
                activated_at TIMESTAMP, reason VARCHAR, source VARCHAR, released_at TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_equity (
                equity_date DATE PRIMARY KEY,
                total_equity DOUBLE, cash DOUBLE, market_value DOUBLE,
                source VARCHAR, algo VARCHAR DEFAULT 'simple'
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_cycles (
                cycle_id INTEGER, code VARCHAR, status VARCHAR,
                buy_avg_price DOUBLE, sell_avg_price DOUBLE,
                pnl DOUBLE, pnl_pct DOUBLE,
                PRIMARY KEY (cycle_id, code)
            )
        """)
        # 信号心跳表(v1.2.2 §5.2 + §10.6 索引)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_signal_heartbeat (
                id BIGINT PRIMARY KEY DEFAULT nextval('audit_seq'),
                signal_date DATE NOT NULL,
                source VARCHAR NOT NULL,
                signal_count INTEGER NOT NULL,
                scan_status VARCHAR NOT NULL,
                received_at TIMESTAMP DEFAULT current_timestamp
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_heartbeat_date
            ON live_signal_heartbeat(signal_date, received_at DESC)
        """)
        logger.info("DuckDB schema 初始化完成")

    # ===== L1 buffer 写入(callback 调用,< 100μs)=====

    def buffer_order_update(self, order: Dict[str, Any]) -> None:
        """缓冲委托更新(L1)"""
        with self._buffer_lock:
            self._buffer.append(("order", order))

    def buffer_deal_insert(self, deal: Dict[str, Any]) -> None:
        """缓冲成交插入(L1)"""
        with self._buffer_lock:
            self._buffer.append(("deal", deal))

    def sync_terminal_write(self, kind: str, data: Dict[str, Any]) -> None:
        """终态同步落盘(不走 buffer,H2 防丢)

        终态订单(53/54/56/57)和成交必须同步落盘,防 os._exit 丢数据。
        同时写 WAL 文件,重启可从 WAL 补。
        """
        self._write_wal(kind, data)
        with self._db_lock:
            self._write_to_db(kind, data)

    def _write_wal(self, kind: str, data: Dict[str, Any]) -> None:
        """写 WAL append-only 文件(H2)"""
        try:
            with self._wal_lock:
                with open(self.config.wal_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"kind": kind, "data": data, "ts": time.time()}, default=str) + "\n")
        except Exception as e:
            logger.error(f"WAL 写入失败: {e}")

    # ===== L2 flusher(单线程,100ms 批量写)=====

    def _start_flusher(self) -> None:
        def _flush_loop():
            while not self._stop_event.is_set():
                if self._buffer:
                    self._flush_buffer()
                self._stop_event.wait(self.config.buffer_flush_interval_ms / 1000.0)
            # 退出前最后 flush 一次
            self._flush_buffer()

        self._flusher.submit(_flush_loop)
        logger.info("L2 flusher 启动")

    def _flush_buffer(self) -> None:
        """排空 L1 buffer 批量写 DuckDB"""
        if not self._buffer:
            return
        with self._buffer_lock:
            items = list(self._buffer)
            self._buffer.clear()
        if not items:
            return
        with self._db_lock:
            for kind, data in items:
                try:
                    self._write_to_db(kind, data)
                except Exception as e:
                    logger.error(f"flush 写入失败 kind={kind}: {e}")

    def _write_to_db(self, kind: str, data: Dict[str, Any]) -> None:
        """实际写 DuckDB(入库前统一 format_code,防 code 格式不一致)"""
        assert self._conn is not None
        # 漏洞B修复:所有含 code 字段的数据入库前统一格式化
        if "code" in data and data["code"]:
            data = {**data, "code": format_code(data["code"])}
        try:
            if kind == "order":
                self._upsert_order(data)
            elif kind == "deal":
                self._insert_deal(data)
            elif kind == "position":
                self._upsert_position(data)
        except Exception as e:
            logger.error(f"DB 写入失败 kind={kind} data={data}: {e}")

    def _upsert_order(self, data: Dict[str, Any]) -> None:
        assert self._conn is not None
        self._conn.execute("""
            INSERT INTO live_orders
            (order_id, client_order_id, code, direction, volume, price, price_type,
             status, status_msg, seq, mode, strategy_name, order_remark, terminal,
             created_at, updated_at, finished_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(order_id) DO UPDATE SET
                client_order_id=excluded.client_order_id, code=excluded.code,
                direction=excluded.direction, volume=excluded.volume, price=excluded.price,
                price_type=excluded.price_type, status=excluded.status,
                status_msg=excluded.status_msg, seq=excluded.seq, mode=excluded.mode,
                strategy_name=excluded.strategy_name, order_remark=excluded.order_remark,
                terminal=excluded.terminal, updated_at=excluded.updated_at,
                finished_at=excluded.finished_at
        """, [
            data.get("order_id"), data.get("client_order_id"), data.get("code"),
            data.get("direction"), data.get("volume"), data.get("price"),
            data.get("price_type"), data.get("status"), data.get("status_msg", ""),
            data.get("seq", 0), data.get("mode", "dry-run"),
            data.get("strategy_name", ""), data.get("order_remark", ""),
            data.get("terminal", "SYS"), data.get("created_at"),
            data.get("updated_at"), data.get("finished_at"),
        ])

    def _insert_deal(self, data: Dict[str, Any]) -> None:
        assert self._conn is not None
        self._conn.execute("""
            INSERT INTO live_deals
            (trade_id, order_id, code, direction, filled_volume, filled_price,
             filled_amount, commission, mode, traded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_id) DO UPDATE SET
                order_id=excluded.order_id, code=excluded.code,
                direction=excluded.direction, filled_volume=excluded.filled_volume,
                filled_price=excluded.filled_price, filled_amount=excluded.filled_amount,
                commission=excluded.commission, mode=excluded.mode, traded_at=excluded.traded_at
        """, [
            data.get("trade_id"), data.get("order_id"), data.get("code"),
            data.get("direction"), data.get("filled_volume"), data.get("filled_price"),
            data.get("filled_amount"), data.get("commission", 0.0),
            data.get("mode", "dry-run"), data.get("traded_at"),
        ])

    def _upsert_position(self, data: Dict[str, Any]) -> None:
        assert self._conn is not None
        self._conn.execute("""
            INSERT INTO live_positions
            (code, volume, can_use_volume, frozen_volume, pending_buy_volume,
             avg_cost, last_price, market_value, float_profit, profit_rate,
             peak_price, sell_count, entry_date, managed, strategy_name)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                volume=excluded.volume, can_use_volume=excluded.can_use_volume,
                frozen_volume=excluded.frozen_volume,
                pending_buy_volume=excluded.pending_buy_volume,
                avg_cost=excluded.avg_cost, last_price=excluded.last_price,
                market_value=excluded.market_value, float_profit=excluded.float_profit,
                profit_rate=excluded.profit_rate, peak_price=excluded.peak_price,
                sell_count=excluded.sell_count, entry_date=excluded.entry_date,
                managed=excluded.managed, strategy_name=excluded.strategy_name
        """, [
            data.get("code"), data.get("volume", 0), data.get("can_use_volume", 0),
            data.get("frozen_volume", 0), data.get("pending_buy_volume", 0),
            data.get("avg_cost", 0.0), data.get("last_price", 0.0),
            data.get("market_value", 0.0), data.get("float_profit", 0.0),
            data.get("profit_rate", 0.0), data.get("peak_price", 0.0),
            data.get("sell_count", 0), data.get("entry_date"),
            data.get("managed", True), data.get("strategy_name", ""),
        ])

    # ===== 查询 =====

    def get_order_by_seq(self, seq: int) -> Optional[Dict[str, Any]]:
        """按 seq 查委托(下单时用 seq 做临时 order_id,callback 需反查)"""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM live_orders WHERE seq = ?", [seq]
        ).fetchone()
        if not rows:
            return None
        cols = [d[0] for d in self._conn.description]
        return dict(zip(cols, rows))

    def update_order_id(self, old_id: int, new_id: int) -> bool:
        """将临时 order_id(seq) 更新为 QMT 真实 order_id"""
        assert self._conn is not None
        with self._db_lock:
            result = self._conn.execute(
                "UPDATE live_orders SET order_id = ? WHERE order_id = ?",
                [new_id, old_id]
            )
            return result.fetchone()[0] > 0 if result.fetchone() else False

    def get_order_by_client_id(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        """C3 幂等检查:按 client_order_id 查"""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM live_orders WHERE client_order_id = ?",
            [client_order_id]
        ).fetchone()
        if not rows:
            return None
        cols = [d[0] for d in self._conn.description]
        return dict(zip(cols, rows))

    def get_order(self, order_id: int) -> Optional[Dict[str, Any]]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM live_orders WHERE order_id = ?", [order_id]
        ).fetchone()
        if not rows:
            return None
        cols = [d[0] for d in self._conn.description]
        return dict(zip(cols, rows))

    def get_inflight_orders(self) -> List[Dict[str, Any]]:
        """获取在途订单(非终态,§17.1 启动恢复)"""
        assert self._conn is not None
        from app.utils.xtquant_compat import ORDER_STATUS_TERMINAL
        placeholders = ",".join("?" * len(ORDER_STATUS_TERMINAL))
        rows = self._conn.execute(
            f"SELECT * FROM live_orders WHERE status NOT IN ({placeholders})",
            list(ORDER_STATUS_TERMINAL)
        ).fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._conn.description]
        return [dict(zip(cols, r)) for r in rows]

    def get_positions(self, managed_only: bool = False) -> List[Dict[str, Any]]:
        assert self._conn is not None
        if managed_only:
            rows = self._conn.execute(
                "SELECT * FROM live_positions WHERE managed = TRUE"
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM live_positions").fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._conn.description]
        return [dict(zip(cols, r)) for r in rows]

    def get_position(self, code: str) -> Optional[Dict[str, Any]]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM live_positions WHERE code = ?", [code]
        ).fetchone()
        if not rows:
            return None
        cols = [d[0] for d in self._conn.description]
        return dict(zip(cols, rows))

    def get_deals(self, code: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        """获取成交(盈亏重算数据源,§18.7 内存缓存)"""
        assert self._conn is not None
        if code:
            rows = self._conn.execute(
                "SELECT * FROM live_deals WHERE code = ? ORDER BY traded_at DESC LIMIT ?",
                [code, limit]
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM live_deals ORDER BY traded_at DESC LIMIT ?", [limit]
            ).fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._conn.description]
        return [dict(zip(cols, r)) for r in rows]

    def upsert_position(self, pos: Dict[str, Any]) -> None:
        """直接更新持仓(非 buffer 路径,如启动接管)"""
        # 漏洞B修复:入库前统一 format_code
        if "code" in pos and pos["code"]:
            pos = {**pos, "code": format_code(pos["code"])}
        with self._db_lock:
            self._upsert_position(pos)

    def insert_audit(self, action: str, code: str = "", order_id: Optional[int] = None,
                     gate_result: str = "", reason: str = "", snapshot: str = "") -> None:
        """审计日志(每笔决策可回放)"""
        assert self._conn is not None
        # 漏洞B修复:审计记录也统一 code 格式
        if code:
            code = format_code(code)
        with self._db_lock:
            self._conn.execute("""
                INSERT INTO live_audit (action, code, order_id, gate_result, reason, snapshot)
                VALUES (?,?,?,?,?,?)
            """, [action, code, order_id, gate_result, reason, snapshot])

    def get_killswitch(self) -> Dict[str, Any]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM live_killswitch WHERE id = 1"
        ).fetchone()
        if not rows:
            return {"activated": False}
        cols = [d[0] for d in self._conn.description]
        return dict(zip(cols, rows))

    def set_killswitch(self, activated: bool, reason: str = "", source: str = "",
                       activated_at: Optional[datetime] = None) -> None:
        assert self._conn is not None
        with self._db_lock:
            self._conn.execute("""
                INSERT INTO live_killswitch
                (id, activated, activated_at, reason, source, released_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    activated=excluded.activated, activated_at=excluded.activated_at,
                    reason=excluded.reason, source=excluded.source, released_at=excluded.released_at
            """, [activated, activated_at, reason, source,
                  datetime.now() if not activated else None])

    def get_open_asset(self) -> Optional[float]:
        """获取当日开盘总资产(从 live_assets_backup 首条,§16.4 闸门5a 基准)"""
        assert self._conn is not None
        from datetime import date as date_type
        today = date_type.today()
        row = self._conn.execute(
            "SELECT total_asset FROM live_assets_backup "
            "WHERE backup_date = ? ORDER BY backup_time ASC LIMIT 1",
            [today]
        ).fetchone()
        if row and row[0] is not None:
            return float(row[0])
        return None

    def backup_asset(self, asset_data: Dict[str, Any]) -> None:
        """写入资产快照到 live_assets_backup(闸门5a 基准 + EOD 归档)"""
        assert self._conn is not None
        from datetime import date as date_type
        today = date_type.today()
        now_str = datetime.now().strftime("%H:%M")
        with self._db_lock:
            self._conn.execute("""
                INSERT INTO live_assets_backup
                (backup_date, backup_time, cash, frozen_cash, market_value, total_asset)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(backup_date, backup_time) DO UPDATE SET
                    cash=excluded.cash, frozen_cash=excluded.frozen_cash,
                    market_value=excluded.market_value, total_asset=excluded.total_asset
            """, [
                today, now_str,
                asset_data.get("cash", 0), asset_data.get("frozen_cash", 0),
                asset_data.get("market_value", 0), asset_data.get("total_asset", 0),
            ])

    def eod_archive(self, qmt_wrapper=None) -> None:
        """EOD 归档:持仓/成交/资产写入 backup 表(§6 EOD 归档)"""
        assert self._conn is not None
        from datetime import date as date_type
        today = date_type.today()
        with self._db_lock:
            # 持仓归档
            positions = self._conn.execute("SELECT * FROM live_positions").fetchall()
            if positions:
                cols = [d[0] for d in self._conn.description]
                for row in positions:
                    p = dict(zip(cols, row))
                    self._conn.execute("""
                        INSERT INTO live_holdings_backup
                        (backup_date, code, volume, can_use_volume, avg_cost, last_price,
                         market_value, daily_high, daily_low, managed)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(backup_date, code) DO UPDATE SET
                            volume=excluded.volume, can_use_volume=excluded.can_use_volume,
                            avg_cost=excluded.avg_cost, last_price=excluded.last_price,
                            market_value=excluded.market_value, managed=excluded.managed
                    """, [
                        today, p.get("code"), p.get("volume", 0),
                        p.get("can_use_volume", 0), p.get("avg_cost", 0),
                        p.get("last_price", 0), p.get("market_value", 0),
                        p.get("last_price", 0), p.get("last_price", 0),  # daily_high/low 简化
                        p.get("managed", True),
                    ])
            # 成交归档
            deals = self._conn.execute("SELECT * FROM live_deals").fetchall()
            if deals:
                cols = [d[0] for d in self._conn.description]
                for row in deals:
                    d = dict(zip(cols, row))
                    self._conn.execute("""
                        INSERT INTO live_deals_backup
                        (backup_date, trade_id, order_id, code, direction,
                         filled_volume, filled_price, filled_amount, commission, mode, traded_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(backup_date, trade_id) DO UPDATE SET
                            filled_volume=excluded.filled_volume,
                            filled_price=excluded.filled_price,
                            filled_amount=excluded.filled_amount
                    """, [
                        today, d.get("trade_id"), d.get("order_id"), d.get("code"),
                        d.get("direction"), d.get("filled_volume"), d.get("filled_price"),
                        d.get("filled_amount"), d.get("commission", 0),
                        d.get("mode", "dry-run"), d.get("traded_at"),
                    ])
        # 资产归档(用 QMT 实时数据)
        if qmt_wrapper and qmt_wrapper.connected:
            try:
                asset_data = qmt_wrapper.query_asset()
                if asset_data:
                    self.backup_asset(asset_data)
            except Exception as e:
                logger.error(f"EOD 资产归档失败: {e}")
        logger.info(f"EOD 归档完成: {len(positions) if positions else 0} 持仓, {len(deals) if deals else 0} 成交")

    def emergency_flush(self) -> None:
        """强退前紧急 flush(H2:500ms 超时尽力落盘)"""
        try:
            self._flush_buffer()
            logger.info("紧急 flush 完成")
        except Exception as e:
            logger.error(f"紧急 flush 失败: {e}")

    def close(self) -> None:
        """关闭(停 flusher + 最后 flush + 关连接)"""
        self._stop_event.set()
        try:
            self._flusher.shutdown(wait=True)
        except Exception:
            self._flusher.shutdown(wait=False)
        self._flush_buffer()
        if self._conn:
            self._conn.close()
        logger.info("Store 关闭")

    # ===== 信号心跳(v1.2.2 §5.2) =====

    def record_heartbeat(self, source: str, signal_count: int,
                         scan_status: str) -> None:
        """记录信号心跳(Windows 端收到 buy-signal 后调用)"""
        assert self._conn is not None
        from datetime import date as date_type, datetime as dt
        with self._db_lock:
            self._conn.execute("""
                INSERT INTO live_signal_heartbeat
                (signal_date, source, signal_count, scan_status, received_at)
                VALUES (?,?,?,?,?)
            """, [date_type.today(), source, signal_count, scan_status, dt.now()])
        logger.info(f"心跳记录: source={source} count={signal_count} status={scan_status}")

    def get_latest_heartbeat(self, source: str = "docker_tdx") -> Optional[Dict[str, Any]]:
        """获取指定 source 当日最新心跳(看门狗用)"""
        assert self._conn is not None
        from datetime import date as date_type
        rows = self._conn.execute(
            "SELECT * FROM live_signal_heartbeat "
            "WHERE signal_date = ? AND source = ? "
            "ORDER BY received_at DESC LIMIT 1",
            [date_type.today(), source]
        ).fetchone()
        if not rows:
            return None
        cols = [d[0] for d in self._conn.description]
        return dict(zip(cols, rows))
