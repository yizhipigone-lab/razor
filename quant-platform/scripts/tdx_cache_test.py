"""TDX 缓存互转一致性验证（不需通达信）
验证 signals/prices dict → parquet → dict 往返后与原始数据一致。
运行: python scripts/tdx_cache_test.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tqsdk import result_cache


def build_mock():
    """构造 mock 数据：含 OHLC 完整、OHLC 缺失、纯信号无价格 三种 code"""
    signals = {
        "000001": {"Date": ["20260101", "20260102", "20260103"], "ZP": ["0", "1", "0"]},
        "000002": {"Date": ["20260101", "20260102", "20260103"], "ZP": ["100", "0", "0.5"]},
        "000003": {"Date": ["20260101", "20260102"], "ZP": ["0", "0"]},  # 无信号
        "688001": {"Date": ["20260101", "20260102"], "ZP": ["0", "1"]},
    }
    prices = {
        "000001": {  # OHLC 完整
            "Date": ["20260101", "20260102", "20260103"],
            "Close": ["10.5", "10.6", "10.4"],
            "High": ["10.8", "10.9", "10.5"],
            "Low": ["10.3", "10.4", "10.2"],
            "Open": ["10.4", "10.5", "10.3"],
        },
        "000002": {  # OHLC 缺失（模拟老 TDX 只返回 Close）
            "Date": ["20260101", "20260102"],
            "Close": ["20.1", "20.2"],
        },
        "688001": {
            "Date": ["20260101", "20260102"],
            "Close": ["50.0", "51.0"],
            "High": ["50.5", "51.5"],
            "Low": ["49.5", "50.5"],
            "Open": ["50.0", "51.0"],
        },
    }
    return signals, prices


def assert_eq(label, got, expect):
    if got == expect:
        print(f"  [OK] {label}")
        return True
    else:
        print(f"  [FAIL] {label}")
        print(f"    expect: {expect}")
        print(f"    got: {got}")
        return False


def main():
    signals, prices = build_mock()

    # 清空旧缓存
    result_cache.clear_cache()

    # 写缓存
    path = result_cache.save_cache_from_dict(
        signals, prices,
        formula_name="TESTFORMULA", start_time="20260101", end_time="20260103",
        kline_count=100, return_count=100, stock_list_override=None)
    print(f"缓存写入: {path}")

    # 命中检查
    hit = result_cache.get_cache(
        "TESTFORMULA", "20260101", "20260103", 100, 100, None)
    assert_eq("缓存命中", hit is not None, True)

    # 读回
    import pandas as pd
    df = pd.read_parquet(path)
    print(f"读回 DataFrame: {len(df)} 行, 列={list(df.columns)}")
    print(df.to_string(index=False))

    sig2, pri2 = result_cache.df_to_signals_prices(df)

    ok = True
    # signals 一致性
    print("\n[signals 一致性]")
    ok &= assert_eq("signals code 集", set(sig2.keys()), set(signals.keys()))
    for code in signals:
        ok &= assert_eq(f"{code} Date", sig2[code]["Date"], signals[code]["Date"])
        var = next(k for k in signals[code] if k != "Date")
        ok &= assert_eq(f"{code} {var}", sig2[code].get(var), signals[code][var])

    # prices 一致性
    print("\n[prices 一致性]")
    ok &= assert_eq("prices code 集", set(pri2.keys()), set(prices.keys()))
    for code in prices:
        for field in ["Date", "Close", "High", "Low", "Open"]:
            expect = prices[code].get(field)
            got = pri2[code].get(field)
            if expect is None:
                # 原始缺该字段 → 重组装时应有（NaN→"0"）
                ok &= assert_eq(f"{code} {field} (原缺失→补'0')",
                                got is not None and all(v == "0" for v in got), True)
            else:
                ok &= assert_eq(f"{code} {field}", got, expect)

    # df_to_signals（API 用）一致性
    print("\n[df_to_signals 一致性]")
    sig_only = result_cache.df_to_signals(df)
    ok &= assert_eq("df_to_signals code 集", set(sig_only.keys()), set(signals.keys()))

    print("\n" + ("=" * 50))
    print("[ALL PASS]" if ok else "[FAIL]")
    print("=" * 50)

    # 清理
    result_cache.clear_cache()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
