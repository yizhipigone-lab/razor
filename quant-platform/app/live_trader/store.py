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
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import duckdb

from core.logger import get_logger

from .config import LiveTraderConfig
from app.utils.xtquant_compat import format_code

logger = get_logger("live_trader.store")

# EOD 收盘口径阈值:净值曲线取 <= 该时间的最后一条快照当作当日收盘点
_EOD_CUTOFF = "15:30"


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
                commission DOUBLE, mode VARCHAR, traded_at TIMESTAMP,
                position_applied BOOLEAN DEFAULT FALSE
            )
        """)
        # C1(2026-07-15 全项目审计): 旧库补 position_applied 列(幂等认领标记)
        con.execute("ALTER TABLE live_deals ADD COLUMN IF NOT EXISTS position_applied BOOLEAN DEFAULT FALSE")
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_positions (
                code VARCHAR PRIMARY KEY,
                volume BIGINT, can_use_volume BIGINT, frozen_volume BIGINT,
                pending_buy_volume BIGINT, avg_cost DOUBLE, last_price DOUBLE,
                market_value DOUBLE, float_profit DOUBLE, profit_rate DOUBLE,
                peak_price DOUBLE, sell_count INTEGER, entry_date DATE,
                managed BOOLEAN, strategy_name VARCHAR,
                tp_triggered VARCHAR DEFAULT '[]'
            )
        """)
        # 迁移(v2 A3):为旧库补 tp_triggered 列(IF NOT EXISTS 幂等,审计L1)
        con.execute("ALTER TABLE live_positions ADD COLUMN IF NOT EXISTS tp_triggered VARCHAR DEFAULT '[]'")
        con.execute("ALTER TABLE live_positions ADD COLUMN IF NOT EXISTS last_close DOUBLE")
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

        v2(2026-07-14 审计H3):_write_to_db 失败必须向上抛(资金账目不能丢)。
        WAL 已先写(就算 DB 失败,重启还能补),所以 DB 失败 re-raise 让上层告警。
        """
        self._write_wal(kind, data)
        with self._db_lock:
            self._write_to_db(kind, data)  # H3:异常自然向上抛

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
                    # H3:flush 失败不能只 log——上层接住后必须告警。
                    # 这里 catch 是为了 flusher loop 不退出;错误信息已足够定位。
                    try:
                        self._flush_buffer()
                    except Exception as e:
                        logger.error(f"L2 flush 写入失败(可能丢 buffer 项): {e}", exc_info=True)
                self._stop_event.wait(self.config.buffer_flush_interval_ms / 1000.0)
            # 退出前最后 flush 一次
            try:
                self._flush_buffer()
            except Exception as e:
                logger.error(f"L2 退出前 flush 失败: {e}", exc_info=True)

        self._flusher.submit(_flush_loop)
        logger.info("L2 flusher 启动")

    def _flush_buffer(self) -> None:
        """排空 L1 buffer 批量写 DuckDB

        v2(2026-07-14 审计H3):异常向上抛,让 flusher 顶层 catch 接住+告警。
        旧版静默吞 → DB 写入失败上层不知道 → 资金账目失真。
        """
        if not self._buffer:
            return
        with self._buffer_lock:
            items = list(self._buffer)
            self._buffer.clear()
        if not items:
            return
        with self._db_lock:
            for kind, data in items:
                self._write_to_db(kind, data)  # H3:异常自然向上抛

    def _write_to_db(self, kind: str, data: Dict[str, Any]) -> None:
        """实际写 DuckDB(入库前统一 format_code,防 code 格式不一致)

        v2(2026-07-14 审计H3):异常向上抛,不再静默吞——调用方负责捕获+告警。
        旧版静默吞 → 上层以为成功实际失败 → 资金账目失真风险。
        """
        assert self._conn is not None
        # 漏洞B修复:所有含 code 字段的数据入库前统一格式化
        if "code" in data and data["code"]:
            data = {**data, "code": format_code(data["code"])}
        if kind == "order":
            self._upsert_order(data)
        elif kind == "deal":
            self._insert_deal(data)
        elif kind == "position":
            self._upsert_position(data)
        else:
            raise ValueError(f"_write_to_db 未知 kind={kind}")

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

    def apply_sell_fill(self, code: str, filled_volume: int,
                        trade_id: int = None) -> None:
        """卖出成交后递减持仓,清仓则重置全部状态防再买同票用旧值。

        C1(2026-07-15 全项目审计): 幂等改用 position_applied 原子认领。
        旧版 SELECT live_deals 判存在——但 sync_terminal_write 先于本方法插入 deal,
        首次即命中→持仓永不更新(实盘账目脱钩, PR#6 回归)。现用
        UPDATE...RETURNING 抢 position_applied FALSE→TRUE: 抢到才更新持仓,
        重复回报抢不到则跳过。deal 未入库(sync 失败)时仍更新(WAL+reconciler 兜底)。
        清仓重置含 avg_cost/entry_date(M2),否则再买同票用旧成本/旧日期。
        """
        assert self._conn is not None
        if filled_volume <= 0:
            return
        with self._db_lock:
            if trade_id is not None:
                claimed = self._conn.execute(
                    "UPDATE live_deals SET position_applied=TRUE "
                    "WHERE trade_id=? AND position_applied=FALSE RETURNING trade_id",
                    [trade_id]
                ).fetchone()
                if claimed is None:
                    exists = self._conn.execute(
                        "SELECT 1 FROM live_deals WHERE trade_id = ?", [trade_id]
                    ).fetchone()
                    if exists:
                        return  # 已应用, 幂等跳过
                    logger.warning(f"apply_sell_fill: trade_id={trade_id} 未入库(sync失败?), 仍更新持仓")
            row = self._conn.execute(
                "SELECT volume, can_use_volume FROM live_positions WHERE code = ?",
                [code]
            ).fetchone()
            if not row:
                return
            new_volume = max(0, int(row[0] or 0) - filled_volume)
            new_can_use = max(0, int(row[1] or 0) - filled_volume)
            if new_volume == 0:
                # 清仓:重置全部状态,防再买同票用旧值(F4/M2)
                self._conn.execute(
                    "UPDATE live_positions SET volume = 0, can_use_volume = 0, "
                    "frozen_volume = 0, peak_price = 0, sell_count = 0, "
                    "avg_cost = 0, entry_date = NULL, tp_triggered = '[]' WHERE code = ?",
                    [code]
                )
            else:
                self._conn.execute(
                    "UPDATE live_positions SET volume = ?, can_use_volume = ? WHERE code = ?",
                    [new_volume, new_can_use, code]
                )

    def update_tp_triggered(self, code: str, tp_triggered: str) -> None:
        """v2(A3):更新 TP 档位触发状态(JSON set 字符串,如 '["0"]')"""
        assert self._conn is not None
        with self._db_lock:
            self._conn.execute(
                "UPDATE live_positions SET tp_triggered = ? WHERE code = ?",
                [tp_triggered, code]
            )

    def apply_buy_fill(self, code: str, filled_volume: int,
                        filled_price: float = 0.0,
                        trade_id: int = None) -> None:
        """v2(审计H2/H3):买入成交原子递增 volume + 释放在途预扣 + 首次建仓写 entry_date
        v3(2026-07-14 审计H1):加 trade_id 幂等,防重复回报双扣持仓(对齐 apply_sell_fill)。
        v4(2026-07-15):新增 filled_price,首次建仓用买入价兜底算 avg_cost/profit_rate/market_value/float_profit,
        避免 QMT 报价格 0 时 refresh_quotes 跳过,profit_rate 永远是 0。

        原子 SQL(不读改写),避免 _release_pending_buy 全字段 upsert 覆盖 tp_triggered/sell_count(H3)。
        首次建仓(entry_date 为空)写 today,修 hold_days 恒=1 问题(H2)。
        C1(2026-07-15 全项目审计): 幂等改 position_applied 原子认领(对齐 apply_sell_fill),
        修旧版 SELECT live_deals 判存在导致首次即跳过、持仓永不更新的回归。
        """
        assert self._conn is not None
        if filled_volume <= 0:
            return
        from datetime import date as _date
        today = _date.today()
        with self._db_lock:
            if trade_id is not None:
                claimed = self._conn.execute(
                    "UPDATE live_deals SET position_applied=TRUE "
                    "WHERE trade_id=? AND position_applied=FALSE RETURNING trade_id",
                    [trade_id]
                ).fetchone()
                if claimed is None:
                    exists = self._conn.execute(
                        "SELECT 1 FROM live_deals WHERE trade_id = ?", [trade_id]
                    ).fetchone()
                    if exists:
                        logger.debug(f"apply_buy_fill 幂等命中,跳过重复 trade_id={trade_id} code={code}")
                        return  # 已应用, 幂等跳过
                    logger.warning(f"apply_buy_fill: trade_id={trade_id} 未入库(sync失败?), 仍更新持仓")
            row = self._conn.execute(
                "SELECT volume, pending_buy_volume, entry_date FROM live_positions WHERE code = ?",
                [code]
            ).fetchone()
            if not row:
                # 新建持仓(本地无行,QMT 已成交)
                # 用 filled_price 兜底算 avg_cost / profit_rate / market_value / float_profit,
                # 防止 refresh_quotes 拿不到价时 profit_rate 长期为 0
                _fp = float(filled_price or 0)
                _avg_cost = _fp if _fp > 0 else None  # QMT 回报无价时成本未确认,留 NULL(不写 0 冒充,防 refresh 浮盈虚高)
                _mv = _fp * filled_volume
                self._conn.execute(
                    "INSERT INTO live_positions (code, volume, can_use_volume, "
                    "pending_buy_volume, avg_cost, last_price, market_value, "
                    "float_profit, profit_rate, entry_date, managed, tp_triggered) "
                    "VALUES (?, ?, 0, 0, ?, ?, ?, 0, 0, ?, TRUE, '[]')",
                    [code, filled_volume, _avg_cost, _fp, _mv, today]
                )
                return
            new_volume = int(row[0] or 0) + filled_volume
            new_pending = max(0, int(row[1] or 0) - filled_volume)
            entry_date = row[2]
            if entry_date is None:
                # 首次建仓写 entry_date(H2),不动 peak/sell_count/tp_triggered
                self._conn.execute(
                    "UPDATE live_positions SET volume = ?, pending_buy_volume = ?, "
                    "entry_date = ? WHERE code = ?",
                    [new_volume, new_pending, today, code]
                )
            else:
                self._conn.execute(
                    "UPDATE live_positions SET volume = ?, pending_buy_volume = ? WHERE code = ?",
                    [new_volume, new_pending, code]
                )

    def release_pending_buy(self, code: str, volume: int) -> None:
        """C2(2026-07-15 全项目审计): 废单/拒单时只释放在途预扣冻结, 不动 volume。

        废单=零成交, 本应只释放 risk_gate 下单时冻结的 pending_buy_volume,
        绝不能复用 apply_buy_fill(那会 volume += 把废单股数加进持仓, 反向膨胀)。
        """
        assert self._conn is not None
        if volume <= 0:
            return
        with self._db_lock:
            self._conn.execute(
                "UPDATE live_positions SET "
                "pending_buy_volume = GREATEST(0, pending_buy_volume - ?) WHERE code = ?",
                [volume, code]
            )

    def clean_dryrun_residue(self) -> None:
        """v2(F8/H1): 清理 dry-run 残留委托/成交/盈亏闭环(切 live 前,防 mock 污染)

        cycles 表无 mode 字段,切 live 前(仍 dry-run)全清,下次 recompute 用
        live 成交重建(build_cycles 已过滤 mode='live',见 pnl_engine)。
        """
        assert self._conn is not None
        with self._db_lock:
            self._conn.execute("DELETE FROM live_orders WHERE mode = 'dry-run'")
            self._conn.execute("DELETE FROM live_deals WHERE mode = 'dry-run'")
            self._conn.execute("DELETE FROM live_cycles")

    def _upsert_position(self, data: Dict[str, Any]) -> None:
        assert self._conn is not None
        self._conn.execute("""
            INSERT INTO live_positions
            (code, volume, can_use_volume, frozen_volume, pending_buy_volume,
             avg_cost, last_price, market_value, float_profit, profit_rate,
             peak_price, sell_count, entry_date, managed, strategy_name,
             tp_triggered, last_close)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                volume=excluded.volume, can_use_volume=excluded.can_use_volume,
                frozen_volume=excluded.frozen_volume,
                pending_buy_volume=excluded.pending_buy_volume,
                avg_cost=excluded.avg_cost, last_price=excluded.last_price,
                market_value=excluded.market_value, float_profit=excluded.float_profit,
                profit_rate=excluded.profit_rate,
                peak_price=GREATEST(COALESCE(live_positions.peak_price, 0), COALESCE(excluded.peak_price, 0)),
                sell_count=excluded.sell_count, entry_date=excluded.entry_date,
                managed=excluded.managed, strategy_name=excluded.strategy_name,
                tp_triggered=excluded.tp_triggered,
                last_close=COALESCE(excluded.last_close, live_positions.last_close)
        """, [
            data.get("code"), data.get("volume", 0), data.get("can_use_volume", 0),
            data.get("frozen_volume", 0), data.get("pending_buy_volume", 0),
            data.get("avg_cost", 0.0), data.get("last_price", 0.0),
            data.get("market_value", 0.0), data.get("float_profit", 0.0),
            data.get("profit_rate", 0.0), data.get("peak_price", 0.0),
            data.get("sell_count", 0), data.get("entry_date"),
            data.get("managed", True), data.get("strategy_name", ""),
            data.get("tp_triggered", "[]"),
            data.get("last_close"),
        ])

    # ===== 查询 =====

    def get_order_by_seq(self, seq: int) -> Optional[Dict[str, Any]]:
        """按 seq 查委托(下单时用 seq 做临时 order_id,callback 需反查)"""
        assert self._conn is not None
        with self._db_lock:  # C1 修复(2026-07-19 审计):查询持锁防单连接并发段错误
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
            self._conn.execute(
                "UPDATE live_orders SET order_id = ? WHERE order_id = ?",
                [new_id, old_id]
            )
            return True

    def get_order_by_client_id(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        """C3 幂等检查:按 client_order_id 查"""
        assert self._conn is not None
        with self._db_lock:  # C1 修复(审计):查询持锁防并发段错误
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
        with self._db_lock:  # C1 修复(审计)
            rows = self._conn.execute(
                "SELECT * FROM live_orders WHERE order_id = ?", [order_id]
            ).fetchone()
            if not rows:
                return None
            cols = [d[0] for d in self._conn.description]
            return dict(zip(cols, rows))

    def get_deal_by_trade_id(self, trade_id: int) -> Optional[Dict[str, Any]]:
        """H1(2026-07-14):按 trade_id 查成交记录,供 callback_handler 入口幂等检查用。

        QMT 网络抖动/重连会推重复 trade_id 回报,callback_handler 在写 deal 之前
        先查 deals 表;有则整个跳过(不写 deal、不动 position、不重算盈亏)。
        """
        assert self._conn is not None
        if trade_id is None or trade_id == 0:
            return None
        with self._db_lock:  # C1 修复(审计)
            rows = self._conn.execute(
                "SELECT * FROM live_deals WHERE trade_id = ?", [trade_id]
            ).fetchone()
            if not rows:
                return None
            cols = [d[0] for d in self._conn.description]
            return dict(zip(cols, rows))

    def get_inflight_orders(self, live_only: bool = False) -> List[Dict[str, Any]]:
        """获取在途订单(非终态,§17.1 启动恢复)

        v2审计中-7: live_only=True 只返回真实在途(过滤 dry-run mock 单,
        模式切换撤单/轮询用,防 mock 残留卡死切换)。
        """
        assert self._conn is not None
        from app.utils.xtquant_compat import ORDER_STATUS_TERMINAL
        placeholders = ",".join("?" * len(ORDER_STATUS_TERMINAL))
        sql = f"SELECT * FROM live_orders WHERE status NOT IN ({placeholders})"
        if live_only:
            sql += " AND mode = 'live'"
        with self._db_lock:  # C1 修复(审计):查询持锁防并发段错误
            rows = self._conn.execute(sql, list(ORDER_STATUS_TERMINAL)).fetchall()
            if not rows:
                return []
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r)) for r in rows]

    def get_positions(self, managed_only: bool = False) -> List[Dict[str, Any]]:
        assert self._conn is not None
        with self._db_lock:  # C1 修复(审计)
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
        with self._db_lock:  # C1 修复(审计)
            rows = self._conn.execute(
                "SELECT * FROM live_positions WHERE code = ?", [code]
            ).fetchone()
            if not rows:
                return None
            cols = [d[0] for d in self._conn.description]
            return dict(zip(cols, rows))

    def _get_earliest_buy_date(self, code: str) -> Optional[date]:
        """从成交记录回填最早买入日期(2026-07-16: 修 _takeover_positions entry_date=NULL)

        返回 None 表示无成交记录,调用方应回退到 date.today()。
        """
        assert self._conn is not None
        with self._db_lock:  # C1 修复(审计)
            try:
                row = self._conn.execute(
                    "SELECT MIN(traded_at) FROM live_deals "
                    "WHERE code = ? AND direction = 'buy'",
                    [code]
                ).fetchone()
                if row and row[0]:
                    val = row[0]
                    if isinstance(val, datetime):
                        return val.date()
                    if isinstance(val, date):
                        return val
                    try:
                        return datetime.fromisoformat(str(val)).date()
                    except Exception:
                        return None
                return None
            except Exception:
                return None

    def get_deals(self, code: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        """获取成交(盈亏重算数据源,§18.7 内存缓存)"""
        assert self._conn is not None
        with self._db_lock:  # C1 修复(审计)
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
        with self._db_lock:  # C1 修复(审计)
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
        with self._db_lock:  # C1 修复(审计)
            row = self._conn.execute(
                "SELECT total_asset FROM live_assets_backup "
                "WHERE backup_date = ? ORDER BY backup_time ASC LIMIT 1",
                [today]
            ).fetchone()
            if row and row[0] is not None:
                return float(row[0])
            return None

    def get_daily_baseline(self) -> Optional[float]:
        """日亏基准(闸门5a 专用,带兜底链)。

        2026-07-16 根因修复:QMT 开盘未连上 → 今日无快照 → 旧逻辑闸门5a 全天 fail-safe
        误禁买。兜底链:
          1. 今日首条快照(与 get_open_asset 相同)
          2. 最近一条早于今日的快照(昨收≈今开,标准日盈亏基准)
          3. 仍无 → None(由调用方兜底 live_capital)

        注意:不改 get_open_asset 语义(它还被 main.py 启动备份守卫/daily_summary 用)。
        """
        assert self._conn is not None
        today = date.today()
        with self._db_lock:  # C1 修复(审计)
            row = self._conn.execute(
                "SELECT total_asset FROM live_assets_backup "
                "WHERE backup_date = ? ORDER BY backup_time ASC LIMIT 1",
                [today]
            ).fetchone()
            if row and row[0] is not None:
                return float(row[0])
            # 今日无快照 → 兜底:昨收(最近一条早于今日的快照)
            row = self._conn.execute(
                "SELECT total_asset FROM live_assets_backup "
                "WHERE backup_date < ? ORDER BY backup_date DESC, backup_time DESC LIMIT 1",
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

    def get_equity_points(self, days: int) -> list:
        """净值曲线数据(从 live_assets_backup 快照),统一日收盘口径。

        - days<=1: **最近 2 个交易日的日 EOD 折线**(昨天收盘 + 今天收盘/实时),
          体现最近 1 日的净值变化。粒度按日(每天 1 个点),非 5min 分时,非当日单点。
        - days>=2: **每日一个 EOD 点**(优先取 <=15:30 的最后一条=收盘;当日无收盘点
          才退到当日最后一条),过滤掉启动重启写入的 stray 点。

        返回 [{date, time, cash, market_value, total}] 按 date 升序。
        口径统一为日净值:1日=最近2日折线,5/30/1年=多日 EOD。

        修 2026-07-16(3):1日档经历 分时→当日单点→最近2日折线。
        用户要求:不是1个点、要有折线、粒度按1日(非当日/非分时)。
        取最近2个交易日 EOD 点连线,即"最近1日的净值变化"。
        """
        assert self._conn is not None
        with self._db_lock:  # C1 修复(审计):查询持锁防并发段错误
            start = date.today() - timedelta(days=int(days))
            if days <= 1:
                # 最近 2 个交易日的日 EOD 折线:用 09:25-15:05 锁交易日(避开夜间重启
                # stray),每天取 <=15:30 的最后一条(收盘;最新一天盘中则为最新一条)
                rows = self._conn.execute(
                    "WITH recent_dates AS ("
                    "  SELECT backup_date FROM live_assets_backup "
                    "  WHERE backup_time BETWEEN '09:25' AND '15:05' "
                    "  GROUP BY backup_date ORDER BY backup_date DESC LIMIT 2"
                    "), eod AS ("
                    "  SELECT backup_date, backup_time, cash, market_value, total_asset, "
                    "    ROW_NUMBER() OVER ("
                    "      PARTITION BY backup_date ORDER BY "
                    f"        CASE WHEN backup_time <= '{_EOD_CUTOFF}' THEN 0 ELSE 1 END, "
                    "        backup_time DESC) AS rn "
                    "  FROM live_assets_backup "
                    "  WHERE backup_date IN (SELECT backup_date FROM recent_dates)"
                    ") "
                    "SELECT backup_date, backup_time, cash, market_value, total_asset "
                    "FROM eod WHERE rn = 1 ORDER BY backup_date",
                ).fetchall()
            else:
                # 每日 EOD:同日多条快照取"<=15:30 的最后一条"(收盘),无则取当日最后一条
                rows = self._conn.execute(
                    "SELECT backup_date, backup_time, cash, market_value, total_asset FROM ("
                    "  SELECT backup_date, backup_time, cash, market_value, total_asset, "
                    "    ROW_NUMBER() OVER ("
                    "      PARTITION BY backup_date ORDER BY "
                    f"        CASE WHEN backup_time <= '{_EOD_CUTOFF}' THEN 0 ELSE 1 END, "
                    "        backup_time DESC) AS rn "
                    "  FROM live_assets_backup WHERE backup_date >= ?"
                    ") WHERE rn = 1 ORDER BY backup_date",
                    [start]
                ).fetchall()
            return [{"date": str(r[0]), "time": r[1],
                     "cash": float(r[2] or 0), "market_value": float(r[3] or 0),
                     "total": float(r[4] or 0)} for r in rows]

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

    def atomic_add_pending_buy(self, code: str, volume: int) -> bool:
        """原子增加 pending_buy_volume(防 TOCTOU 竞态)

        Returns True if position existed and was updated, False if not found.
        """
        assert self._conn is not None
        with self._db_lock:
            result = self._conn.execute(
                "UPDATE live_positions SET pending_buy_volume = pending_buy_volume + ? "
                "WHERE code = ?",
                [volume, code]
            )
            return result.fetchone()[0] > 0

    def refresh_quotes(self, quotes: Dict[str, Dict[str, float]]) -> int:
        """批量刷新持仓行情(行情订阅回调 / scheduler 周期任务用)

        仅更新现价/市值/浮盈三字段,不触碰 volume/avg_cost 等关键数据,
        保证 QMT 报价断流时不会写脏数量和成本。

        Args:
            quotes: {code: {lastPrice, lastClose, ...}} 来自 get_realtime_quotes

        Returns:
            实际更新的持仓条数
        """
        assert self._conn is not None
        if not quotes:
            return 0
        updated = 0
        with self._db_lock:
            for code, q in quotes.items():
                last = float(q.get("lastPrice", 0) or 0)
                if last <= 0:
                    continue
                # 读现有持仓,计算市值和浮盈
                row = self._conn.execute(
                    "SELECT volume, avg_cost FROM live_positions WHERE code = ?",
                    [code]
                ).fetchone()
                if not row:
                    continue
                volume = int(row[0] or 0)
                avg_cost = float(row[1] or 0)
                market_value = last * volume
                if avg_cost > 0:
                    float_profit = (last - avg_cost) * volume
                    cost_basis = avg_cost * volume
                    profit_rate = (float_profit / cost_basis * 100) if cost_basis > 0 else 0.0
                else:
                    # 成本未确认(apply_buy_fill 遇 QMT 回报无 filled_price 写 NULL):
                    # 不瞎算浮盈,避免 (last-0)*volume 虚高;待持仓同步用 QMT avg_cost 覆盖后再算
                    float_profit = 0.0
                    profit_rate = 0.0
                last_close = float(q.get("lastClose", 0) or 0)
                self._conn.execute(
                    "UPDATE live_positions SET last_price = ?, "
                    "market_value = ?, float_profit = ?, profit_rate = ?, "
                    "last_close = CASE WHEN ? > 0 THEN ? ELSE last_close END, "
                    "peak_price = GREATEST(COALESCE(peak_price, 0), ?) WHERE code = ?",
                    [last, market_value, float_profit, profit_rate, last_close, last_close, last, code],
                )
                updated += 1
        if updated:
            logger.debug(f"持仓行情刷新: {updated} 条")
        return updated

    # 历史命名,实际为 API 服务端信号
    def get_latest_heartbeat(self, source: str = "docker_tdx") -> Optional[Dict[str, Any]]:
        """获取指定 source 当日最新心跳(看门狗用)"""
        assert self._conn is not None
        from datetime import date as date_type
        with self._db_lock:  # C1 修复(审计)
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
