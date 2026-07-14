"""
ScreenerEngine 专项测试
覆盖 run_scan 编排器和各子方法的可独立测试性。
使用 Mock 避免依赖真实 parquet/DuckDB 数据。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.screener.engine import ScreenerEngine, _StrategyContext


@pytest.fixture
def engine():
    return ScreenerEngine()


@pytest.fixture
def fake_stocks():
    """模拟股票池 DataFrame"""
    return pd.DataFrame({
        "code": ["600000.SH", "000001.SZ", "300001.SZ"],
        "name": ["浦发银行", "平安银行", "特锐德"],
        "sector": ["银行", "银行", "电气设备"],
        "exchange": ["SH", "SZ", "SZ"],
        "concepts": ["金融", "金融", "充电桩"],
    })


# ─── _filter_stock_pool 子方法测试 ───────────────────────────────

def test_filter_stock_pool_no_filter(engine, fake_stocks):
    """无任何过滤时返回全部股票"""
    with patch("app.screener.engine.db") as mock_db:
        mock_db.get_all_stocks.return_value = fake_stocks
        mock_db.get_latest_sentiment.return_value = None
        result = engine._filter_stock_pool(None, None, None, None, None, False, lambda *a: None)
        assert len(result) == 3


def test_filter_stock_pool_by_exchange(engine, fake_stocks):
    """按交易所过滤"""
    with patch("app.screener.engine.db") as mock_db:
        mock_db.get_all_stocks.return_value = fake_stocks
        result = engine._filter_stock_pool(["SH"], None, None, None, None, False, lambda *a: None)
        assert len(result) == 1
        assert result.iloc[0]["code"] == "600000.SH"


def test_filter_stock_pool_by_sector(engine, fake_stocks):
    """按板块过滤"""
    with patch("app.screener.engine.db") as mock_db:
        mock_db.get_all_stocks.return_value = fake_stocks
        result = engine._filter_stock_pool(None, ["银行"], None, None, None, False, lambda *a: None)
        assert len(result) == 2


def test_filter_stock_pool_empty(engine):
    """股票池为空时返回空 DataFrame"""
    with patch("app.screener.engine.db") as mock_db:
        mock_db.get_all_stocks.return_value = pd.DataFrame(columns=["code", "name", "sector", "exchange"])
        result = engine._filter_stock_pool(None, None, None, None, None, False, lambda *a: None)
        assert result.empty


# ─── _apply_fundamentals_filter 子方法测试 ───────────────────────

def test_fundamentals_filter_no_filter(engine):
    """无基本面过滤参数时直接返回原 codes"""
    codes = ["600000.SH", "000001.SZ"]
    result = engine._apply_fundamentals_filter(codes, None, None, lambda *a: None)
    assert result == codes


def test_fundamentals_filter_filters_by_roe(engine):
    """按 ROE 过滤"""
    codes = ["600000.SH", "000001.SZ", "300001.SZ"]
    fund_df = pd.DataFrame({
        "code": ["600000.SH", "000001.SZ", "300001.SZ"],
        "roe": [15.0, 8.0, 20.0],
    })
    with patch("app.screener.engine.db") as mock_db:
        mock_db.conn.execute.return_value.df.return_value = fund_df
        result = engine._apply_fundamentals_filter(codes, {"min_roe": 10.0}, None, lambda *a: None)
        assert "600000.SH" in result  # roe=15 >= 10
        assert "300001.SZ" in result  # roe=20 >= 10
        assert "000001.SZ" not in result  # roe=8 < 10 被淘汰


def test_fundamentals_filter_empty_table(engine):
    """基本面表无记录时返回原 codes（降级不崩）"""
    codes = ["600000.SH"]
    with patch("app.screener.engine.db") as mock_db:
        mock_db.conn.execute.return_value.df.return_value = pd.DataFrame(columns=["code", "roe"])
        result = engine._apply_fundamentals_filter(codes, {"min_roe": 10.0}, None, lambda *a: None)
        assert result == codes  # 降级返回原列表


def test_fundamentals_filter_strategy_params_transparent(engine):
    """strategy_params 中的基本面参数透传到 fundamentals_filter"""
    codes = ["600000.SH"]
    fund_df = pd.DataFrame({"code": ["600000.SH"], "roe": [5.0]})
    with patch("app.screener.engine.db") as mock_db:
        mock_db.conn.execute.return_value.df.return_value = fund_df
        # strategy_params 带 min_roe，应被识别
        result = engine._apply_fundamentals_filter(codes, None, {"min_roe": 10.0}, lambda *a: None)
        assert result == []  # roe=5 < 10 被淘汰


# ─── _enrich_results 子方法测试 ──────────────────────────────────

def test_enrich_results_basic(engine, fake_stocks):
    """结果丰富化：合并元数据 + 字段映射"""
    signals_df = pd.DataFrame({
        "code": ["600000.SH"],
        "date": [pd.Timestamp("2024-06-01")],
        "close": [10.5],
    })
    result = engine._enrich_results(signals_df, fake_stocks, "daily", lambda *a: None)
    assert len(result) == 1
    r = result[0]
    assert r["code"] == "600000.SH"
    assert r["name"] == "浦发银行"
    assert r["buy_date"] == "2024-06-01"
    assert r["entry_price"] == 10.5


def test_enrich_results_empty_signals(engine, fake_stocks):
    """空信号不崩"""
    signals_df = pd.DataFrame(columns=["code", "date", "close"])
    result = engine._enrich_results(signals_df, fake_stocks, "daily", lambda *a: None)
    assert result == []


def test_enrich_results_sector_score_default_none(engine, fake_stocks):
    """板块评分失败时置 None（L-04 修复）"""
    signals_df = pd.DataFrame({
        "code": ["600000.SH"],
        "date": [pd.Timestamp("2024-06-01")],
        "close": [10.5],
    })
    # batch_score_stocks_detail 抛异常 → 评分应置 None
    import app.hot_sector.engine as hse
    with patch.object(hse, "hot_sector_engine") as mock_hse:
        mock_hse.batch_score_stocks_detail.side_effect = RuntimeError("boom")
        result = engine._enrich_results(signals_df, fake_stocks, "daily", lambda *a: None)
    assert result[0]["sector_score"] is None
    assert result[0]["concept_score"] is None
    assert result[0]["total_score"] is None


# ─── _StrategyContext 数据类测试 ─────────────────────────────────

def test_strategy_context_dataclass():
    """上下文数据类可正确构造"""
    ctx = _StrategyContext(
        module_path="app.screener.strategies.ma5_angle",
        needs_market=False,
        needs_all_stock=False,
        market_df=None,
        all_stock_df=None,
    )
    assert ctx.module_path == "app.screener.strategies.ma5_angle"
    assert ctx.needs_market is False


# ─── run_scan 编排器端到端测试（Mock 全链路）────────────────────

def test_run_scan_empty_pool_returns_empty(engine):
    """空股票池直接返回空列表，不进入后续流程"""
    with patch("app.screener.engine.db") as mock_db:
        mock_db.get_all_stocks.return_value = pd.DataFrame(columns=["code", "name", "sector", "exchange"])
        mock_db.get_latest_sentiment.return_value = None
        result = engine.run_scan(
            strategy_name="MA5角度_原版",
            freq="daily",
            progress_callback=lambda *a: None,
        )
        assert result == []


def test_run_scan_orchestration_flow(engine, fake_stocks):
    """编排器全流程：股票池→基本面(无)→上下文→扫描→丰富化→存历史"""
    with patch("app.screener.engine.db") as mock_db, \
         patch("app.screener.engine.get_strategy_info", return_value="app.screener.strategies.ma5_angle"), \
         patch("app.screener.engine.load_strategy") as mock_load, \
         patch("app.screener.engine._scan_worker") as mock_worker, \
         patch("app.screener.engine.settings") as mock_settings:

        mock_db.get_all_stocks.return_value = fake_stocks
        mock_db.get_latest_sentiment.return_value = None
        mock_db.get_strategies.return_value = pd.DataFrame({"name": ["MA5角度_原版"], "id": [1]})
        mock_db.save_scan_result = MagicMock()

        # load_strategy 返回一个简单 mock 策略
        mock_strategy = MagicMock()
        mock_strategy.generate_signals.__signature__ = None
        mock_load.return_value = mock_strategy
        mock_settings.get.return_value = None  # 非 qmt 网关

        # _scan_worker 返回一个信号 DataFrame
        mock_worker.return_value = pd.DataFrame({
            "code": ["600000.SH"],
            "date": [pd.Timestamp("2024-06-01")],
            "close": [10.5],
        })

        result = engine.run_scan(
            strategy_name="MA5角度_原版",
            freq="daily",
            progress_callback=lambda *a: None,
        )
        assert len(result) == 1
        assert result[0]["code"] == "600000.SH"
        # 历史记录被保存
        mock_db.save_scan_result.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
