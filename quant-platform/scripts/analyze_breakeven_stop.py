#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
组合8 (RPS>80) — 移动止损面回测
概念：硬止损的"止损面"随着盈利上升而向上移动。
每一级盈利阈值触发后，止损面上移至新的位置。
"""
import sys, os
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, timedelta
import multiprocessing
import time
import numpy as np


def main():
    from database.duckdb_manager import db
    from app.screener.strategies.ma5_angle import generate_signals
    import pandas as pd

    END   = date(2026, 4, 29)
    START = date(2025, 1, 1)
    LOAD_START = START - timedelta(days=365)
    N_CPUS = min(multiprocessing.cpu_count(), 16)

    print("加载 K 线数据 ...")
    bars = db.load_all_bars(freq="daily", start=LOAD_START, end=END)
    bars = bars.to_pandas() if hasattr(bars, "to_pandas") else pd.DataFrame(bars)
    for c in ["open","high","low","close","volume"]:
        if c in bars: bars[c] = pd.to_numeric(bars[c], errors='coerce')
    bars = bars.dropna(subset=["close"])
    bars["date"] = pd.to_datetime(bars["date"]).dt.date
    bars = bars.sort_values(["code","date"])

    print("计算 RPS ...")
    rps_values = {}
    for code in bars["code"].unique():
        cb = bars[bars["code"] == code].sort_values("date")
        if len(cb) < 120: continue
        rps_values[code] = cb.iloc[-1]["close"] / cb.iloc[-120]["close"] - 1
    rps_series = pd.Series(rps_values)
    rps_rank = rps_series.rank(pct=True) * 100
    rps_high_set = set(rps_rank[rps_rank > 80].index.tolist())

    print("生成信号 ...")
    sig = generate_signals(bars, version="original")
    sig = sig[(sig["date"] >= START) & (sig["date"] <= END)].copy()
    sig["date"] = pd.to_datetime(sig["date"]).dt.date
    sig["buy_price"] = sig["close"].astype(float)
    sig = sig[sig["code"].isin(rps_high_set)].copy()
    print(f"  组合8 信号数: {len(sig)}\n")

    print("构建数据索引 ...")
    code_price_arrays = {}
    code_date_pos = {}
    for code in bars["code"].unique():
        cb = bars[bars["code"] == code].sort_values("date")
        code_price_arrays[code] = cb["close"].values.astype(np.float64)
        code_date_pos[code] = {d: i for i, d in enumerate(cb["date"].values)}

    signal_tuples = []
    for _, row in sig.iterrows():
        d = row["date"]
        pos = code_date_pos.get(row["code"], {}).get(d)
        if pos is not None:
            signal_tuples.append((row["code"], float(row["buy_price"]), pos))
    print(f"  预建 {len(signal_tuples)} 个信号位置\n")

    # ── 多级移动止损模拟器 ────────────────────────────────
    def simulate_breakeven(config):
        """
        config: (name, initial_stop, breakeven_levels, trailing_stop, dd_activate, dd_drawdown, tiers, max_hold)
        breakeven_levels: [(profit_threshold, new_stop_pct), ...]
        每次盈利达到 threshold，就将 stop_pct 更新到新值
        """
        name, initial_stop, be_levels, ts, dd_act, dd_dd, tiers, max_hold = config

        tier_profits = None
        tier_pcts = None
        if tiers:
            tier_profits = np.array([t[0] for t in tiers], dtype=np.float64)
            tier_pcts = np.array([t[1] for t in tiers], dtype=np.float64)
            tier_pcts = tier_pcts / tier_pcts.sum()

        # 解析 breakeven 级别
        be_thresholds = None
        be_stops = None
        if be_levels:
            be_thresholds = np.array([l[0] for l in be_levels], dtype=np.float64)
            be_stops = np.array([l[1] for l in be_levels], dtype=np.float64)

        all_returns = []
        for code, entry, pos in signal_tuples:
            prices = code_price_arrays.get(code)
            if prices is None or pos + 1 >= len(prices):
                continue

            highest = entry
            current_stop = initial_stop
            pos_size = 1.0
            trade_rets = []
            drawdown_active = False
            tier_used = set()
            max_i = min(pos + 1 + max_hold * 2, len(prices))
            be_idx = 0  # 当前 breakeven 级别索引

            for i in range(pos + 1, max_i):
                price = prices[i]
                pnl = (price / entry - 1) * 100

                if price > highest:
                    highest = price
                highest_pnl = (highest / entry - 1) * 100

                # ① 移动止损面：检查是否需要升级止损级别
                if be_thresholds is not None and be_idx < len(be_thresholds):
                    if pnl >= be_thresholds[be_idx]:
                        current_stop = be_stops[be_idx]
                        be_idx += 1

                # ② 硬止损（当前止损面）
                if current_stop is not None and pnl <= current_stop:
                    ret = (price / entry - 1) * 100
                    trade_rets.append((ret, pos_size))
                    pos_size = 0
                    break

                # ③ 移动止盈
                if ts is not None and highest > entry:
                    trail_price = highest * (1 + ts / 100)
                    if price <= trail_price:
                        ret = (price / entry - 1) * 100
                        trade_rets.append((ret, pos_size))
                        pos_size = 0
                        break

                # ④ 回撤止盈
                if dd_act is not None and dd_dd is not None:
                    if pnl >= dd_act:
                        drawdown_active = True
                    if drawdown_active and (highest_pnl - pnl) >= dd_dd:
                        ret = (price / entry - 1) * 100
                        trade_rets.append((ret, pos_size))
                        pos_size = 0
                        break

                # ⑤ 分档止盈
                if tier_profits is not None:
                    for ti in range(len(tier_profits)):
                        if ti not in tier_used and pnl >= tier_profits[ti]:
                            sell_pct = tier_pcts[ti]
                            ret = (price / entry - 1) * 100
                            trade_rets.append((ret, sell_pct))
                            pos_size -= sell_pct
                            tier_used.add(ti)
                            if pos_size <= 0:
                                break
                    if pos_size <= 0:
                        break

                # ⑥ 最长持有
                if (i - pos) >= max_hold and pos_size > 0:
                    ret = (price / entry - 1) * 100
                    trade_rets.append((ret, pos_size))
                    pos_size = 0
                    break

            if pos_size > 0:
                continue
            all_returns.append(sum(r * w for r, w in trade_rets))

        if not all_returns:
            return None

        arr = np.array(all_returns, dtype=np.float64)
        wins = arr[arr > 0]
        losses = arr[arr <= 0]
        n = len(arr)
        return {
            "name": name,
            "trades": n,
            "win_rate": round(len(wins) / n * 100, 2),
            "avg_return": round(float(arr.mean()), 4),
            "med_return": round(float(np.median(arr)), 4),
            "total_return": round(float(arr.sum()), 2),
            "profit_factor": round(abs(wins.sum() / losses.sum()), 4) if losses.sum() != 0 else float('inf'),
        }

    # ── 测试配置 ──────────────────────────────────────────
    # 格式: (name, initial_stop, be_levels, trailing_stop, dd_activate, dd_drawdown, tiers, max_hold)
    configs = []

    # A. 基线：固定硬止损
    configs.append(("固定硬止损-7% hold=20d", -7.0, None, None, None, None, None, 20))
    configs.append(("固定硬止损-7% hold=30d", -7.0, None, None, None, None, None, 30))

    # B. 移动止损面 — 不同梯级设计
    be_designs = [
        ("一级: +5%→0%",    [(5, 0)]),
        ("一级: +5%→-2%",   [(5, -2)]),
        ("一级: +8%→0%",    [(8, 0)]),
        ("二级: 5%→0%, 10%→+3%",  [(5, 0), (10, 3)]),
        ("二级: 5%→-2%, 10%→+3%", [(5, -2), (10, 3)]),
        ("二级: 5%→-2%, 15%→+5%", [(5, -2), (15, 5)]),
        ("三级: 5%→-2%, 10%→+3%, 15%→+8%", [(5, -2), (10, 3), (15, 8)]),
        ("三级: 3%→0%, 7%→+3%, 12%→+7%",   [(3, 0), (7, 3), (12, 7)]),
        ("四级: 3%→-2%, 7%→+2%, 12%→+7%, 20%→+15%", [(3, -2), (7, 2), (12, 7), (20, 15)]),
    ]

    for desc, levels in be_designs:
        for h in [20, 30]:
            name = f"移动止损_{desc}_hold={h}d"
            configs.append((name, -7.0, levels, None, None, None, None, h))

    # C. 移动止损面 + 回撤止盈（最佳组合）
    configs.append(("移动止损二级(5→-2,10→+3)+回撤15/5 hold=20", -7.0, [(5, -2), (10, 3)], None, 15, 5, None, 20))
    configs.append(("移动止损三级(5→-2,10→+3,15→+8)+回撤15/5 hold=20", -7.0, [(5, -2), (10, 3), (15, 8)], None, 15, 5, None, 20))

    # D. 移动止损面 + 分档止盈
    configs.append(("移动止损二级(5→-2,10→+3)+分档10/20/30 hold=30", -7.0, [(5, -2), (10, 3)], None, None, None, [(10, 33), (20, 33), (30, 34)], 30))

    # E. 不同初始止损的移动面
    for init_stop in [-5, -7, -10]:
        configs.append((f"初始止损{init_stop}%+移动三级_hold=20d", init_stop, [(5, -2), (10, 3), (15, 8)], None, None, None, None, 20))

    # F. hold=30d 基线对比
    configs.append(("基线_hold=30d", None, None, None, None, None, None, 30))

    N = len(configs)
    print(f"共 {N} 组配置\n")

    # ── 顺序执行（配置少，不必多进程）─────────────────────
    t0 = time.time()
    results = []
    for i, c in enumerate(configs):
        if (i + 1) % 10 == 0:
            print(f"  进度: {i+1}/{N}")
        r = simulate_breakeven(c)
        if r:
            results.append(r)
    print(f"耗时 {time.time()-t0:.0f}s\n")

    # ── 按 PF 排名 ──────────────────────────────────────
    sorted_r = sorted(results, key=lambda x: x["profit_factor"], reverse=True)

    print(f"{'='*120}")
    print(f"{'移动止损面回测 — 综合排名':^120}")
    print(f"{'='*120}")
    print(f"{'#':<3} {'策略':<55} {'交易':>6} {'胜率':>7} {'avg%':>8} {'med%':>8} {'PF':>8} {'总收益':>11}")
    print(f"{'─'*120}")
    for i, r in enumerate(sorted_r):
        print(f"{i+1:<3} {r['name']:<55} {r['trades']:>6} {r['win_rate']:>6.1f}% "
              f"{r['avg_return']:>+7.2f}% {r['med_return']:>+7.2f}% "
              f"{r['profit_factor']:>7.2f} {r['total_return']:>+10.0f}%")
    print("=" * 120)

    # ── 关键对比 ──────────────────────────────────────────
    print(f"\n{'='*60}")
    print("关键对比：固定止损 vs 各级移动止损 (hold=20d)")
    print(f"{'='*60}")
    for r in sorted_r:
        if "hold=20d" in r["name"]:
            print(f"  {r['name']:<55} PF={r['profit_factor']:.2f}  "
                  f"胜率={r['win_rate']:.1f}%  avg={r['avg_return']:.2f}%")

    print(f"\n{'='*60}")
    print("关键对比：固定止损 vs 各级移动止损 (hold=30d)")
    print(f"{'='*60}")
    for r in sorted_r:
        if "hold=30d" in r["name"]:
            print(f"  {r['name']:<55} PF={r['profit_factor']:.2f}  "
                  f"胜率={r['win_rate']:.1f}%  avg={r['avg_return']:.2f}%")


if __name__ == '__main__':
    main()
