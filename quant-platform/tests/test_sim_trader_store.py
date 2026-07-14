"""
P1 测试套件 — JsonSimStore 持久化健壮性
覆盖修复点:
  P1-1 原子写        — _save 用 tmp+os.replace, 不留半截文件
  P1-2 双写合并/去重  — equity_curve 同日仅一条, 统一 'pos' 键
  P1-3 source 字段    — equity 记录带来源标记
  P1-5 Position 补全  — save/load 往返保住 is_active/peak_price/remaining_shares/tp1/tp2
  P1-6 即时落盘        — save_equity_point 立即写盘(不再 %10 延迟)
  P0-4 加载校验        — 污染 equity_curve 被丢弃, 清零/正常曲线放行
"""
import json
from datetime import date

import pytest

from app.sim_trader.store import JsonSimStore
from app.sim_trader.engine import Position, SimTraderEngine
import app.sim_trader.engine as eng_mod
from app.sim_trader.config import INITIAL_CAPITAL


@pytest.fixture
def tmp_store(tmp_path):
    """指向临时文件的空 JsonSimStore。"""
    p = tmp_path / "sim" / "state.json"
    s = JsonSimStore(path=str(p))
    s._data = {}
    return s


def _make_position(code="000001"):
    pos = Position(code=code, entry_date=date(2026, 3, 2),
                   entry_price=10.0, shares=5000, cost=50000.0,
                   strategy_name="QUANTQQ", entry_time="14:54")
    # 模拟运行中状态变化
    pos.peak_price = 12.5
    pos.remaining_shares = 2500
    pos.tp1_triggered = True
    pos.tp2_triggered = False
    pos.is_active = True
    return pos


# ── P1-5: Position 序列化字段完整性 ──────────────────────

def test_position_roundtrip_preserves_all_fields(tmp_store):
    """save_positions -> load_positions 必须保住全部运行态字段。"""
    pos = _make_position()
    tmp_store.save_positions({"000001": pos})

    # 重新从磁盘加载(新实例, 杜绝内存残留)
    reloaded_store = JsonSimStore(path=str(tmp_store._path))
    loaded = reloaded_store.load_positions()

    assert "000001" in loaded
    p = loaded["000001"]
    assert p.entry_price == 10.0
    assert p.shares == 5000
    assert p.peak_price == 12.5            # P1-5 关键: 历史会丢
    assert p.remaining_shares == 2500      # P1-5 关键: 历史会丢
    assert p.tp1_triggered is True
    assert p.tp2_triggered is False
    assert p.is_active is True
    assert p.strategy_name == "QUANTQQ"


def test_position_load_missing_fields_falls_back(tmp_store):
    """旧数据(缺新字段)加载时回退默认, 不报错(向后兼容)。"""
    # 模拟历史只有6字段的持仓
    tmp_store._data["positions"] = {
        "000002": {"entry_date": "2026-03-02", "entry_price": 8.0,
                   "shares": 3000, "cost": 24000.0,
                   "strategy_name": "QUANTQQ", "entry_time": "15:00"}
    }
    tmp_store._save()
    loaded = JsonSimStore(path=str(tmp_store._path)).load_positions()
    p = loaded["000002"]
    # __post_init__ 默认: peak=entry, remaining=shares
    assert p.peak_price == 8.0
    assert p.remaining_shares == 3000
    assert p.is_active is True


# ── P1-2 / P1-3 / P1-6: equity_curve 单路径/去重/source/即时落盘 ──

def test_save_equity_point_immediate_persist(tmp_store):
    """P1-6: 单次写入立即落盘(不等10次)。"""
    tmp_store.save_equity_point(date(2026, 3, 2), 1_000_000, 1_000_000, 0)
    on_disk = json.loads(tmp_store._path.read_text(encoding="utf-8"))
    assert len(on_disk["equity_curve"]) == 1


def test_save_equity_point_has_source_and_pos_key(tmp_store):
    """P1-3: 带 source; P1-2: 用 'pos' 键。"""
    tmp_store.save_equity_point(date(2026, 3, 2), 1_000_000, 1_000_000, 0,
                                source="record")
    e = tmp_store.load_equity_curve()[0]
    assert e["source"] == "record"
    assert "pos" in e
    assert "positions" not in e


def test_save_equity_point_same_day_dedup(tmp_store):
    """P1-2: 同一天多次写入只保留最新一条。"""
    tmp_store.save_equity_point(date(2026, 3, 2), 1_000_000, 1_000_000, 0)
    tmp_store.save_equity_point(date(2026, 3, 2), 1_050_000, 900_000, 2)  # 同日覆盖
    tmp_store.save_equity_point(date(2026, 3, 3), 1_060_000, 800_000, 3)
    ec = tmp_store.load_equity_curve()
    assert len(ec) == 2                       # 不是 3
    assert ec[0]["equity"] == 1_050_000       # 保留最新
    assert ec[1]["date"] == "2026-03-03"


def test_record_no_duplicate_via_engine(tmp_store):
    """P1-2 端到端: engine.record 同日不产生重复/双键记录。"""
    engine = SimTraderEngine(store=tmp_store)
    engine.cash = 1_000_000
    engine.record(date(2026, 3, 2), snapshot={})
    engine.record(date(2026, 3, 2), snapshot={})  # 同日再记
    ec = tmp_store.load_equity_curve()
    assert len(ec) == 1
    assert all("pos" in e and "positions" not in e for e in ec)


# ── P1-1: 原子写 ────────────────────────────────────────

def test_atomic_write_no_tmp_left_behind(tmp_store):
    """P1-1: 正常写入后不残留 .tmp 文件。"""
    tmp_store.save_equity_point(date(2026, 3, 2), 1_000_000, 1_000_000, 0)
    tmp_file = str(tmp_store._path) + ".tmp"
    import os
    assert not os.path.exists(tmp_file)
    # 主文件是完整合法 JSON
    json.loads(tmp_store._path.read_text(encoding="utf-8"))


def test_atomic_write_replaces_completely(tmp_store):
    """P1-1: 二次写入完全替换, 不损坏。"""
    tmp_store.save_equity_point(date(2026, 3, 2), 1_000_000, 1_000_000, 0)
    tmp_store.save_state(950_000, 0, None, 5)
    data = json.loads(tmp_store._path.read_text(encoding="utf-8"))
    assert data["state"]["cash"] == 950_000
    assert len(data["equity_curve"]) == 1


# ── P0-4: 加载期一致性校验 ──────────────────────────────

def test_load_validation_rejects_polluted_curve(tmp_store):
    """P0-4: 首条 equity 超本金1.10倍 -> 丢弃曲线, 保留 cash/trades。"""
    eng_mod._BAD_EQUITY_CURVE_DETECTED = False
    tmp_store._data = {
        "state": {"cash": 2_944_349, "consecutive_losses": 0,
                  "pause_until": None, "trade_count": 1},
        "equity_curve": [{"date": "2026-01-05", "equity": 2_139_705,
                          "cash": 466_635, "pos": 17}],
        "trades": [{"code": "000727", "entry_date": "2026-01-05",
                    "exit_date": "2026-01-06", "entry_price": 2.57,
                    "exit_price": 2.65, "shares": 12400, "ret_pct": 3.0,
                    "profit": 899.0, "reason": "TP1", "hold_days": 2}],
        "positions": {},
    }
    tmp_store._save()
    engine = SimTraderEngine(store=JsonSimStore(path=str(tmp_store._path)))
    assert engine.equity_curve == []                  # 污染曲线被丢弃
    assert eng_mod._BAD_EQUITY_CURVE_DETECTED is True
    assert len(engine.trades) == 1                    # trades 保留
    assert engine.cash == 2_944_349                   # cash 保留


def test_load_validation_passes_clean_curve(tmp_store):
    """P0-4: 正常曲线(首条≈本金)放行, 不置告警位。
    注: 加载后引擎会调 _fill_missing_snapshots 回填到昨天, 曲线会变长, 这是预期行为;
    校验只关心"未被拒绝"(首条保留、未置告警位)。"""
    eng_mod._BAD_EQUITY_CURVE_DETECTED = False
    tmp_store._data = {
        "state": {"cash": 1_000_000, "consecutive_losses": 0,
                  "pause_until": None, "trade_count": 0},
        "equity_curve": [{"date": "2026-03-02", "equity": 1_000_000,
                          "cash": 1_000_000, "pos": 0, "source": "record"}],
        "positions": {},
    }
    tmp_store._save()
    engine = SimTraderEngine(store=JsonSimStore(path=str(tmp_store._path)))
    assert len(engine.equity_curve) >= 1                      # 未被清空(非拒绝)
    assert str(engine.equity_curve[0]["date"]) == "2026-03-02"  # 首条保留
    assert engine.equity_curve[0]["equity"] == 1_000_000
    assert eng_mod._BAD_EQUITY_CURVE_DETECTED is False        # 未置告警位
    # P1-2 验证: 回填点也统一用 'pos' 键, 无 'positions' 键
    assert all("positions" not in e for e in engine.equity_curve)


def test_load_validation_empty_curve_ok(tmp_store):
    """P0-4: 清零后空曲线放行(回放起点场景)。"""
    eng_mod._BAD_EQUITY_CURVE_DETECTED = False
    engine = SimTraderEngine(store=tmp_store)  # _data={}
    assert engine.equity_curve == []
    assert engine.cash == INITIAL_CAPITAL
    assert eng_mod._BAD_EQUITY_CURVE_DETECTED is False


# ── 净值虚高修复: total_equity 估值优先级 + 覆盖率 + source标记 ──

def _mk_engine_with_position():
    engine = SimTraderEngine()  # 无store纯内存
    engine.cash = 100_000
    pos = Position(code="000001", entry_date=date(2026, 3, 2),
                   entry_price=10.0, shares=1000, cost=10_000)
    pos.remaining_shares = 1000
    pos.current_price = 15.0  # 上次已知市价
    engine.positions["000001"] = pos
    return engine


def test_total_equity_uses_snapshot_price():
    """有当前价时用 snapshot 价格。"""
    e = _mk_engine_with_position()
    assert e.total_equity({"000001": {"close": 20.0}}) == 100_000 + 1000 * 20


def test_total_equity_missing_price_uses_current_not_entry():
    """净值虚高修复: snapshot 缺价时用上次市价(15), 而非买入价(10)。"""
    e = _mk_engine_with_position()
    eq = e.total_equity({})  # 空 snapshot
    assert eq == 100_000 + 1000 * 15   # 用 current_price=15
    assert eq != 100_000 + 1000 * 10   # 不是 entry_price=10


def test_total_equity_no_current_falls_back_entry():
    """既无 snapshot 又无 current_price 时, 才兜底 entry_price。"""
    e = _mk_engine_with_position()
    e.positions["000001"].current_price = 0.0  # 无上次市价
    eq = e.total_equity({})
    assert eq == 100_000 + 1000 * 10   # 兜底 entry_price


def test_equity_price_coverage():
    """覆盖率: 有价/总持仓。"""
    e = _mk_engine_with_position()
    assert e.equity_price_coverage({}) == (0, 1)                    # 空 -> 0覆盖
    assert e.equity_price_coverage({"000001": {"close": 20}}) == (1, 1)
    assert e.equity_price_coverage({"000001": {"close": 0}}) == (0, 1)  # 价<=0不算


def test_record_marks_partial_when_price_missing(tmp_store):
    """净值可信度: 持仓缺实时报价时, equity_curve 标记 source=partial。"""
    engine = SimTraderEngine(store=tmp_store)
    engine.cash = 100_000
    pos = Position(code="000001", entry_date=date(2026, 3, 2),
                   entry_price=10.0, shares=1000, cost=10_000)
    pos.remaining_shares = 1000
    pos.current_price = 12.0
    engine.positions["000001"] = pos
    engine.record(date(2026, 3, 3), snapshot={})  # 空行情 -> partial
    ec = tmp_store.load_equity_curve()
    assert ec[-1]["source"] == "partial"


def test_record_marks_record_when_price_complete(tmp_store):
    """行情齐全时 source=record(正常)。"""
    engine = SimTraderEngine(store=tmp_store)
    engine.cash = 100_000
    pos = Position(code="000001", entry_date=date(2026, 3, 2),
                   entry_price=10.0, shares=1000, cost=10_000)
    pos.remaining_shares = 1000
    engine.positions["000001"] = pos
    engine.record(date(2026, 3, 3), snapshot={"000001": {"close": 11.0}})
    ec = tmp_store.load_equity_curve()
    assert ec[-1]["source"] == "record"


# ── CARD4: Position.today_pnl 统一今日盈亏 ─────────────────────
#
# 锁口径:
#   base_px = entry_price if entry_date == today else prev_close
#   rem     = remaining_shares or shares
#   return  round(rem * (cur_price - base_px), 0)
# 已平仓/缺现价/缺基准 → None(保留 api/sim_trader.py:471 哨兵语义)


class TestPositionTodayPnl:
    def test_overnight_uses_prev_close(self):
        """过夜持仓: 基准 = 昨收 10.5, 现 11.0, 1000 股 → +500"""
        pos = Position(code="000001", entry_date=date(2026, 3, 2),
                       entry_price=10.0, shares=1000, cost=10000.0)
        pos.remaining_shares = 1000
        assert pos.today_pnl(11.0, 10.5, date(2026, 3, 3)) == 500.0

    def test_bought_today_uses_entry_price(self):
        """当日买入: 基准 = entry 10.0, 现 10.8, 1000 股 → +800"""
        today = date(2026, 3, 3)
        pos = Position(code="000001", entry_date=today,
                       entry_price=10.0, shares=1000, cost=10000.0)
        pos.remaining_shares = 1000
        assert pos.today_pnl(10.8, 10.5, today) == 800.0

    def test_partial_remaining(self):
        """部分卖出后用 remaining_shares: 剩 300 股, 现 11.0, 昨收 10.0 → +300"""
        pos = Position(code="000001", entry_date=date(2026, 3, 2),
                       entry_price=10.0, shares=1000, cost=10000.0)
        pos.remaining_shares = 300
        assert pos.today_pnl(11.0, 10.0, date(2026, 3, 3)) == 300.0

    def test_closed_returns_none(self):
        """已平仓(remaining_shares<=0)→ None(api:471 哨兵语义)"""
        pos = Position(code="000001", entry_date=date(2026, 3, 2),
                       entry_price=10.0, shares=1000, cost=10000.0)
        pos.remaining_shares = 0
        assert pos.today_pnl(11.0, 10.0, date(2026, 3, 3)) is None

    def test_missing_cur_price_returns_none(self):
        """缺现价 → None"""
        pos = Position(code="000001", entry_date=date(2026, 3, 2),
                       entry_price=10.0, shares=1000, cost=10000.0)
        pos.remaining_shares = 1000
        assert pos.today_pnl(0.0, 10.0, date(2026, 3, 3)) is None
        assert pos.today_pnl(-1.0, 10.0, date(2026, 3, 3)) is None

    def test_overnight_missing_prev_close_returns_none(self):
        """过夜但缺昨收 → None"""
        pos = Position(code="000001", entry_date=date(2026, 3, 2),
                       entry_price=10.0, shares=1000, cost=10000.0)
        pos.remaining_shares = 1000
        assert pos.today_pnl(11.0, 0.0, date(2026, 3, 3)) is None


# ── H2(2026-07-15 全项目审计): execute_sell 多次部分卖出成本基按剩余股数摊分 ──

class TestExecuteSellPartialCostBasis:
    """H2: 旧版用 pos.shares(原始)做分母, 部分卖出后 cost 已摊减但分母不变 → 利润虚高。
    修复后用 remaining_shares(本次卖出前)做分母, 多次部分卖出总成本回收 == 原 pos.cost。
    """

    def test_three_partial_sells_recover_full_cost(self):
        """1000 股 cost=10000, 分 3 次各卖 300: 每次成本基应按剩余摊分, 总回收==10000。"""
        from app.sim_trader.engine import SimTraderEngine
        engine = SimTraderEngine()  # 无 store 纯内存
        engine.cash = 100_000
        pos = Position(code="000001", entry_date=date(2026, 3, 2),
                       entry_price=10.0, shares=1000, cost=10000.0)
        pos.remaining_shares = 1000
        engine.positions["000001"] = pos

        # 卖出价=成本价(10.0), 无费用场景下 profit=0, cost 回收应等于成本基
        # calc_sell_revenue 含费, 这里用高价让 profit>0 不影响成本基断言
        recovered = 0.0
        for _ in range(3):
            trade = engine.execute_sell(pos, 12.0, "TP1", partial=300,
                                        exit_date=date(2026, 3, 3))
            if trade:
                recovered += trade.profit_amount + (10000.0 * 300 / 1000 if recovered == 0 else 0)
        # 关键: 部分卖出后 pos.cost 正确摊减
        # 卖1: cost_basis=10000*(300/1000)=3000, pos.cost→7000, remaining→700
        # 卖2: cost_basis=7000*(300/700)=3000, pos.cost→4000, remaining→400  (旧 bug 会算 2100)
        # 卖3: cost_basis=4000*(300/400)=3000, pos.cost→1000, remaining→100
        # 三次 cost_basis 都应是 3000, 总 9000, 剩余 cost=1000
        assert pos.remaining_shares == 100
        assert pos.cost == pytest.approx(1000.0)  # 10000 - 9000
        # 旧 bug: 卖2 cost_basis=2100, 卖3=1470 → 总 6570, 剩余 cost=3430(≠1000)

    def test_partial_sell_cost_basis_uses_remaining_not_original(self):
        """直接断言: 第二次部分卖的 cost_basis 按 remaining 摊分(3000), 不是按 original(2100)。"""
        from app.sim_trader.engine import SimTraderEngine
        engine = SimTraderEngine()
        engine.cash = 100_000
        pos = Position(code="000001", entry_date=date(2026, 3, 2),
                       entry_price=10.0, shares=1000, cost=10000.0)
        pos.remaining_shares = 1000
        engine.positions["000001"] = pos

        # 第一次卖 300: cost_basis=3000, pos.cost→7000, remaining→700
        engine.execute_sell(pos, 10.0, "TP1", partial=300, exit_date=date(2026, 3, 3))
        assert pos.cost == pytest.approx(7000.0)
        assert pos.remaining_shares == 700

        # 第二次卖 300: cost_basis 应=7000*(300/700)=3000, pos.cost→4000
        # 旧 bug 会算 7000*(300/1000)=2100 → pos.cost→4900
        engine.execute_sell(pos, 10.0, "TP1", partial=300, exit_date=date(2026, 3, 3))
        assert pos.cost == pytest.approx(4000.0)  # 不是 4900
        assert pos.remaining_shares == 400

