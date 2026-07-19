# 零差异基线脚本 — TDX 日线回测
# 用法:
#   改前: python scripts/_perf_baseline.py --sig-source dict    --out output/_baseline_before.json
#   改后: python scripts/_perf_baseline.py --sig-source dict    --out output/_baseline_after_legacy.json
#         python scripts/_perf_baseline.py --sig-source parquet --out output/_baseline_after_parquet.json
#   对比: python scripts/_perf_baseline.py --diff A B
import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tqsdk import result_cache
from app.backtest import tdx_runner


def build_params():
    params = {
        "start_date": date(2025, 5, 1),
        "end_date": date(2026, 7, 15),
        "intraday_freq": "daily",
        "strategy_name": "QUANTQQ",
    }
    from app.config.risk_params import load_risk_params, load_position_params, load_streak_params
    _rp, _pp, _sp = load_risk_params(), load_position_params(), load_streak_params()
    params.setdefault("initial_capital", _pp.initial_capital)
    params.setdefault("position_size",    _pp.position_size)
    params.setdefault("min_buy_amt",      _pp.min_buy_amt)
    params.setdefault("hard_stop",        _rp.hard_stop)
    params.setdefault("take_profit_tiers", _rp.take_profit_tiers)
    params.setdefault("trail_activate",   _rp.trail_activate)
    params.setdefault("trail_dd",         _rp.trail_dd)
    params.setdefault("time_exit_days",   _rp.time_exit_days)
    params.setdefault("time_exit_profit", _rp.time_exit_profit)
    params.setdefault("time_force_days",  _rp.time_force_days)
    params.setdefault("loss_streak_pause", _sp.loss_streak_pause)
    params.setdefault("pause_days",       _sp.pause_days)
    params.setdefault("loss_streak_halve", _sp.loss_streak_halve)
    params.setdefault("same_stock_cooldown", _sp.same_stock_cooldown)
    params["position_ratio"] = params["position_size"] / params["initial_capital"]
    return params


def latest_cache():
    cands = sorted((ROOT / "output" / "tdx_cache").glob("*.parquet"),
                   key=lambda p: p.stat().st_mtime)
    return cands[-1]


def run(sig_source: str, out: str, cache: str = None):
    cache = Path(cache) if cache else latest_cache()
    print(f"cache: {cache}")
    params = build_params()
    if sig_source == "dict":
        df = pd.read_parquet(cache)
        signals, prices = result_cache.df_to_signals_prices(df)
        sig_result = {"status": "ok", "signals": signals, "prices": prices}
    else:
        # 新路径: 只给 parquet_path, 不提供 signals/prices
        sig_result = {"status": "ok", "parquet_path": str(cache), "cache_hit": True}

    t0 = time.perf_counter()
    result = tdx_runner._run_daily_backtest(
        sig_result, params, params["start_date"], params["end_date"], None, None, {})
    elapsed = time.perf_counter() - t0

    payload = {
        "summary": result["summary"],
        "trades": sorted(result["trades"], key=lambda t: (t["code"], t["entry_date"], t["exit_date"], t["reason"])),
        "equity": result["equity"],
    }
    Path(out).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] {sig_source} path: {elapsed:.2f}s trades={result['summary']['trades']} "
          f"total_return={result['summary']['total_return']}% -> {out}")


def _eq(a, b, path=""):
    """递归比较, float 容差 1e-6"""
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= 1e-6
        except (TypeError, ValueError):
            return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            print(f"  key 差异 @ {path}: {set(a.keys()) ^ set(b.keys())}")
            return False
        return all(_eq(a[k], b[k], f"{path}.{k}") for k in a)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            print(f"  长度差异 @ {path}: {len(a)} vs {len(b)}")
            return False
        return all(_eq(x, y, f"{path}[{i}]") for i, (x, y) in enumerate(zip(a, b)))
    if a != b:
        print(f"  值差异 @ {path}: {a!r} vs {b!r}")
        return False
    return True


def diff(pa: str, pb: str):
    a = json.loads(Path(pa).read_text(encoding="utf-8"))
    b = json.loads(Path(pb).read_text(encoding="utf-8"))
    ok = True
    for section in ("summary", "trades", "equity"):
        # data_source 字段允许不同(路径标记), 比对时剔除
        if section == "summary":
            a[section].pop("data_source", None)
            b[section].pop("data_source", None)
        same = _eq(a[section], b[section], section)
        print(f"  {section}: {'一致' if same else '*** 有差异 ***'}")
        ok = ok and same
    print("零差异验证:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sig-source", choices=["dict", "parquet"])
    ap.add_argument("--out")
    ap.add_argument("--cache", help="指定缓存 parquet(默认取最新)")
    ap.add_argument("--diff", nargs=2, metavar=("A", "B"))
    args = ap.parse_args()
    if args.diff:
        diff(args.diff[0], args.diff[1])
    else:
        run(args.sig_source, args.out, args.cache)
