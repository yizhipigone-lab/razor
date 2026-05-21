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
from app.backtest.simple_runner import FastEngine, load_index_data

log = get_logger("TdxBT")


def run_tdx_backtest(params: dict, progress_cb: Optional[Callable] = None,
                     stop_event=None, stock_names: Optional[dict] = None) -> dict:
    """TDX 策略回测入口"""
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

    if progress_cb:
        progress_cb(0, 4, f"从通达信获取信号 (QUANTQQ, {kline_count}条K线)...")

    bridge = TdxBridge()
    if stop_event and stop_event.is_set():
        return {"status": "stopped"}

    sig_result = bridge.execute_screen_range(
        end_time=end_time,
        kline_count=kline_count,
    )
    if sig_result.get("status") != "ok":
        return {"status": "error", "message": sig_result.get("message", "TDX 信号获取失败")}

    raw_signals = sig_result.get("signals", {})
    raw_prices = sig_result.get("prices", {})
    if not raw_signals:
        return {"status": "error", "message": "TDX 返回空数据，请确认通达信已启动"}

    # 解析信号（去掉代码后缀）
    sig_by_code = {}
    all_signal_codes = set()
    for code, d in raw_signals.items():
        code_num = code.split(".")[0] if "." in code else code
        dates = d.get("Date", [])
        zps = d.get("ZP", [])
        if len(dates) != len(zps):
            continue
        code_sigs = {}
        has_any = False
        for dt, zp in zip(dates, zps):
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
        dates = d.get("Date", [])
        closes = d.get("Close", [])
        for dt_str, cl in zip(dates, closes):
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

    params.setdefault("initial_capital", 1_000_000)
    params.setdefault("position_size", 50_000)
    params.setdefault("min_buy_amt", 5_000)
    params.setdefault("hard_stop", -0.06)
    params.setdefault("take_profit_tiers", [{"profit_pct": 0.03, "sell_ratio": 0.10}])
    params.setdefault("trail_activate", 0.06)
    params.setdefault("trail_dd", 0.03)
    params.setdefault("time_exit_days", 20)
    params.setdefault("time_exit_profit", -0.02)
    params.setdefault("time_force_days", 60)
    params.setdefault("loss_streak_pause", 5)
    params.setdefault("pause_days", 3)
    params.setdefault("loss_streak_halve", 3)
    params.setdefault("same_stock_cooldown", 20)

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

        # 补充 snap 中当前持仓股票的价格（可能当天无信号但需要止损检查）
        for code in list(eng.positions.keys()):
            if code not in snap:
                bar = prices_by_date.get(d_str, {}).get(code)
                if bar:
                    snap[code] = bar

        signals_today = [
            code for code, sigs in sig_by_code.items()
            if sigs.get(d_str) == "1"
        ]
        total_buy_signals += len(signals_today)
        max_buy_today = params.get("max_daily_buys", 20)
        bought = 0
        for code in signals_today:
            if bought >= max_buy_today:
                break
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
                bought += 1

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

    result = _build_result(eng, stock_names or {}, params, td_list,
                           total_buy_signals, start, end, indices)
    if progress_cb:
        progress_cb(3, 4, "回测完成")
    return result


def _build_result(eng, stock_names, params, td_list, total_buy_signals,
                  start, end, indices):
    """构建与 simple_runner 完全一致的输出格式"""
    trades = eng.trades
    n = len(trades)
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
            'exit_date': str(t.exit_date),
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
        'trades': n,
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
