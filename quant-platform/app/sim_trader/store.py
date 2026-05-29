"""
模拟盘交易 — 持久化存储
所有持仓/交易/引擎状态变化即时写入 DuckDB，服务器重启可恢复。
"""
import json
from datetime import date
from typing import Optional, Dict, List
from pathlib import Path

from core.logger import get_logger

log = get_logger("SimStore")


class SimTraderStore:
    """DuckDB 持久化：持仓、交易记录、净值曲线、引擎状态"""

    def __init__(self):
        from database.duckdb_manager import db
        self._db = db
        self._ensure_tables()

    @property
    def conn(self):
        return self._db.conn

    # ── 建表 ────────────────────────────────────

    def _ensure_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sim_positions (
                code            VARCHAR PRIMARY KEY,
                entry_date      DATE,
                entry_price     DOUBLE,
                shares          INTEGER,
                cost            DOUBLE,
                peak_price      DOUBLE,
                remaining_shares INTEGER,
                tp1_triggered   BOOLEAN,
                tp2_triggered   BOOLEAN,
                is_active       BOOLEAN,
                strategy_name   VARCHAR
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sim_trades (
                id              INTEGER PRIMARY KEY,
                code            VARCHAR,
                entry_date      DATE,
                exit_date       DATE,
                entry_price     DOUBLE,
                exit_price      DOUBLE,
                shares          INTEGER,
                return_pct      DOUBLE,
                profit_amount   DOUBLE,
                exit_reason     VARCHAR,
                hold_days       INTEGER,
                entry_reason    VARCHAR,
                exit_timing     VARCHAR
            )
        """)
        # 自增 ID 从已有最大开始
        try:
            max_id = self.conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM sim_trades"
            ).fetchone()[0]
            self.conn.execute(
                f"CREATE SEQUENCE IF NOT EXISTS sim_trade_id START {max_id + 1}"
            )
        except Exception:
            log.warning("CREATE SEQUENCE sim_trade_id 失败，后续 save_trade 将出错")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sim_equity (
                date     DATE PRIMARY KEY,
                equity   DOUBLE,
                cash     DOUBLE,
                positions INTEGER
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sim_state (
                key   VARCHAR PRIMARY KEY,
                value VARCHAR
            )
        """)

    # ── 持仓 ────────────────────────────────────

    def save_positions(self, positions: Dict[str, "Position"]):
        # 全量覆写（先清空，再插入活跃持仓）
        self.conn.execute("DELETE FROM sim_positions")
        for code, pos in positions.items():
            if not pos.is_active:
                continue
            self.conn.execute("""
                INSERT INTO sim_positions
                    (code, entry_date, entry_price, shares, cost, peak_price,
                     remaining_shares, tp1_triggered, tp2_triggered,
                     is_active, strategy_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                pos.code, pos.entry_date, pos.entry_price, pos.shares,
                pos.cost, pos.peak_price, pos.remaining_shares,
                pos.tp1_triggered, pos.tp2_triggered, pos.is_active,
                pos.strategy_name,
            ])

    def load_positions(self) -> Dict[str, "Position"]:
        from app.sim_trader.engine import Position
        rows = self.conn.execute(
            "SELECT * FROM sim_positions WHERE is_active = TRUE"
        ).fetchall()
        result = {}
        for r in rows:
            pos = Position(
                code=r[0], entry_date=r[1], entry_price=r[2],
                shares=r[3], cost=r[4], strategy_name=r[10] or "",
            )
            pos.peak_price = r[5]
            pos.remaining_shares = r[6]
            pos.tp1_triggered = r[7]
            pos.tp2_triggered = r[8]
            pos.is_active = r[9]
            result[pos.code] = pos
        return result

    # ── 交易记录 ────────────────────────────────

    def save_trade(self, trade: "Trade"):
        self.conn.execute("""
            INSERT INTO sim_trades
                (id, code, entry_date, exit_date, entry_price, exit_price,
                 shares, return_pct, profit_amount, exit_reason,
                 hold_days, entry_reason, exit_timing)
            VALUES (nextval('sim_trade_id'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            trade.code, trade.entry_date, trade.exit_date,
            trade.entry_price, trade.exit_price, trade.shares,
            trade.return_pct, trade.profit_amount, trade.exit_reason,
            trade.hold_days, trade.entry_reason, trade.exit_timing,
        ])

    def load_trades(self) -> List["Trade"]:
        from app.sim_trader.engine import Trade
        rows = self.conn.execute(
            "SELECT * FROM sim_trades ORDER BY id"
        ).fetchall()
        return [Trade(
            code=r[1], entry_date=r[2], exit_date=r[3],
            entry_price=r[4], exit_price=r[5], shares=r[6],
            return_pct=r[7], profit_amount=r[8], exit_reason=r[9],
            hold_days=r[10], entry_reason=r[11], exit_timing=r[12],
        ) for r in rows]

    # ── 净值曲线 ────────────────────────────────

    def save_equity_point(self, d: date, equity: float, cash: float,
                          positions: int):
        self.conn.execute("""
            INSERT OR REPLACE INTO sim_equity VALUES (?, ?, ?, ?)
        """, [d, equity, cash, positions])

    def load_equity_curve(self) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT date, equity, cash, positions FROM sim_equity ORDER BY date"
        ).fetchall()
        return [{'date': r[0], 'equity': r[1], 'cash': r[2],
                 'positions': r[3]} for r in rows]

    # ── 引擎状态 ────────────────────────────────

    def save_state(self, cash: float, consecutive_losses: int,
                   pause_until: Optional[date], trade_count: int):
        try:
            self.conn.execute("BEGIN")
            self.conn.execute("INSERT OR REPLACE INTO sim_state VALUES ('cash', ?)", [str(cash)])
            self.conn.execute("INSERT OR REPLACE INTO sim_state VALUES ('consecutive_losses', ?)", [str(consecutive_losses)])
            self.conn.execute("INSERT OR REPLACE INTO sim_state VALUES ('pause_until', ?)", [str(pause_until) if pause_until else ''])
            self.conn.execute("INSERT OR REPLACE INTO sim_state VALUES ('trade_count', ?)", [str(trade_count)])
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def load_state(self) -> dict:
        rows = self.conn.execute(
            "SELECT key, value FROM sim_state"
        ).fetchall()
        state = {r[0]: r[1] for r in rows}
        pause_str = state.get('pause_until', '')
        try:
            pause = date.fromisoformat(pause_str) if pause_str else None
        except ValueError:
            pause = None
        return {
            'cash': float(state.get('cash', '1000000')),
            'consecutive_losses': int(state.get('consecutive_losses', '0')),
            'pause_until': pause,
            'trade_count': int(state.get('trade_count', '0')),
        }


class JsonSimStore:
    """JSON 文件持久化（无 DuckDB 依赖，无锁）"""
    def __init__(self, path: str = None):
        from pathlib import Path as _P
        self._path = _P(path or str(_P(__file__).parent.parent.parent / "output" / "sim_trader" / "state.json"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data = {}
        if self._path.exists():
            try:
                with open(self._path, 'r', encoding='utf-8') as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def _save(self):
        with open(self._path, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2, default=str)

    def load_state(self) -> dict:
        s = self._data.get('state', {})
        return {
            'cash': float(s.get('cash', 1_000_000)),
            'consecutive_losses': int(s.get('consecutive_losses', 0)),
            'pause_until': date.fromisoformat(s['pause_until']) if s.get('pause_until') else None,
            'trade_count': int(s.get('trade_count', 0)),
        }

    def save_state(self, cash, consecutive_losses, pause_until, trade_count):
        self._data['state'] = {
            'cash': cash, 'consecutive_losses': consecutive_losses,
            'pause_until': str(pause_until) if pause_until else None,
            'trade_count': trade_count,
        }
        self._save()

    def load_positions(self) -> Dict[str, "Position"]:
        from app.sim_trader.engine import Position
        result = {}
        for code, p in self._data.get('positions', {}).items():
            result[code] = Position(
                code=code, entry_date=date.fromisoformat(p['entry_date']),
                entry_price=float(p['entry_price']), shares=int(p['shares']),
                cost=float(p['cost']), strategy_name=p.get('strategy_name', ''),
                entry_time=p.get('entry_time', '15:00'),
            )
        return result

    def save_positions(self, positions: Dict[str, "Position"]):
        self._data['positions'] = {
            code: {'entry_date': str(p.entry_date), 'entry_price': p.entry_price,
                   'shares': p.shares, 'cost': p.cost, 'strategy_name': p.strategy_name,
                   'entry_time': getattr(p, 'entry_time', '15:00')}
            for code, p in positions.items()
        }
        self._save()

    def save_trade(self, trade: "Trade"):
        trades = self._data.setdefault('trades', [])
        trades.append({
            'code': trade.code, 'entry_date': str(trade.entry_date), 'exit_date': str(trade.exit_date),
            'entry_price': trade.entry_price, 'exit_price': trade.exit_price,
            'shares': trade.shares, 'ret_pct': trade.return_pct,
            'profit': trade.profit_amount, 'reason': trade.exit_reason,
            'hold_days': trade.hold_days,
            'entry_time': getattr(trade, 'entry_time', '15:00'),
            'exit_time': getattr(trade, 'exit_time', '15:00'),
        })
        self._save()

    def load_trades(self) -> List["Trade"]:
        from app.sim_trader.engine import Trade
        result = []
        for t in self._data.get('trades', []):
            result.append(Trade(
                code=t['code'], entry_date=date.fromisoformat(t['entry_date']),
                exit_date=date.fromisoformat(t['exit_date']),
                entry_price=float(t['entry_price']), exit_price=float(t['exit_price']),
                shares=int(t['shares']), return_pct=float(t['ret_pct']),
                profit_amount=float(t['profit']), exit_reason=t['reason'],
                hold_days=int(t['hold_days']),
                entry_time=t.get('entry_time', '15:00'),
                exit_time=t.get('exit_time', '15:00'),
            ))
        return result

    def save_equity_point(self, d: date, equity: float, cash: float, positions: int):
        pts = self._data.setdefault('equity_curve', [])
        pts.append({'date': str(d), 'equity': equity, 'cash': cash, 'pos': positions})
        if len(pts) % 10 == 0:
            self._save()

    def load_equity_curve(self) -> List[Dict]:
        return self._data.get('equity_curve', [])

    # ── 全量清空 ────────────────────────────────

    def clear_all(self):
        for t in ('sim_positions', 'sim_trades', 'sim_equity', 'sim_state'):
            self.conn.execute(f"DELETE FROM {t}")
        try:
            self.conn.execute("DROP SEQUENCE IF EXISTS sim_trade_id")
        except Exception:
            log.warning("DROP SEQUENCE sim_trade_id 失败")
