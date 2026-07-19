"""锁死 AI 优化器搜索空间 → 回测内核的单位转换。

背景(2026-07-20): 搜索空间采样值是百分数(hard_stop_loss_pct=-7 表示 -7%),
但回测内核期望小数:
  - simulate_one_trade params_override: hard_stop/trail_*/breakeven_*/first_day_exit 小数,
    tp*_profit 百分数(内核自 /100)
  - run_tdx_backtest: hard_stop/trail_* 小数命名

两条路径各自的转换函数必须把百分数 pct 字段 /100, 否则量纲错误(如 hard_stop=-7
被当小数, entry*(1-7)=负数, 硬止损崩坏)。
"""
import sys
from unittest import mock

# ai_optimizer 模块级 `from database.duckdb_manager import db` 会初始化 DuckDB 单例,
# 测试环境常被运行中的实盘/模拟盘服务占用 meta.db。被测的两个转换函数是纯 Python,
# 预先 mock 掉 DB 模块即可隔离(不影响转换逻辑的正确性)。
sys.modules.setdefault("database.duckdb_manager", mock.MagicMock())

from app.backtest.ai_optimizer import (
    _ai_params_to_tdx_params,
    _search_space_to_python_override,
)


class TestSearchSpaceToPythonOverride:
    """Python 策略路径: _run_trial → _fast_simulate → simulate_one_trade"""

    def test_pct_fields_divided_by_100(self):
        """搜索空间的 6 个 pct 字段(百分数) → params_override 小数"""
        params = {
            "hard_stop_loss_pct": -7.0,       # -7%
            "trailing_activate_pct": 5.0,     # 5%
            "trailing_drawdown_pct": 2.0,     # 2%
            "breakeven_threshold_pct": 3.0,   # 3%
            "breakeven_stop_pnl_pct": 1.0,    # 1%
            "first_day_exit_min_profit": 4.0, # 4%
        }
        out = _search_space_to_python_override(params)
        assert out["hard_stop_loss_pct"] == -0.07
        assert out["trailing_activate_pct"] == 0.05
        assert out["trailing_drawdown_pct"] == 0.02
        assert out["breakeven_threshold_pct"] == 0.03
        assert out["breakeven_stop_pnl_pct"] == 0.01
        assert out["first_day_exit_min_profit"] == 0.04

    def test_tp_profit_kept_as_percentage(self):
        """tp*_profit 保持百分数(内核 TP 路径自己 /100), 不在这里二次转换"""
        params = {"tp1_profit": 3.0, "tp2_profit": 6.0, "tp3_profit": 9.0}
        out = _search_space_to_python_override(params)
        assert out["tp1_profit"] == 3.0
        assert out["tp2_profit"] == 6.0
        assert out["tp3_profit"] == 9.0

    def test_tp_ratio_and_int_fields_preserved(self):
        """tp*_ratio(小数)与整数字段原样透传"""
        params = {
            "tp1_ratio": 0.33, "tp2_ratio": 0.34,
            "time_exit_days": 7, "time_exit_force_days": 12,
            "first_day_exit_days": 1,
        }
        out = _search_space_to_python_override(params)
        assert out["tp1_ratio"] == 0.33
        assert out["time_exit_days"] == 7
        assert out["first_day_exit_days"] == 1

    def test_does_not_mutate_input(self):
        """不污染外层 params(_summarize_result 仍要用原始百分数展示 Top-10)"""
        params = {"hard_stop_loss_pct": -7.0, "tp1_profit": 3.0}
        _search_space_to_python_override(params)
        assert params["hard_stop_loss_pct"] == -7.0   # 原值不变
        assert params["tp1_profit"] == 3.0


class TestAiParamsToTdxParams:
    """TDX 策略路径: _run_trial_tdx → run_tdx_backtest"""

    def test_pct_fields_to_decimal_with_renamed_keys(self):
        """百分数 pct → 小数, 且 key 从 *_pct 改名为引擎的 hard_stop/trail_*"""
        ai_params = {
            "hard_stop_loss_pct": -7.0,
            "trailing_activate_pct": 5.0,
            "trailing_drawdown_pct": 2.0,
        }
        p = _ai_params_to_tdx_params(ai_params, {"start_date": "2024-01-01"})
        assert p["hard_stop"] == -0.07
        assert p["trail_activate"] == 0.05
        assert p["trail_dd"] == 0.02

    def test_breakeven_divided_by_100(self):
        """breakeven_* 搜索空间百分数 → 引擎小数(2026-07-20 修复, 原透传是 bug)"""
        ai_params = {
            "breakeven_threshold_pct": 2.0,   # 搜索空间百分数 2%
            "breakeven_stop_pnl_pct": 0.5,    # 搜索空间百分数 0.5%
        }
        p = _ai_params_to_tdx_params(ai_params, {})
        assert p["breakeven_threshold_pct"] == 0.02
        assert p["breakeven_stop_pnl_pct"] == 0.005

    def test_tp_profit_to_decimal_in_tiers(self):
        """tp*_profit 百分数 → tiers.profit_pct 小数; sell_ratio 不变"""
        ai_params = {
            "tp1_profit": 3.0, "tp1_ratio": 0.33,
            "tp2_profit": 6.0, "tp2_ratio": 0.34,
        }
        p = _ai_params_to_tdx_params(ai_params, {})
        assert p["take_profit_tiers"][0]["profit_pct"] == 0.03
        assert p["take_profit_tiers"][0]["sell_ratio"] == 0.33
        assert p["take_profit_tiers"][1]["profit_pct"] == 0.06
