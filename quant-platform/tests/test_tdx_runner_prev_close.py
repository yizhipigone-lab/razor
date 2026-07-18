import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.backtest.tdx_runner import get_prev_close_from_parquet


class TestGetPrevCloseFromParquet:
    def test_reads_previous_day_close_string_date(self, tmp_path):
        code = "600000"
        parquet_dir = tmp_path / "daily"
        parquet_dir.mkdir()
        df = pd.DataFrame({
            "date": ["20240101", "20240102", "20240103"],
            "close": [10.0, 10.5, 11.0],
        })
        df.to_parquet(parquet_dir / f"{code}.parquet", index=False)

        prev_close = get_prev_close_from_parquet(code, "20240103", parquet_dir=str(parquet_dir))
        assert prev_close == 10.5

    def test_reads_previous_day_close_datetime_index(self, tmp_path):
        code = "600000"
        parquet_dir = tmp_path / "daily"
        parquet_dir.mkdir()
        df = pd.DataFrame({
            "close": [10.0, 10.5, 11.0],
        }, index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]))
        df.to_parquet(parquet_dir / f"{code}.parquet")

        prev_close = get_prev_close_from_parquet(code, "20240103", parquet_dir=str(parquet_dir))
        assert prev_close == 10.5

    def test_no_previous_day_returns_none(self, tmp_path):
        code = "600000"
        parquet_dir = tmp_path / "daily"
        parquet_dir.mkdir()
        df = pd.DataFrame({
            "date": ["20240101"],
            "close": [10.0],
        })
        df.to_parquet(parquet_dir / f"{code}.parquet", index=False)

        prev_close = get_prev_close_from_parquet(code, "20240101", parquet_dir=str(parquet_dir))
        assert prev_close is None

    def test_missing_file_returns_none(self, tmp_path):
        prev_close = get_prev_close_from_parquet("600000", "20240103", parquet_dir=str(tmp_path))
        assert prev_close is None
