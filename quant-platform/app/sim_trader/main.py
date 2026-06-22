"""
模拟盘交易 — CLI 历史回放入口
逐日回放从 SIM_START 到 SIM_END 的完整交易序列（非实时交易）。
开关 AUTO_SELL/AUTO_BUY 不影响此历史回放模式。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pandas as pd
from datetime import date, timedelta
from collections import defaultdict

from app.sim_trader.config import *
from app.sim_trader.data_loader import (
    load_all_bars, generate_today_signals, load_sh_index,
    is_bull_market, get_daily_snapshot
)
from app.sim_trader.engine import SimTraderEngine
from app.sim_trader.reporter import final_report
from app.sim_trader.store import SimTraderStore


def run():
    print("=" * 64)
    print("  模拟盘交易系统")
    print(f"  区间: {SIM_START} ~ {SIM_END}")
    print(f"  买入: {BUY_TIME}  |  卖出: {SELL_TIME}")
    print("=" * 64)

    # ── 1. 加载数据 ──────────────────────────
    print("\n[1/4] 加载日线数据 ...")
    bars = load_all_bars()
    print(f"  {bars['code'].nunique():,} 只股票, {len(bars):,} 行")

    # ── 2. 预计算信号 ────────────────────────
    print("\n[2/4] 预计算全区间信号 ...")
    from app.screener.engine import load_strategy
    strategy = load_strategy(STRATEGY_NAME)
    try:
        sig = strategy.generate_signals(bars)
    except TypeError:
        sig = strategy.generate_signals(bars, **SIGNAL_PARAMS)
    sig = sig[(sig["date"] >= SIM_START) & (sig["date"] <= SIM_END)].copy()
    sig["date"] = pd.to_datetime(sig["date"]).dt.date
    print(f"  策略: {STRATEGY_NAME}  信号: {len(sig):,}")

    # 按日索引
    sig_by_date = defaultdict(list)
    for _, r in sig.iterrows():
        sig_by_date[r['date']].append((r['code'], float(r['close'])))

    # ── 3. 准备交易日历 ──────────────────────
    bt_bars = bars[(bars["date"] >= SIM_START) & (bars["date"] <= SIM_END)]
    trading_dates = sorted(bt_bars["date"].unique())
    print(f"  交易日: {len(trading_dates):,}")

    # 上证指数
    sh_idx = load_sh_index()

    # ── 4. 逐日模拟 ──────────────────────────
    print(f"\n[3/4] 逐日模拟 ...")
    engine = SimTraderEngine(store=SimTraderStore())

    for i, today in enumerate(trading_dates):
        snapshot = get_daily_snapshot(bt_bars, today)

        # ── 14:52 卖出（先卖，回收现金） ────
        engine.sell_phase(today, snapshot, trading_dates)

        # ── 14:54 买入（用回收后的现金） ────
        paused = engine.pause_until is not None and today <= engine.pause_until
        if today in sig_by_date and not paused:
            max_new = int(engine.cash / engine.max_buy_amount()) + 1
            for code, price in sig_by_date[today][:max_new]:
                # 20天冷却: 检查历史交易
                if any(t.code == code and (today - t.entry_date).days <= SAME_STOCK_COOLDOWN
                       for t in engine.trades):
                    continue
                engine.execute_buy(today, code, price, strategy_name=STRATEGY_NAME)

        # ── 14:56 记录 ──────────────────────
        engine.record(today, snapshot)

        # 进度
        if (i + 1) % 150 == 0:
            eq = engine.total_equity(snapshot)
            print(f"  {today} | {i+1}/{len(trading_dates)} | "
                  f"净值 {eq:,.0f} | 持仓 {engine.position_count} | 现金 {engine.cash:,.0f}")

    # ── 5. 报告 ──────────────────────────────
    print(f"\n[4/4] 生成报告 ...")
    final_report(engine, trading_dates)


if __name__ == "__main__":
    run()
