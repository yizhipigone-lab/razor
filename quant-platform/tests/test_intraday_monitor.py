"""
IntradayMonitor 回归测试 — 锁死 NameError bug 不复发

背景: intraday_monitor._check_position 在 v5.5 重构后引用未定义的 ctx/overall_peak,
盘中 tick 命中持仓必崩 NameError。本测试直接调用 _check_position 验证:
  1. 不再抛 NameError
  2. 跌破硬止损触发 HS (T+1 后, hold_days>=2)
  3. 价格平稳时不误触发
  4. session_peak 抬升峰值后移动止盈触发 TR
  5. TP1 部分卖返回 100 整数倍股数

注: 规则有 T+1 护栏(hold_days<2 不触发 HS/TR/TP), 故 monkeypatch _calc_hold_days=5
    隔离日历依赖, 并避开 TF(>12)/TC(>7) 干扰。
"""
from datetime import date

import pytest

from app.sim_trader.engine import SimTraderEngine
from app.sim_trader.intraday_monitor import IntradayMonitor
from app.sim_trader.models import Position
from app.config.risk_params import RiskParams


@pytest.fixture
def engine():
    e = SimTraderEngine()  # 无 store, 纯内存
    e.cash = 1_000_000
    return e


@pytest.fixture
def monitor(engine, monkeypatch):
    m = IntradayMonitor(engine)
    # 隔离日历: 固定 hold_days=5 (在 [2,12] 区间, 避开 TF/TC, 满足 T+1 护栏)
    monkeypatch.setattr(m, "_calc_hold_days", lambda entry_date: 5)
    return m


@pytest.fixture
def fixed_risk(monkeypatch):
    """固定风控参数, 杜绝对 settings/app_setting.json 的环境依赖。"""
    rp = RiskParams(
        hard_stop=-0.06, trail_activate=0.05, trail_dd=0.02,
        take_profit_tiers=[{"profit_pct": 0.03, "sell_ratio": 0.30}],
        time_exit_days=7, time_exit_profit=0.03, time_force_days=12,
        first_day_exit_min_profit=0.0, first_day_exit_days=1,
        use_atr_trail=False, atr_trail_multiplier=1.0,
    )
    import app.config.risk_params as rpmod
    monkeypatch.setattr(rpmod, "load_risk_params", lambda: rp)
    return rp


def _make_pos(entry=10.0):
    pos = Position(code="000001", entry_date=date.today(),
                   entry_price=entry, shares=1000, cost=entry * 1000)
    pos.remaining_shares = 1000
    return pos


def test_check_position_no_crash_on_flat_price(monitor, fixed_risk):
    """回归核心: 价格平稳时调用不崩 NameError, 且无信号。"""
    pos = _make_pos(10.0)
    result = monitor._check_position(pos, 10.05, 10.05)
    assert result is None


def test_check_position_hard_stop_triggers(monitor, fixed_risk):
    """跌破硬止损(-6%)触发 HS, 全卖 partial=None。
    stop_price = 10*(1-0.06)=9.4, low=9.0 <= 9.4 → HS。"""
    pos = _make_pos(10.0)
    result = monitor._check_position(pos, 9.0, 9.0)  # -10% < -6%
    assert result is not None
    assert result[0].startswith("HS")
    assert result[1] is None  # HS 全卖


def test_check_position_trailing_stop_triggers(monitor, fixed_risk):
    """峰值抬升后回撤超 trail_dd 触发 TR (验证 session_peak 真的进了 ctx)。

    trailing_first 顺序下 TP 先于 TR, 故先标记 tp1_triggered=True 跳过 TP。
    entry=10, session_peak=12(+20%>trail_activate 5%), 现价 11.5。
    pos.peak_price 被抬升到 12, dd_from_peak = 11.5/12-1 = -4.17% > trail_dd 2% → TR。
    """
    pos = _make_pos(10.0)
    pos.tp1_triggered = True  # 跳过 TP, 让 TR 有机会触发
    result = monitor._check_position(pos, 11.5, 12.0)
    assert result is not None
    assert result[0].startswith("TR")
    # 验证峰值确实被抬升(session_peak 进了 pos)
    assert pos.peak_price == 12.0


def test_check_position_tp_partial_returns_qty(monitor, fixed_risk):
    """+4% 触发 TP1(use_high_for_tp, high=10.4 >= target 10.3), 部分卖 300 股。"""
    pos = _make_pos(10.0)
    result = monitor._check_position(pos, 10.4, 10.4)
    assert result is not None
    assert result[0].startswith("TP")
    # sell_ratio=0.30 * 1000 = 300, 100 的整数倍 → 300
    assert result[1] == 300


def test_check_position_session_peak_does_not_lower_historic_peak(monitor, fixed_risk):
    """session_peak 低于历史峰值时, 不应拉低 pos.peak_price(峰值只升不降)。"""
    pos = _make_pos(10.0)
    pos.peak_price = 15.0  # 历史已抬到 15
    monitor._check_position(pos, 11.0, 11.0)  # session_peak 11 < 15
    assert pos.peak_price == 15.0  # 未被拉低
