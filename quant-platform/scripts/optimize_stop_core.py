#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
止盈止损参数优化 — 模拟引擎
独立模块，不导入 db，供多进程 Worker 使用
"""
import numpy as np

# 由主进程通过 init_worker 设置
CODE_PRICE_ARRAYS = None
CODE_DATE_POS = None
SIGNAL_TUPLES = None


def init_worker(price_arrays, date_pos, signal_tuples):
    global CODE_PRICE_ARRAYS, CODE_DATE_POS, SIGNAL_TUPLES
    CODE_PRICE_ARRAYS = price_arrays
    CODE_DATE_POS = date_pos
    SIGNAL_TUPLES = signal_tuples


def simulate_single(params):
    """
    模拟一组参数下的所有交易
    params: (name, trailing_stop, dd_activate, dd_drawdown, tiers, max_hold, stop_loss)

    止盈机制优先级：
    1. 硬止损 (stop_loss): 任何时候跌破此 % 即卖
    2. 移动止盈 (trailing_stop): 最高点回落此 % 即卖
    3. 回撤止盈 (dd): 盈利到 activate% 后，回撤 drawdown% 即卖
    4. 分档止盈 (tiers): 每到一个目标 %，卖指定比例
    5. 最长持有 (max_hold): 到期全卖
    """
    name, trailing_stop, dd_activate, dd_drawdown, tiers, max_hold, stop_loss = params

    # 解析分档止盈
    tier_profits = None
    tier_pcts = None
    if tiers:
        tier_profits = np.array([t[0] for t in tiers], dtype=np.float64)
        tier_pcts = np.array([t[1] for t in tiers], dtype=np.float64)
        tier_pcts = tier_pcts / tier_pcts.sum()

    all_returns = []

    for code, entry, pos in SIGNAL_TUPLES:
        prices = CODE_PRICE_ARRAYS.get(code)
        if prices is None or pos + 1 >= len(prices):
            continue

        highest = entry
        pos_size = 1.0
        trade_rets = []  # (return, weight) for partial sells
        drawdown_active = False
        tier_used = set()
        max_i = min(pos + 1 + max_hold * 2, len(prices))

        for i in range(pos + 1, max_i):
            price = prices[i]
            pnl = (price / entry - 1) * 100

            if price > highest:
                highest = price
            highest_pnl = (highest / entry - 1) * 100

            # 1. 硬止损
            if stop_loss is not None and pnl <= stop_loss:
                ret = (price / entry - 1) * 100
                trade_rets.append((ret, pos_size))
                pos_size = 0
                break

            # 2. 移动止盈
            if trailing_stop is not None and highest > entry:
                trail_price = highest * (1 + trailing_stop / 100)
                if price <= trail_price:
                    ret = (price / entry - 1) * 100
                    trade_rets.append((ret, pos_size))
                    pos_size = 0
                    break

            # 3. 回撤止盈
            if dd_activate is not None and dd_drawdown is not None:
                if pnl >= dd_activate:
                    drawdown_active = True
                if drawdown_active and (highest_pnl - pnl) >= dd_drawdown:
                    ret = (price / entry - 1) * 100
                    trade_rets.append((ret, pos_size))
                    pos_size = 0
                    break

            # 4. 分档止盈
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

            # 5. 最长持有
            if (i - pos) >= max_hold and pos_size > 0:
                ret = (price / entry - 1) * 100
                trade_rets.append((ret, pos_size))
                pos_size = 0
                break

        if pos_size > 0:
            continue

        total_ret = sum(r * w for r, w in trade_rets)
        all_returns.append(total_ret)

    if not all_returns:
        return None

    arr = np.array(all_returns, dtype=np.float64)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]

    n_trades = len(arr)
    win_rate = len(wins) / n_trades * 100
    avg_ret = float(arr.mean())
    med_ret = float(np.median(arr))
    total_ret = float(arr.sum())
    gain_sum = float(wins.sum()) if len(wins) > 0 else 0
    loss_sum = float(losses.sum()) if len(losses) > 0 else 0
    pf = abs(gain_sum / loss_sum) if loss_sum != 0 else float('inf')

    return {
        "name": name,
        "trailing_stop": trailing_stop,
        "dd_activate": dd_activate,
        "dd_drawdown": dd_drawdown,
        "tiers": str(tiers),
        "max_hold": max_hold,
        "stop_loss": stop_loss,
        "trades": n_trades,
        "win_rate": round(win_rate, 2),
        "avg_return": round(avg_ret, 4),
        "med_return": round(med_ret, 4),
        "total_return": round(total_ret, 2),
        "profit_factor": round(pf, 4),
    }
