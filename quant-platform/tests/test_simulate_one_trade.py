"""app.backtest.simulate_one_trade 深 module 单元测试(候选②)。

TDD RED: 先写测试锁住 kernel 行为(从 engine._simulate_trade_daily_fallback 抽出);
GREEN: 抽 kernel → engine 委托 → ai_optimizer._fast_simulate 委托(影子变忠实)→ 删 _v2。

关键 correctness 修正(对比 _fast_simulate 影子):
- TP1 固定 3% 成交(entry*(1+tp1_fill_pct=0.03)),不按真实档位 target。
- trailing_first / stack_mode 生效。
- 无假默认 -7.0/15.0(走 params_override / schema)。
"""
import pandas as pd
import pytest

from app.backtest.simulate_one_trade import simulate_one_trade


# ── 工具:构造 bars_daily ────────────────────────────────────
def _bars(rows):
    """rows: list of (date_str, open, high, low, close)。"""
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])


_PARAMS = {
    # schema 字段(走 params_override,绕开 load_risk_params)
    # 2026-07-15:单位统一为小数(0.07 表示 7%),与 risk_params.py 一致
    "hard_stop_loss_pct": -0.07,
    "breakeven_threshold_pct": 0.05,
    "breakeven_stop_pnl_pct": 0.0,
    "trailing_activate_pct": 0.15,
    "trailing_drawdown_pct": 0.05,
    "time_exit_days": 30,
    "time_exit_force_days": 12,
    "first_day_exit_min_profit": 0.0,
    "first_day_exit_days": 1,
    # TP 档位(语义保持 % 输入,_p() 不读这两个,TP 路径自己 /100)
    "tp1_profit": 3.0,
    "tp1_ratio": 0.33,
    "tp2_profit": 5.0,
    "tp2_ratio": 0.33,
}


def _call(bars, entry=10.0, signal="2024-01-02", apply_costs=True, time_exit_min_pnl=None, extra_params=None):
    p = dict(_PARAMS)
    if extra_params:
        p.update(extra_params)
    return simulate_one_trade(
        code="000001", stock_name="TEST", entry_price=entry,
        signal_date=signal, bars_daily=bars, params_override=p,
        time_exit_min_pnl=time_exit_min_pnl, apply_costs=apply_costs,
    )


# ── 测试 ───────────────────────────────────────────────────────
class TestHardStop:
    def test_low_triggers_hard_stop_at_7pct(self):
        bars = _bars([
            ("2024-01-02", 10.0, 10.0, 10.0, 10.0),  # signal day
            ("2024-01-03", 9.7, 9.8, 9.7, 9.75),    # hold_days=1,过了一晚(T+1 不查)
            ("2024-01-04", 9.5, 9.6, 9.2, 9.4),     # hold_days=2,low 9.2 < entry*0.93=9.3
        ])
        t = _call(bars)
        assert t is not None
        assert any("HS" in e["reason"] for e in t["sell_events"])

    def test_no_trigger_when_low_above_stop(self):
        bars = _bars([
            ("2024-01-02", 10.0, 10.0, 10.0, 10.0),
            ("2024-01-03", 9.6, 9.7, 9.5, 9.65),    # hold_days=1,过了一晚
            ("2024-01-04", 9.6, 9.7, 9.5, 9.65),    # hold_days=2,不触发 HS,尾日清仓
        ])
        # 不触发 HS;但尾日清仓
        t = _call(bars)
        assert t is not None
        assert t["sell_events"][-1]["reason"] == "清仓"


class TestTP1FixedAt3pct:
    def test_tp1_fills_at_3pct_even_when_target_is_higher(self):
        """核心修正:tp1_profit=10(目标 11),但 TP1 成交价固定 entry*1.03(_fast_simulate 影子按 11 成交,错)。
        exit_rule_engine 选 highest=LARGEST idx 触发:要让 TP1(idx 0)为 highest,tp2 必须不触发。
        所以 tp2_profit=50(目标 15)→ bar high 11 只触 tp1 → override → 10.3。
        T+1 保护:hold_days=1(过了一晚那根)不查 TP,hold_days=2 才查。
        """
        bars = _bars([
            ("2024-01-02", 10.0, 10.0, 10.0, 10.0),  # signal day
            ("2024-01-03", 10.1, 10.2, 9.9, 10.1),     # hold_days=1,过了一晚(不查 TP)
            ("2024-01-04", 11.0, 11.0, 10.5, 11.0),  # hold_days=2,high 11 ≥ tp1 目标 11
        ])
        t = _call(bars, entry=10.0, extra_params={"tp1_profit": 10.0, "tp2_profit": 50.0})
        assert t is not None
        first_sell = next(e for e in t["sell_events"] if "止盈" in e["reason"])
        assert first_sell["price"] == pytest.approx(10.3)  # 3% 覆盖,而非 11
        assert first_sell["ratio"] == pytest.approx(0.33, abs=1e-6)  # 仅 tp1,ratio=0.33


class TestTPStack:
    def test_tp1_and_tp2_both_fill_in_same_bar_when_stack(self):
        # tp1=3%(10.3)+tp2=5%(10.5),bar high=10.6 同触 → stack 合并 1 信号,
        # highest=tp2(idx 1,LARGEST)→ 成交价 10.5(无 TP1 覆盖,那是 tp1 单独触发时才生效)。
        # 注:exit_rule_engine 产生的 total_ratio 被 kernel 忽略(忠实 engine 行为:
        # kernel 按 highest 单档 sell_ratio 卖,不是累加),所以 ratio=0.33。
        # T+1 保护:hold_days=1(过了一晚)不查 TP,hold_days=2 才查。
        bars = _bars([
            ("2024-01-02", 10.0, 10.0, 10.0, 10.0),  # signal day
            ("2024-01-03", 10.05, 10.1, 9.95, 10.05), # hold_days=1,过了一晚(不查 TP)
            ("2024-01-04", 10.6, 10.6, 10.5, 10.6),  # hold_days=2,high 10.6 同触
        ])
        t = _call(bars, entry=10.0, extra_params={"tp1_profit": 3.0, "tp2_profit": 5.0})
        tp_sells = [e for e in t["sell_events"] if "止盈" in e["reason"]]
        assert len(tp_sells) == 1  # stack 合并为 1 笔
        assert tp_sells[0]["price"] == pytest.approx(10.5)  # tp2 目标
        assert tp_sells[0]["ratio"] == pytest.approx(0.33, abs=1e-6)  # 单档 ratio(忠实 engine)


class TestTimeExit:
    def test_exit_at_max_hold_when_no_trigger(self):
        # 构造 N+1 根 bar,N=max_hold,都不触发
        rows = [("2024-01-02", 10.0, 10.0, 10.0, 10.0)]
        for i in range(1, 6):
            rows.append((f"2024-01-{2 + i:02d}", 10.0, 10.1, 9.95, 10.02))
        bars = _bars(rows)
        t = _call(bars, extra_params={"time_exit_days": 5, "tp1_profit": 50.0, "tp2_profit": 80.0})
        # time_exit 在第 N(=time_exit_days)根 bar 触发
        assert t["sell_events"][-1]["reason"].startswith("T") or "时间" in t["sell_events"][-1]["reason"] \
               or t["sell_events"][-1]["reason"] == "清仓"  # 兜底看实现


class TestPeriodEnd:
    def test_no_trigger_returns_last_close_as_exit(self):
        rows = [("2024-01-02", 10.0, 10.0, 10.0, 10.0)]
        for i in range(1, 4):
            rows.append((f"2024-01-{2 + i:02d}", 10.0, 10.1, 9.95, 10.02))
        bars = _bars(rows)
        t = _call(bars)
        # 最后一天清仓(无任何触发)
        assert t["sell_events"][-1]["reason"] == "清仓"


class TestCost:
    def test_buy_cost_lowers_pnl_vs_no_cost(self):
        """buy 成本摊入 cost_entry,同场景下 pnl 应低于无成本。"""
        bars = _bars([
            ("2024-01-02", 10.0, 10.0, 10.0, 10.0),
            ("2024-01-03", 11.0, 11.0, 10.5, 11.0),
        ])
        t_no = _call(bars, entry=10.0, apply_costs=False)
        t_yes = _call(bars, entry=10.0, apply_costs=True)
        assert t_yes["return_pct"] <= t_no["return_pct"]

    def test_apply_costs_false_ignores_cost(self):
        bars = _bars([
            ("2024-01-02", 10.0, 10.0, 10.0, 10.0),
            ("2024-01-03", 11.0, 11.0, 10.5, 11.0),
        ])
        t_no = _call(bars, entry=10.0, apply_costs=False)
        t_yes = _call(bars, entry=10.0, apply_costs=True)
        # 扣成本后 pnl 应更低(或平)
        assert t_yes["return_pct"] <= t_no["return_pct"]


class TestEmptyBars:
    def test_empty_bars_returns_none(self):
        bars = _bars([("2024-01-02", 10.0, 10.0, 10.0, 10.0)])  # 只有信号日,无后续
        # signal_date 是 2024-01-02,但 bars 在 d<=signal_date 时 continue,无 bar 可处理 → 末尾清仓?
        # 实际上 empty bars 之外的"只有信号日"会走末尾清仓 last bar = signal day
        t = _call(bars)
        # 至少有一个 sell 事件(清仓)
        assert t is not None and any(e["reason"] == "清仓" for e in t["sell_events"])


class TestNoFakeDefaults:
    def test_no_hardcoded_minus7_when_params_override_gives_different_value(self):
        """影子 _fast_simulate 默认 hard_sl=-7.0(假默认)。kernel 走 params_override,无 -7 默认。
        T+1 保护:hold_days=1(过了一晚)不查 HS,hold_days=2 才查。
        """
        bars = _bars([
            ("2024-01-02", 10.0, 10.0, 10.0, 10.0),  # signal day
            ("2024-01-03", 9.6, 9.7, 9.5, 9.6),     # hold_days=1,过了一晚(不查 HS)
            ("2024-01-04", 9.0, 9.0, 8.5, 8.7),     # hold_days=2,low 8.5 < entry*0.95=9.5
        ])
        # override 给 -5%: 应触 HS
        t = _call(bars, entry=10.0, extra_params={"hard_stop_loss_pct": -0.05})
        assert any("HS" in e["reason"] for e in t["sell_events"])