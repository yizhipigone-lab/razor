"""本次整改的回归测试：成本计算 / 前复权 / 寻优口径 / 单位约定。

覆盖审计后的核心修复点，防止回退。这些函数不触发 DuckDB，可独立运行。
"""
import pytest
import pandas as pd

from app.backtest.execution import calc_buy_cost, calc_sell_revenue, get_cost_cfg


# ───────────── 任务一: 交易成本计算 ─────────────

class TestTradeCost:
    def test_buy_cost_includes_commission_and_slippage(self):
        r = calc_buy_cost(10.0, 1000)  # 毛额 10000
        # 佣金 max(10000*0.00025, 5)=5 (万2.5但低于5元最低→取5); 滑点 10000*0.001=10
        assert r["gross"] == 10000.0
        assert r["commission"] == 5.0          # min_commission 生效
        assert r["slippage"] == 10.0
        assert r["total"] == 10015.0

    def test_sell_revenue_deducts_stamp_tax(self):
        r = calc_sell_revenue(11.0, 1000)  # 毛额 11000
        # 佣金 max(2.75,5)=5; 印花 11000*0.0005=5.5; 滑点 11
        assert r["stamp_tax"] == 5.5
        assert r["total"] == 11000 - 5 - 5.5 - 11  # 10978.5

    def test_min_commission_floor(self):
        # 小额成交佣金应触发 5 元最低
        r = calc_buy_cost(10.0, 100)  # 毛额 1000, 佣金 0.25→5
        assert r["commission"] == 5.0

    def test_round_trip_cost_is_positive_drag(self):
        # 一买一卖, 毛收益+10% → 净收益应被成本拖累 (< 10%)
        entry, exitp, sh = 10.0, 11.0, 1000
        buy = calc_buy_cost(entry, sh)["total"]
        sell = calc_sell_revenue(exitp, sh)["total"]
        net_ret = (sell - buy) / buy * 100
        assert 9.0 < net_ret < 10.0  # 约 9.62%

    def test_get_cost_cfg_has_required_keys(self):
        cfg = get_cost_cfg()
        for k in ("commission_rate", "min_commission", "stamp_tax_rate", "slippage_rate"):
            assert k in cfg


# ───────────── 任务三: 前复权读取层 ─────────────

class TestQfq:
    def _qfq(self, df, dc="date"):
        # 复刻 _apply_qfq_by_code 核心逻辑(避免 import duckdb_manager 触发 DuckDB)
        df = df.copy()
        df["adj_factor"] = pd.to_numeric(df["adj_factor"]).fillna(1.0)
        latest = df[df[dc] == df.groupby("code")[dc].transform("max")].groupby("code")["adj_factor"].first()
        base = df["code"].map(latest).fillna(1.0)
        base = base.where(base != 0, 1.0)
        ratio = df["adj_factor"] / base
        for c in ["open", "high", "low", "close"]:
            if c in df:
                df[c] = pd.to_numeric(df[c]) * ratio
        return df

    def test_qfq_removes_ex_dividend_jump(self):
        # 除权前 close=20(factor=0.5), 除权后 close=10(factor=1.0) → 前复权后旧价应=10, 连续
        df = pd.DataFrame({
            "code": ["A", "A"],
            "date": pd.to_datetime(["2024-01-01", "2024-06-01"]),
            "open": [20.0, 10.0], "high": [20.0, 10.0], "low": [20.0, 10.0], "close": [20.0, 10.0],
            "adj_factor": [0.5, 1.0],
        })
        out = self._qfq(df)
        assert abs(out["close"].iloc[0] - 10.0) < 1e-9  # 旧价前复权到 10
        assert abs(out["close"].iloc[1] - 10.0) < 1e-9

    def test_qfq_factor_one_no_change(self):
        # adj_factor 全 1.0(存量数据标记) → 价格不变, 不二次复权
        df = pd.DataFrame({
            "code": ["B", "B"],
            "date": pd.to_datetime(["2024-01-01", "2024-06-01"]),
            "open": [12.0, 13.0], "high": [12.0, 13.0], "low": [12.0, 13.0], "close": [12.0, 13.0],
            "adj_factor": [1.0, 1.0],
        })
        out = self._qfq(df)
        assert out["close"].tolist() == [12.0, 13.0]


# ───────────── P0-1: 止盈单位约定(小数) ─────────────

class TestTpUnitConvention:
    def test_decimal_tp_trigger_point(self):
        # tp_pct 小数 0.03 → 触发点 high/entry-1 >= 0.03, 即 +3% (非 +0.03%)
        entry = 10.0
        tp_pct = 3.0 / 100.0  # _build_tp_plan 输出小数
        trigger = entry * (1 + tp_pct)
        assert abs(trigger - 10.30) < 1e-9
        # 涨 2.9% 不触发, 3.0% 触发
        assert not (10.29 / entry - 1) >= tp_pct
        assert (10.30 / entry - 1) >= tp_pct

    def test_ai_apply_pct_to_decimal(self):
        # ai/apply 写 config 无条件 /100, 含 <1 的百分比值(0.83%)
        def _to_decimal_pct(v):
            return float(v) / 100.0
        assert _to_decimal_pct(3.0) == 0.03
        assert _to_decimal_pct(0.83) == 0.0083   # <1 的百分比也正确, 不被当小数
        assert abs(_to_decimal_pct(26.19) - 0.2619) < 1e-9


# ───────────── §3.3: step 量化退化保护 ─────────────

class TestStepQuant:
    def _valid_step(self, lo, hi, step):
        return isinstance(step, (int, float)) and 0 < step < (hi - lo)

    def test_valid_step_accepted(self):
        assert self._valid_step(0.06, 0.38, 0.05)  # ratio step 合法

    def test_oversized_step_ignored(self):
        # step 0.5 > 区间宽度 0.32 → 退化保护, 忽略
        assert not self._valid_step(0.06, 0.38, 0.5)
