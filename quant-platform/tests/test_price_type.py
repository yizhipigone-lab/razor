"""price_type 市价映射单元测试(2026-07-18 手工下单 M1/T1)

运行:pytest tests/test_price_type.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.live_trader.price_type import (
    FIX_PRICE, LATEST_PRICE, MARKET_PEER_PRICE_FIRST, MARKET_MINE_PRICE_FIRST,
    MARKET_SH_CONVERT_5_CANCEL, MARKET_SH_CONVERT_5_LIMIT, MARKET_SZ_CONVERT_5_CANCEL,
    available_keys, detect_market, key_name, map_price_type, needs_price,
)


def test_detect_market():
    assert detect_market("600000") == "SH"
    assert detect_market("600000.SH") == "SH"
    assert detect_market("688981") == "SH"      # 科创板
    assert detect_market("000001") == "SZ"
    assert detect_market("300750") == "SZ"      # 创业板
    assert detect_market("830799") == "BJ"      # 北交所 8 开头
    assert detect_market("430047") == "BJ"      # 北交所 4 开头
    assert detect_market("920001") == "BJ"      # 北交所 920 开头
    assert detect_market("159226") == "SZ"      # ETF 深
    assert detect_market("510300") == "SH"      # ETF 沪


def test_all_keys_map():
    """7 种键全映射到正确常量"""
    assert map_price_type("limit", "600000") == (FIX_PRICE, None)
    assert map_price_type("latest", "600000") == (LATEST_PRICE, None)
    assert map_price_type("peer_best", "600000") == (MARKET_PEER_PRICE_FIRST, None)
    assert map_price_type("mine_best", "600000") == (MARKET_MINE_PRICE_FIRST, None)
    assert map_price_type("sh5_cancel", "600000") == (MARKET_SH_CONVERT_5_CANCEL, None)
    assert map_price_type("sh5_limit", "600000") == (MARKET_SH_CONVERT_5_LIMIT, None)
    assert map_price_type("sz5_cancel", "000001") == (MARKET_SZ_CONVERT_5_CANCEL, None)


def test_cross_market_degrades_with_warning():
    """跨市场五档 → 降级对手最优 + warning"""
    # 沪五档用于深市
    const, warn = map_price_type("sh5_cancel", "000001")
    assert const == MARKET_PEER_PRICE_FIRST
    assert warn and "降级" in warn
    # 深五档用于沪市
    const, warn = map_price_type("sz5_cancel", "600000")
    assert const == MARKET_PEER_PRICE_FIRST
    assert warn and "降级" in warn
    # 沪五档转限价用于深市(深交所不支持五档转限价)
    const, warn = map_price_type("sh5_limit", "300750")
    assert const == MARKET_PEER_PRICE_FIRST
    assert warn and "降级" in warn


def test_beijing_five_level_degrades():
    """北交所不支持五档 → 降级对手最优(2026-07-18 MQ 调研坑点)"""
    for key in ("sh5_cancel", "sh5_limit", "sz5_cancel"):
        const, warn = map_price_type(key, "830799")
        assert const == MARKET_PEER_PRICE_FIRST, f"{key} 北交所应降级"
        assert warn and "降级" in warn
    # 北交所全市场类不受影响
    assert map_price_type("peer_best", "830799") == (MARKET_PEER_PRICE_FIRST, None)
    assert map_price_type("limit", "830799") == (FIX_PRICE, None)


def test_unknown_key_raises():
    import pytest
    with pytest.raises(ValueError):
        map_price_type("not_a_key", "600000")
    with pytest.raises(ValueError):
        needs_price("not_a_key")
    with pytest.raises(ValueError):
        key_name("not_a_key")


def test_needs_price_only_limit():
    assert needs_price("limit") is True
    for k in ("latest", "peer_best", "mine_best", "sh5_cancel", "sh5_limit", "sz5_cancel"):
        assert needs_price(k) is False


def test_available_keys_filter():
    """前端下拉过滤(R4):限定市场类只在对应市场出现"""
    sh_keys = available_keys("600000")
    assert "sh5_cancel" in sh_keys and "sh5_limit" in sh_keys
    assert "sz5_cancel" not in sh_keys
    sz_keys = available_keys("000001")
    assert "sz5_cancel" in sz_keys
    assert "sh5_cancel" not in sz_keys and "sh5_limit" not in sz_keys
    bj_keys = available_keys("830799")
    assert bj_keys == ["limit", "latest", "peer_best", "mine_best"]
