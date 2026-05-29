"""
TDX 公式回测引擎
从通达信获取信号 + 价格，逐日回放买卖，输出格式严格匹配 simple_runner
"""

import numpy as np
import pandas as pd
from datetime import date, timedelta
from collections import defaultdict, Counter
from typing import Callable, Optional

from core.logger import get_logger
from app.backtest.simple_runner import FastEngine, Position, Trade, load_index_data

log = get_logger("TdxBT")

def run_tdx_backtest(params: dict, progress_cb: Optional[Callable] = None,
                     stop_event=None, stock_names: Optional[dict] = None) -> dict:
    """TDX 策略回测入口：优先 5 分钟线引擎，失败降级日线"""
    from app.tqsdk.bridge import TdxBridge

    start = params.get("start_date", date(2023, 1, 1))
    end = params.get("end_date", date.today())
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)

    natural_days = (end - start).days
    est_trade_days = int(natural_days * 0.7)
    lookback = 80
    kline_count = max(100, est_trade_days + lookback)
    end_time = params.get("end_time") or end.strftime("%Y%m%d")
    start_time_str = start.strftime("%Y%m%d")
    # 公式需要足够历史K线，start_time 向前推1年
    from datetime import timedelta
    formula_start = (start - timedelta(days=365)).strftime("%Y%m%d")
    log.info(f"TDX回测: {start}~{end} ({natural_days}d) kline_count={kline_count} formula_start={formula_start}")

    # 默认参数 — 全部从 config.py 读取，不硬编码任何数字
    from app.sim_trader.config import (
        INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
        HARD_STOP, TAKE_PROFIT_TIERS, TRAIL_ACTIVATE, TRAIL_DD,
        TIME_EXIT_DAYS, TIME_EXIT_PROFIT, TIME_FORCE_DAYS,
        LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE, PAUSE_DAYS, SAME_STOCK_COOLDOWN,
    )
    params.setdefault("initial_capital", INITIAL_CAPITAL)
    params.setdefault("position_size", POSITION_SIZE)
    params.setdefault("min_buy_amt", MIN_BUY_AMT)
    params.setdefault("hard_stop", HARD_STOP)
    params.setdefault("take_profit_tiers", TAKE_PROFIT_TIERS)
    params.setdefault("trail_activate", TRAIL_ACTIVATE)
    params.setdefault("trail_dd", TRAIL_DD)
    params.setdefault("time_exit_days", TIME_EXIT_DAYS)
    params.setdefault("time_exit_profit", TIME_EXIT_PROFIT)
    params.setdefault("time_force_days", TIME_FORCE_DAYS)
    params.setdefault("loss_streak_pause", LOSS_STREAK_PAUSE)
    params.setdefault("pause_days", PAUSE_DAYS)
    params.setdefault("loss_streak_halve", LOSS_STREAK_HALVE)
    params.setdefault("same_stock_cooldown", SAME_STOCK_COOLDOWN)

    # 动态仓位：position_size 转为净值的固定比例（默认20%）
    params["position_ratio"] = params.get("position_size", POSITION_SIZE) / params["initial_capital"]

    bridge = TdxBridge()
    if stop_event and stop_event.is_set():
        return {"status": "stopped"}

    # ── 日线收盘价回测 ────────────────────────────
    if progress_cb:
        progress_cb(0, 4, f"日线回测 (QUANTQQ, {kline_count}条K线)...")

    sig_result = bridge.execute_screen_range(
        end_time=end_time,
        kline_count=kline_count,
        start_time=formula_start,
    )
    if sig_result.get("status") != "ok":
        return {"status": "error", "message": sig_result.get("message", "TDX 信号获取失败")}

    return _run_daily_backtest(
        sig_result, params, start, end, progress_cb,
        stop_event, stock_names or {},
    )


def _run_5m_backtest(sig_result: dict, params: dict, start: date, end: date,
                      progress_cb, stop_event, stock_names: dict) -> dict | None:
    """5 分钟线回测引擎：逐 K 线检查止盈止损"""
    try:
        raw_signals = sig_result.get("signals", {})
        bars_5m = sig_result.get("bars_5m", [])

        # 解析信号
        sig_by_code = {}
        all_signal_codes = set()
        for code, d in raw_signals.items():
            code_num = code.split(".")[0] if "." in code else code
            dates_list = d.get("Date", [])
            zps = d.get("ZP", [])
            if len(dates_list) != len(zps):
                continue
            code_sigs = {}
            has_any = False
            for dt, zp in zip(dates_list, zps):
                try:
                    dt_date = date(int(dt[:4]), int(dt[4:6]), int(dt[6:8]))
                except (ValueError, TypeError):
                    continue
                if start <= dt_date <= end:
                    code_sigs[str(dt_date)] = zp
                    if zp == "1":
                        has_any = True
            if has_any:
                sig_by_code[code_num] = code_sigs
                all_signal_codes.add(code_num)

        if not all_signal_codes:
            return _empty_result(params, 0, "区间内无QUANTQQ信号")

        if progress_cb:
            progress_cb(1, 4, f"5分钟逐K线回放 ({len(bars_5m)}根K线, {len(all_signal_codes)}只信号股)...")

        if stop_event and stop_event.is_set():
            return {"status": "stopped"}

        # 按时间排序
        from datetime import datetime as dt_cls
        bars_5m.sort(key=lambda x: x.get("datetime", ""))
        # 为每个 bar 补齐 date 字段
        for b in bars_5m:
            dt_str = b.get("datetime", "")
            if len(dt_str) >= 10:
                b["date"] = date.fromisoformat(dt_str[:10])
            b.setdefault("high", b.get("close", 0))
            b.setdefault("low", b.get("close", 0))
            b.setdefault("open", b.get("close", 0))

        # ── 引擎状态 ─────────────────────────────────
        cash = params["initial_capital"]
        position_ratio = params["position_ratio"]
        positions = {}          # code -> Position
        trades_5m = []          # list of Trade
        equity_curve = []       # list of {datetime, equity, cash, pos}
        cooldown = {}           # code -> last_exit_date
        pending_buys = defaultdict(list)  # date_str -> [code, ...]
        sell_reasons = Counter()
        total_buy_signals = 0
        prev_day = None

        for code in sorted(sig_by_code.keys()):
            for dt_str, zp in sig_by_code[code].items():
                if zp == "1":
                    pending_buys[dt_str].append(code)

        # ── 逐 K 线循环 ───────────────────────────────
        for i, bar in enumerate(bars_5m):
            if stop_event and stop_event.is_set():
                return {"status": "stopped"}

            d = bar["date"]
            d_str = str(d)

            # 新的一天：执行买入信号
            if prev_day != d_str:
                prev_day = d_str
                if d_str in pending_buys:
                    for code in pending_buys[d_str]:
                        if code in positions:
                            continue
                        if code in cooldown and (d - cooldown[code]).days < params.get("same_stock_cooldown", 20):
                            continue
                        # 动态仓位：按当前市价净值 × 比例
                        mkt_value = 0
                        for pc, pp in positions.items():
                            if not pp.active: continue
                            bar_px = next((b["close"] for b in bars_5m[max(0,i-50):i+1]
                                          if b["code"] == pc and b["date"] == d), pp.entry_price)
                            mkt_value += pp.shares * bar_px
                        current_equity = cash + mkt_value
                        dyn_size = current_equity * position_ratio
                        if cash < dyn_size * 0.5:
                            break
                        bar_for_code = next(
                            (b for b in bars_5m[i:] if b["code"] == code and b["date"] == d), None
                        )
                        if bar_for_code is None:
                            continue
                        px = bar_for_code["close"]
                        if px <= 0:
                            continue
                        sh = int(dyn_size / px / 100) * 100
                        if sh < 100:
                            continue
                        cost = sh * px
                        if cost > cash:
                            continue
                        cash -= cost
                        positions[code] = Position(code, d, px, sh, cost)
                        total_buy_signals += 1

            # 检查止盈止损（按该 bar 的股票代码查找持仓）
            code = bar["code"]
            pos = positions.get(code)
            if pos and pos.active:
                h = bar["high"]
                l = bar["low"]
                c = bar["close"]

                if h > pos.peak_price:
                    pos.peak_price = h

                entry = pos.entry_price
                hold_days = (d - pos.entry_date).days
                reason = None
                sell_px = None

                # 1. 硬止损（Low 跌破止损线）
                hs_pct = params["hard_stop"]
                stop_price = entry * (1 + hs_pct)
                if l <= stop_price:
                    sell_px = max(stop_price, bar["open"])
                    reason = "HS"
                # 2. 阶梯止盈（High 突破止盈线，按 sell_ratio 部分卖出）
                elif reason is None:
                    tp_tiers = params.get("take_profit_tiers", [])
                    for idx, tier in enumerate(tp_tiers):
                        if idx not in pos.tp_triggered and h >= entry * (1 + tier["profit_pct"]):
                            pos.tp_triggered.add(idx)
                            sell_px = entry * (1 + tier["profit_pct"])
                            reason = f"TP{idx + 1}"
                            # 部分卖出：按 sell_ratio 计算股数
                            ss = int(pos.shares * tier.get("sell_ratio", 1.0) / 100) * 100
                            if ss < 100:
                                ss = 100
                            partial_sell = min(ss, int(pos.shares))
                            break
                # 3. 移动止盈（Low 跌破回撤线）
                if reason is None:
                    trail_act = params["trail_activate"]
                    trail_dd = params["trail_dd"]
                    if pos.peak_price / entry - 1 >= trail_act:
                        trail_price = pos.peak_price * (1 - trail_dd)
                        if l <= trail_price:
                            sell_px = max(trail_price, bar["open"])
                            reason = "TR"
                # 4. 时间退出
                if reason is None:
                    time_exit = params["time_exit_days"]
                    time_profit = params["time_exit_profit"]
                    if hold_days > time_exit and c / entry - 1 > time_profit:
                        sell_px = c
                        reason = "TC"
                # 5. 强制时间退出
                if reason is None:
                    time_force = params["time_force_days"]
                    if hold_days > time_force:
                        sell_px = c
                        reason = "TF"

                if reason and sell_px and sell_px > 0:
                    # 支持部分卖出（TP 阶梯止盈），其他原因全卖
                    sell_shares = partial_sell if (reason and reason.startswith("TP")) else pos.shares
                    sell_shares = min(sell_shares, pos.shares)
                    if sell_shares <= 0:
                        sell_shares = pos.shares
                    ret = (sell_px / entry - 1) * 100
                    profit = sell_shares * (sell_px - entry)
                    cash += sell_shares * sell_px
                    if sell_shares >= pos.shares:
                        pos.active = False
                    else:
                        pos.shares -= sell_shares
                    trades_5m.append(Trade(
                        code, pos.entry_date, d, entry, sell_px,
                        sell_shares, round(ret, 2), round(profit, 0), reason,
                        hold_days,
                    ))
                    sell_reasons[reason] += 1
                    cooldown[code] = d

            # 清理已平仓
            positions = {k: v for k, v in positions.items() if v.active}

            # 记录净值（每 50 根记一次，减少内存）
            if i % 50 == 0:
                pos_value = sum(
                    p.shares * next(
                        (b["close"] for b in bars_5m[max(0, i - 50):i + 1]
                         if b["code"] == pc and b["date"] == d),
                        p.entry_price,
                    )
                    for pc, p in positions.items()
                )
                equity_curve.append({
                    "date": d_str, "equity": round(cash + pos_value, 2),
                    "cash": round(cash, 2), "pos": len(positions),
                })

        # ── 最终清仓 ──────────────────────────────────
        for code, p in list(positions.items()):
            if not p.active:
                continue
            code_bars = [b for b in bars_5m if b["code"] == code]
            px = code_bars[-1]["close"] if code_bars else p.entry_price
            ret = (px / p.entry_price - 1) * 100
            profit = p.shares * (px - p.entry_price)
            cash += p.shares * px
            p.active = False
            last_date = bars_5m[-1]["date"] if bars_5m else date.today()
            trades_5m.append(Trade(
                code, p.entry_date, last_date, p.entry_price, px,
                p.shares, round(ret, 2), round(profit, 0), "FE",
                (last_date - p.entry_date).days,
            ))
            sell_reasons["FE"] += 1

        # 补充净值终值
        active_positions = [p for p in positions.values() if p.active]
        pos_value = sum(p.shares * p.entry_price for p in active_positions)
        equity_curve.append({
            "date": str(end), "equity": round(cash + pos_value, 2),
            "cash": round(cash, 2), "pos": len(active_positions),
        })

        # ── 不变式断言：资金必须自洽 ────────────────────
        total_trade_profit = sum(t.profit for t in trades_5m)
        expected_equity = params["initial_capital"] + total_trade_profit
        final_snapshot_equity = cash + pos_value
        if abs(final_snapshot_equity - expected_equity) > 1.0:
            log.error(
                f"5m回测资金不一致！equity={final_snapshot_equity:.2f} "
                f"expected={expected_equity:.2f} diff={final_snapshot_equity - expected_equity:.2f} "
                f"cash={cash:.2f} pos_value={pos_value:.2f} trades={len(trades_5m)}"
            )

        # ── 指数 ──────────────────────────────────────
        indices = {}
        try:
            indices = load_index_data(start_date=start)
        except Exception:
            pass

        # ── 构建结果 ──────────────────────────────────
        trading_days = sorted(set(b["date"] for b in bars_5m))
        eng = _FakeEngine(trades_5m, equity_curve, params)
        result = _build_result(eng, stock_names, params, trading_days,
                               total_buy_signals, start, end, indices)
        result["summary"]["exit_reasons"] = dict(sell_reasons)
        result["summary"]["data_source"] = "5m"

        if progress_cb:
            progress_cb(3, 4, "5分钟回测完成")
        return result

    except Exception as e:
        import traceback
        log.error(f"5分钟回测崩溃: {e}\n{traceback.format_exc()}")
        return None


class _FakeEngine:
    """适配 _build_result 的最小接口"""
    def __init__(self, trades, equity, params):
        self.trades = trades
        self.equity = equity
        self.p = params


def _run_daily_backtest(sig_result: dict, params: dict, start: date, end: date,
                         progress_cb, stop_event, stock_names: dict) -> dict:
    """日线收盘价回测引擎（原有逻辑）"""
    raw_signals = sig_result.get("signals", {})
    raw_prices = sig_result.get("prices", {})

    # 解析信号
    sig_by_code = {}
    all_signal_codes = set()
    for code, d in raw_signals.items():
        code_num = code.split(".")[0] if "." in code else code
        dates_list = d.get("Date", [])
        zps = d.get("ZP", [])
        if len(dates_list) != len(zps):
            continue
        code_sigs = {}
        has_any = False
        for dt, zp in zip(dates_list, zps):
            try:
                dt_date = date(int(dt[:4]), int(dt[4:6]), int(dt[6:8]))
            except (ValueError, TypeError):
                continue
            if start <= dt_date <= end:
                code_sigs[str(dt_date)] = zp
                if zp == "1":
                    has_any = True
        if has_any:
            sig_by_code[code_num] = code_sigs
            all_signal_codes.add(code_num)

    if not all_signal_codes:
        return _empty_result(params, 0, "区间内无QUANTQQ信号")

    # 解析价格
    prices_by_date = defaultdict(dict)
    for tdx_code, d in raw_prices.items():
        code_num = tdx_code.split(".")[0]
        dates_list = d.get("Date", [])
        closes = d.get("Close", [])
        for dt_str, cl in zip(dates_list, closes):
            try:
                dt_date = date(int(dt_str[:4]), int(dt_str[4:6]), int(dt_str[6:8]))
            except (ValueError, TypeError):
                continue
            if start <= dt_date <= end:
                try:
                    close_val = float(cl)
                    prices_by_date[str(dt_date)][code_num] = {
                        "close": close_val, "high": close_val,
                    }
                except (ValueError, TypeError):
                    pass

    if progress_cb:
        progress_cb(1, 4, f"逐日回放 ({len(prices_by_date)}个交易日)...")

    if stop_event and stop_event.is_set():
        return {"status": "stopped"}

    td_list = sorted(date.fromisoformat(d) for d in prices_by_date.keys())

    eng = FastEngine(td_list, params)
    prev_snap = None
    total_buy_signals = 0

    for d_obj in td_list:
        if stop_event and stop_event.is_set():
            return {"status": "stopped"}

        d_str = str(d_obj)
        snap = prices_by_date.get(d_str, {})

        eng.sell_phase(d_obj, snap, prev_snap)

        if eng.pause:
            eng.record(d_obj, snap)
            prev_snap = snap
            continue

        for code in list(eng.positions.keys()):
            if code not in snap:
                bar = prices_by_date.get(d_str, {}).get(code)
                if bar:
                    snap[code] = bar

        # 动态仓位：按当前净值比例
        eng.position_size = eng.eq(snap) * params["position_ratio"]

        signals_today = sorted(
            code for code, sigs in sig_by_code.items()
            if sigs.get(d_str) == "1"
        )
        total_buy_signals += len(signals_today)
        for code in signals_today:
            bar = snap.get(code)
            if bar is None:
                continue
            try:
                px = float(bar["close"]) if isinstance(bar, dict) else float(bar)
            except (ValueError, TypeError, KeyError):
                continue
            if px <= 0:
                continue
            if eng.buy(d_obj, code, px):
                pass

        eng.record(d_obj, snap)
        prev_snap = snap

    # 最终清仓
    for code, p in list(eng.positions.items()):
        if not p.active or p.remaining <= 0:
            continue
        last_date = td_list[-1] if td_list else date.today()
        last_snap = prices_by_date.get(str(last_date), {})
        bar = last_snap.get(code)
        try:
            px = float(bar["close"]) if isinstance(bar, dict) else float(bar) if bar else p.entry_price
        except (ValueError, TypeError, KeyError):
            px = p.entry_price
        t = eng.sell(p, px, "FE", None, last_date)
        if t:
            t.hold = eng._td(p.entry_date, last_date)
            eng.trades.append(t)

    # 指数
    indices = {}
    try:
        indices = load_index_data(start_date=start)
    except Exception:
        pass

    result = _build_result(eng, stock_names, params, td_list,
                           total_buy_signals, start, end, indices)
    result["summary"]["data_source"] = "daily"
    if progress_cb:
        progress_cb(3, 4, "日线回测完成")
    return result


def _build_result(eng, stock_names, params, td_list, total_buy_signals,
                  start, end, indices):
    """构建与 simple_runner 完全一致的输出格式"""
    trades = eng.trades
    n = len(trades)
    # 买入次数 = 唯一 (code, entry_date) 组合
    unique_buys = len(set((t.code, str(t.entry_date)) for t in trades))
    wins = [t for t in trades if t.ret > 0]
    losses = [t for t in trades if t.ret <= 0]
    nw, nl = len(wins), len(losses)
    wr = nw / n * 100 if n > 0 else 0
    aw = np.mean([t.ret for t in wins]) if wins else 0
    al = np.mean([t.ret for t in losses]) if losses else 0

    gross_profit = sum(t.profit for t in wins)
    gross_loss = abs(sum(t.profit for t in losses)) if losses else 1
    pf = gross_profit / gross_loss if gross_loss > 0 else 0

    best_trade = max(trades, key=lambda t: t.ret) if trades else None
    worst_trade = min(trades, key=lambda t: t.ret) if trades else None

    avg_hold_win = np.mean([t.hold for t in wins]) if wins else 0
    avg_hold_loss = np.mean([t.hold for t in losses]) if losses else 0

    # 交易记录（匹配前端期望的字段名）
    trades_json = []
    for t in trades:
        trades_json.append({
            'code': t.code,
            'name': stock_names.get(t.code, ''),
            'entry_date': str(t.entry_date),
            'entry_time': getattr(t, 'entry_time', None) or '09:30',
            'exit_date': str(t.exit_date),
            'exit_time': getattr(t, 'exit_time', None) or '15:00',
            'entry_px': round(float(t.entry_px), 2),
            'exit_px': round(float(t.exit_px), 2),
            'shares': int(t.shares),
            'ret_pct': round(float(t.ret), 2),
            'profit': round(float(t.profit), 0),
            'entry_total': round(float(t.shares * t.entry_px), 0),
            'exit_total': round(float(t.shares * t.exit_px), 0),
            'reason': t.reason,
            'hold_days': int(t.hold),
        })

    # 净值曲线（匹配前端期望的 norm / dd 字段）
    eq_df = pd.DataFrame(eng.equity)
    if not eq_df.empty:
        initial_capital = params['initial_capital']
        eq_df['norm'] = eq_df['equity'] / initial_capital
        peak = eq_df['equity'].expanding().max()
        eq_df['dd'] = ((peak - eq_df['equity']) / peak * 100)
        equity_json = [
            {
                'date': str(r['date']),
                'equity': round(float(r['equity']), 2),
                'norm': round(float(r['norm']), 4),
                'cash': round(float(r['cash']), 2),
                'pos': int(r['pos']),
                'dd': round(float(r['dd']), 2),
            }
            for _, r in eq_df.iterrows()
        ]
    else:
        equity_json = []

    # 收益分析
    eq_vals = [e['equity'] for e in equity_json] if equity_json else []
    fe = eq_vals[-1] if eq_vals else params['initial_capital']
    total_ret = (fe / params['initial_capital'] - 1) * 100
    peak_val = eq_vals[0] if eq_vals else params['initial_capital']
    max_dd = 0
    for v in eq_vals:
        if v > peak_val:
            peak_val = v
        dd = (peak_val - v) / peak_val * 100
        if dd > max_dd:
            max_dd = dd

    returns = []
    for i in range(1, len(eq_vals)):
        if eq_vals[i - 1] > 0:
            returns.append(eq_vals[i] / eq_vals[i - 1] - 1)
    sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252) if returns and np.std(returns) > 0 else 0
    calmar = total_ret / max_dd if max_dd > 0 else 0
    neg_returns = [r for r in returns if r < 0]
    sortino = np.mean(returns) / np.std(neg_returns) * np.sqrt(252) if neg_returns and np.std(neg_returns) > 0 else 0
    years = len(td_list) / 252 if td_list else 1
    ann_ret = ((1 + total_ret / 100) ** (1 / years) - 1) * 100 if years > 0 else 0

    monthly = {}
    for e in equity_json:
        m = e['date'][:7]
        monthly.setdefault(m, []).append(e['equity'])
    pos_months = sum(1 for m, vals in monthly.items() if vals[-1] >= vals[0])

    pos_counts = [e['pos'] for e in equity_json]
    max_positions_held = max(pos_counts) if pos_counts else 0
    avg_positions_held = round(float(np.mean(pos_counts)), 1) if pos_counts else 0

    rc = Counter(t.reason for t in trades)

    trading_days_span = (end - start).days

    summary = {
        'total_return': round(total_ret, 2),
        'max_drawdown': round(max_dd, 2),
        'win_rate': round(wr, 1),
        'initial_capital': params['initial_capital'],
        'final_equity': round(float(fe), 0),
        'trading_days': len(td_list),
        'total_calendar_days': trading_days_span,
        'start_date': str(start),
        'end_date': str(end),
        'sharpe': round(sharpe, 2),
        'calmar': round(calmar, 2),
        'sortino': round(sortino, 2),
        'profit_ratio': round(float(pf), 2),
        'ann_return': round(float(ann_ret), 2),
        'signals': total_buy_signals,
        'buy_signals': unique_buys,
        'sell_signals': n,
        'trades': unique_buys + n,
        'wins': nw,
        'losses': nl,
        'profit_factor': round(float(pf), 2),
        'best_trade': round(float(best_trade.ret), 2) if best_trade else 0,
        'worst_trade': round(float(worst_trade.ret), 2) if worst_trade else 0,
        'avg_win': round(float(aw), 2),
        'avg_loss': round(float(al), 2),
        'avg_hold_win': round(float(avg_hold_win), 1),
        'avg_hold_loss': round(float(avg_hold_loss), 1),
        'positive_months': f"{pos_months}/{len(monthly)}" if monthly else "0/0",
        'max_positions_held': max_positions_held,
        'avg_positions_held': avg_positions_held,
        'exit_reasons': dict(rc.most_common()),
    }

    return {
        'status': 'ok',
        'summary': summary,
        'equity': equity_json,
        'trades': trades_json,
        'daily_trades': {},
        'indices': indices,
        'params': params,
    }


def _empty_result(params, signal_count, message=""):
    return {
        "status": "ok",
        "summary": {
            "total_return": 0, "max_drawdown": 0, "win_rate": 0,
            "trades": 0, "final_equity": params.get("initial_capital", 1000000),
            "signals": signal_count, "avg_win": 0, "avg_loss": 0,
            "profit_factor": 0, "sharpe": 0, "calmar": 0, "sortino": 0,
            "ann_return": 0, "positive_months": "0/0",
            "trading_days": 0, "total_calendar_days": 0,
        },
        "equity": [],
        "trades": [],
        "indices": {},
        "message": message,
    }
