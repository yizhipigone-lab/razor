import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import pandas as pd


@pytest.fixture
def sample_bars_df():
    """一只股票的日线样本(含 adj_factor), 供回测/复权测试复用。"""
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"])
    return pd.DataFrame({
        "code": ["000001"] * 5,
        "date": dates,
        "open":  [10.0, 10.2, 10.5, 10.3, 10.8],
        "high":  [10.3, 10.6, 10.7, 10.5, 11.0],
        "low":   [9.9, 10.1, 10.3, 10.1, 10.6],
        "close": [10.2, 10.5, 10.4, 10.4, 10.9],
        "volume": [1000, 1200, 900, 1100, 1300],
        "amount": [10200, 12600, 9360, 11440, 14170],
        "adj_factor": [1.0, 1.0, 1.0, 1.0, 1.0],
    })


@pytest.fixture
def default_cost_cfg():
    """默认成本配置(与 execution.DEFAULT_COST_CFG / config.backtest.cost 一致)。"""
    return {
        "commission_rate": 0.00025,
        "min_commission": 5.0,
        "stamp_tax_rate": 0.0005,
        "slippage_rate": 0.001,
    }
