"""本次整改的回归测试：成本计算 / 前复权 / 寻优口径 / 单位约定。

覆盖审计后的核心修复点，防止回退。这些函数不触发 DuckDB，可独立运行。
"""
import pytest
import pandas as pd

from app.backtest.execution import (
    calc_buy_cost, calc_sell_revenue, get_cost_cfg,
    realized_pnl,  # CARD1: 单笔已实现净盈亏(RED → GREEN in this test class)
)


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


# ───────────── CARD1: realized_pnl 单笔已实现净盈亏 ─────────────
#
# 锁口径:
#   sell_revenue = calc_sell_revenue(exit_price, shares)['total']
#   cb           = cost_basis if not None else calc_buy_cost(entry_price, shares)['total']
#   pnl          = sell_revenue - cb
#   ret_pct      = (pnl / cb * 100) if cb > 0 else 0.0  (百分比, 与 Trade.ret 一致)
# 部分卖出时,必传 cost_basis = pos.cost*(sell_shares/pos.shares),避免重复计 min_commission
# 破 Σprofit 资金守恒(各部分卖出佣金之和 > 整手佣金)。

class TestRealizedPnl:
    def test_full_sell_net_matches_hand_calc(self):
        """全平:pnl == sell_revenue_total - calc_buy_cost_total"""
        entry, exitp, sh = 10.0, 11.0, 1000
        cb = calc_buy_cost(entry, sh)["total"]  # 10015
        rp = realized_pnl(entry, exitp, sh, cost_basis=cb)
        assert rp["cost_basis"] == 10015.0
        assert rp["sell_revenue"] == calc_sell_revenue(exitp, sh)["total"]
        assert rp["pnl"] == rp["sell_revenue"] - 10015.0
        # 毛收益 10%, 净收益因双边成本拖累应在 (9, 10) 之间(参考 TestTradeCost.round_trip)
        assert 9.0 < rp["ret_pct"] < 10.0
        # ret_pct == pnl/cb*100
        assert abs(rp["ret_pct"] - rp["pnl"] / 10015.0 * 100) < 1e-9

    def test_cost_basis_none_uses_calc_buy_cost_equivalent(self):
        """cost_basis=None 内部重算,必须与显式传同一值等价"""
        rp1 = realized_pnl(10.0, 11.0, 1000)
        rp2 = realized_pnl(10.0, 11.0, 1000, cost_basis=calc_buy_cost(10.0, 1000)["total"])
        assert rp1 == rp2

    def test_partial_sell_prorated_cost_basis_conserves(self):
        """部分卖出:Σcb == pos.cost(按比例摊分,无重复计费)"""
        # 1000 股整手:pos.cost = calc_buy_cost(10, 1000) = 10015
        pos_cost = calc_buy_cost(10.0, 1000)["total"]
        # 拆 300 + 700,按比例摊分
        cb_a = pos_cost * (300 / 1000)
        cb_b = pos_cost * (700 / 1000)
        assert abs((cb_a + cb_b) - pos_cost) < 1e-9

    def test_partial_sell_does_not_re_double_min_commission(self):
        """关键防呆:小笔部分卖出不能每笔重算 min_commission(否则总和 > 整手)"""
        # 整手 100 股@10: gross 1000, 佣金 max(0.25, 5)=5; pos.cost = 1000+5+10 = 1015
        pos_cost = calc_buy_cost(10.0, 100)["total"]
        # 若每笔 50 股重算 calc_buy_cost(10, 50): gross 500, 佣金 max(0.125, 5)=5; 摊 2 笔 = 10 佣金
        bad = calc_buy_cost(10.0, 50)["total"] * 2  # 2 次重算的总和
        # bad > pos_cost (因为整手只触发一次 min_commission=5)
        assert bad > pos_cost + 1.0
        # 正确做法按比例摊分:Σcb == pos_cost(不破资金守恒)
        cb_a = pos_cost * (50 / 100)
        cb_b = pos_cost * (50 / 100)
        assert abs((cb_a + cb_b) - pos_cost) < 1e-9

    def test_zero_or_negative_shares_safe(self):
        """零股/负股 → 全零字典(防御 ss<=0 边界)"""
        z1 = realized_pnl(10.0, 11.0, 0)
        z2 = realized_pnl(10.0, 11.0, -5)
        for z in (z1, z2):
            assert z["cost_basis"] == 0.0
            assert z["sell_revenue"] == 0.0
            assert z["pnl"] == 0.0
            assert z["ret_pct"] == 0.0

    def test_ret_pct_is_percentage_unit_and_signed(self):
        """ret_pct 是百分比单位(非小数);亏时为负"""
        # 亏:entry=10, exit=9, cost_basis=含费基
        rp_loss = realized_pnl(10.0, 9.0, 1000, cost_basis=calc_buy_cost(10.0, 1000)["total"])
        assert rp_loss["ret_pct"] < 0
        # 数额一致: pnl == cost_basis * ret_pct / 100
        assert abs(rp_loss["pnl"] - rp_loss["cost_basis"] * rp_loss["ret_pct"] / 100) < 1e-6
        # 赚:反之
        rp_win = realized_pnl(10.0, 11.0, 1000, cost_basis=calc_buy_cost(10.0, 1000)["total"])
        assert rp_win["ret_pct"] > 0
