"""验证 #7 修复: amount 字段不被 × 1000"""
import os
import sys
# 确保项目根目录可被 import
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pandas as pd
from unittest.mock import patch


def test_download_daily_amount_not_scaled():
    """download_daily_bars 中 amount 不应被 × 1000"""
    fake_df = pd.DataFrame({
        'trade_date': ['20250101', '20250102'],
        'open': [10.0, 10.5],
        'high': [10.5, 11.0],
        'low': [9.8, 10.2],
        'close': [10.2, 10.8],
        'vol': [1000000, 1100000],
        'amount': [100000000.0, 110000000.0]
    })
    with patch.dict(os.environ, {'TUSHARE_KEY': 'fake_key'}):
        with patch('tushare.set_token'):
            with patch('tushare.pro_bar', return_value=fake_df):
                from app.data_manager.engine import download_daily_bars
                result = download_daily_bars('000001', 1)
                assert result is not None, "download_daily_bars 返回 None"
                first_amount = result.iloc[0]['amount']
                assert first_amount == 100000000.0, f"amount 仍被放大: {first_amount}"
                print(f"OK download_daily_bars amount = {first_amount:.0f} (未被 x 1000)")


def test_download_min5_amount_not_scaled():
    """download_min5_bars 中 amount 不应被 × 1000"""
    fake_df = pd.DataFrame({
        'trade_time': ['20250101 09:35:00', '20250101 09:40:00'],
        'open': [10.0, 10.5],
        'high': [10.5, 11.0],
        'low': [9.8, 10.2],
        'close': [10.2, 10.8],
        'vol': [50000, 55000],
        'amount': [5000000.0, 5500000.0]
    })
    with patch.dict(os.environ, {'TUSHARE_KEY': 'fake_key'}):
        with patch('tushare.set_token'):
            with patch('tushare.pro_bar', return_value=fake_df):
                from app.data_manager.engine import download_min5_bars
                result = download_min5_bars('000001', 100)
                assert result is not None, "download_min5_bars 返回 None"
                first_amount = result.iloc[0]['amount']
                assert first_amount == 5000000.0, f"amount 仍被放大: {first_amount}"
                print(f"OK download_min5_bars amount = {first_amount:.0f} (未被 x 1000)")


def test_no_multiply_1000_in_source():
    """engine.py 源码中不应再有 * 1000"""
    engine_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'app', 'data_manager', 'engine.py'
    )
    with open(engine_path, 'r', encoding='utf-8') as f:
        content = f.read()
    bad_patterns = [
        "df['amount'] = df['amount'] * 1000",
        'df["amount"] = df["amount"] * 1000',
    ]
    for pattern in bad_patterns:
        assert pattern not in content, f"仍存在错误模式: {pattern}"
    print("OK engine.py 源码中无 amount * 1000")


if __name__ == '__main__':
    test_download_daily_amount_not_scaled()
    test_download_min5_amount_not_scaled()
    test_no_multiply_1000_in_source()
    print("\n#7 修复验证通过")
