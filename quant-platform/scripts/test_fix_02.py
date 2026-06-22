# -*- coding: utf-8 -*-
"""验证 #2 修复: db.update_stock_list 不再抛 AttributeError"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch
import pandas as pd


def test_update_stock_list_method_exists():
    """update_stock_list 方法必须存在"""
    from database.duckdb_manager import db
    assert hasattr(db, 'update_stock_list'), "db.update_stock_list 不存在"
    assert callable(db.update_stock_list), "update_stock_list 不可调用"
    print("OK update_stock_list 方法存在")


def test_update_stock_list_calls_upsert_stocks():
    """update_stock_list 必须委托给 upsert_stocks"""
    from database.duckdb_manager import db
    with patch.object(db, 'upsert_stocks', return_value=None) as mock_upsert:
        df = pd.DataFrame([{"code": "000001", "name": "测试"}])
        db.update_stock_list(df)
        assert mock_upsert.called, "update_stock_list 未调用 upsert_stocks"
        assert mock_upsert.call_count == 1
        print("OK update_stock_list 正确委托给 upsert_stocks")


def test_engine_no_bare_except():
    """engine.py:71 必须不是裸 except"""
    engine_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "data_manager", "engine.py"
    )
    with open(engine_path, 'r', encoding='utf-8') as f:
        content = f.read()
    lines = content.split('\n')
    found_bare = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == 'except: pass' or stripped == 'except:pass':
            found_bare = True
            print(f"FAIL 第 {i+1} 行发现裸 except: {line!r}")
    assert not found_bare, "engine.py 中仍存在裸 except: pass"
    print("OK engine.py 无裸 except: pass")


if __name__ == '__main__':
    test_update_stock_list_method_exists()
    test_update_stock_list_calls_upsert_stocks()
    test_engine_no_bare_except()
    print("\nALL PASS #2 修复验证通过")