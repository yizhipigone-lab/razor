"""
TDX 公式回测引擎
从通达信获取信号 + 价格，逐日回放买卖，输出格式严格匹配 simple_runner
优先使用5分钟线逐K线检查，某只股票没有5分钟线时降级日线OHLC
"""

import numpy as np
import pandas as pd
from datetime import date, timedelta
from collections import defaultdict, Counter
from pathlib import Path
from typing import Callable, Optional

from core.logger import get_logger
from app.backtest.simple_runner import FastEngine, Position, Trade, load_index_data
from app.backtest.execution import can_buy, can_sell_today

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

    # ── 日内K线回测（优先），失败降级日线 ──────────
    period = params.get("intraday_freq", "5m")
    # 选什么精度就严格按照什么精度：daily 直接走日线路径
    if period == "daily":
        if progress_cb:
            progress_cb(0, 5, "使用日线回测...")
        sig_result = bridge.execute_screen_range(
            end_time=end_time, kline_count=kline_count, start_time=formula_start)
        if sig_result.get("status") != "ok":
            return {"status": "error", "message": sig_result.get("message", "TDX 信号获取失败")}
        return _run_daily_backtest(
            sig_result, params, start, end, progress_cb, stop_event, stock_names or {})
    # 日内精度: 5m 或 1m
    period = params.get("intraday_freq", "5m")
    if period not in ("1m", "5m"):
        period = "5m"
    is_intraday = False
    stocks_with_intraday = set()

    try:
        if progress_cb:
            progress_cb(0, 5, f"尝试获取{period}线数据...")
        sig_result = bridge.execute_screen_range_intraday(
            end_time=end_time,
            kline_count=kline_count,
            start_time=formula_start,
            signal_start=start_time_str,
            period=period,
        )
        if sig_result.get("status") == "ok":
            bars_intra = sig_result.get("bars_intraday", sig_result.get("bars_intra", []))
            valid_bars = [b for b in (bars_intra or []) if b.get("close", 0) > 0]
            if valid_bars:
                is_intraday = True
                for b in valid_bars:
                    code = b.get("code", "")
                    if code:
                        stocks_with_intraday.add(code.split(".")[0] if "." in code else code)
    except Exception as e:
        log.warning(f"{period}线获取失败: {e}，降级日线")

    if not is_intraday and period == "1m":
        # 1m失败→尝试5m
        if progress_cb:
            progress_cb(0, 5, "1分钟线不可用，降级尝试5分钟线...")
        try:
            sig_result = bridge.execute_screen_range_intraday(
                end_time=end_time, kline_count=kline_count,
                start_time=formula_start, signal_start=start_time_str, period="5m")
            if sig_result.get("status") == "ok":
                bars_5m = sig_result.get("bars_intraday", sig_result.get("bars_5m", []))
                valid_bars = [b for b in (bars_5m or []) if b.get("close", 0) > 0]
                if valid_bars:
                    is_intraday = True
                    period = "5m"
                    stocks_with_intraday = set()
                    for b in valid_bars:
                        code = b.get("code", "")
                        if code:
                            stocks_with_intraday.add(code.split(".")[0] if "." in code else code)
        except Exception:
            pass

    if not is_intraday:
        if progress_cb:
            progress_cb(0, 5, "日内数据不可用，降级日线回测...")
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

    if progress_cb:
        progress_cb(0, 5, f"{period}逐K线回放 ({len(stocks_with_intraday)}只)...")

    return _run_intraday_backtest(
        sig_result, params, start, end, progress_cb,
        stop_event, stock_names or {}, stocks_with_intraday, period,
    )


def _check_stops_daily(pos, close_p, high_p, hold_days, params, low_p=None, open_p=None):
    """单日止盈止损检查（委托给统一规则引擎）"""
    from app.backtest.exit_rules import exit_rule_engine

    if high_p > pos.peak_price:
        pos.peak_price = high_p

    # 优先用真实盘中 low，否则 fallback 到 close
    actual_low = low_p if low_p is not None else close_p
    # 优先用真实 open，否则 fallback 到 close
    actual_open = open_p if open_p is not None else close_p
    bar = {"close": close_p, "high": high_p, "low": actual_low, "open": actual_open}
    ctx = exit_rule_engine.build_context(pos, bar, hold_days, params,
                                          first_day_hold_value=1)
    signal = exit_rule_engine.check(ctx)

    if signal is None:
        return None

    if signal.reason.startswith('TP'):
        idx = int(signal.reason[2]) - 1
        pos.tp_triggered.add(idx)
        ss = int(pos.shares * signal.sell_ratio / 100) * 100
        if ss < 100:
            ss = 100
        ss = min(ss, int(pos.shares))
        return (signal.reason, signal.sell_price, ss)

    return (signal.reason, signal.sell_price, None) if signal.sell_ratio >= 1.0 else None


def _run_intraday_backtest(sig_result: dict, params: dict, start: date, end: date,
                            progress_cb, stop_event, stock_names: dict,
                            stocks_with_intraday: set, period: str = "5m") -> dict | None:
    """日内K线回测引擎：有日内数据的逐K线检查，无数据的每日收盘检查"""
    from app.backtest.exit_rules import exit_rule_engine
    # L26 修复: 用交易日计数(替代日历天)
    from app.backtest.trading_calendar import TradingCalendar
    try:
        raw_signals = sig_result.get("signals", {})
        bars_intra = sig_result.get("bars_intraday", sig_result.get("bars_intra", []))
        raw_prices = sig_result.get("prices", {})

        # 过滤有效K线
        bars_intra = [b for b in (bars_intra or []) if b.get("close", 0) > 0]

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

        # 解析日线价格（用于无5m数据的股票）
        # raw_prices 来自 TDX bridge,正常包含 Date/Open/High/Low/Close 5个字段
        # 如果 TDX worker 老版本没传 Low 字段, fallback 从本地 parquet 补
        prices_by_date = defaultdict(dict)
        daily_dir = Path(__file__).parent.parent.parent / "data" / "parquet" / "daily"
        # 缓存 parquet 加载的 low 数据: {(date_str, code): low}
        low_cache = {}
        for tdx_code, d in raw_prices.items():
            code_num = tdx_code.split(".")[0] if "." in tdx_code else tdx_code
            dates_list = d.get("Date", [])
            closes = d.get("Close", [])
            highs = d.get("High", [])
            lows = d.get("Low", [])
            opens = d.get("Open", [])
            # TDX 老版本可能只返回 Close, 没有 High/Low/Open
            has_ohlc = len(highs) > 0 and len(lows) > 0 and len(opens) > 0
            for i, (dt_str, cl) in enumerate(zip(dates_list, closes)):
                try:
                    dt_date = date(int(dt_str[:4]), int(dt_str[4:6]), int(dt_str[6:8]))
                except (ValueError, TypeError):
                    continue
                if start <= dt_date <= end:
                    try:
                        close_val = float(cl)
                        # 优先用 TDX 返回的 OHLC
                        if has_ohlc and i < len(highs) and i < len(lows) and i < len(opens):
                            try:
                                high_val = float(highs[i])
                                low_val = float(lows[i])
                                open_val = float(opens[i])
                                # 任何字段为 0/NaN 视为缺失, fallback 到 parquet
                                if low_val <= 0 or high_val <= 0 or open_val <= 0:
                                    raise ValueError("OHLC has zero, fallback to parquet")
                            except (ValueError, TypeError):
                                has_ohlc = False  # 老 TDX/部分字段缺失, 全用 parquet
                        if not has_ohlc:
                            # 从 parquet 读 low
                            cache_key = (dt_str, code_num)
                            if cache_key not in low_cache:
                                low_val = close_val  # fallback
                                pq = daily_dir / f"{code_num}.parquet"
                                if pq.exists():
                                    try:
                                        pdf = pd.read_parquet(str(pq), columns=['date', 'low'])
                                        pdf['date'] = pd.to_datetime(pdf['date']).dt.strftime('%Y-%m-%d')
                                        row = pdf[pdf['date'] == dt_str]
                                        if not row.empty:
                                            low_val = float(row.iloc[0]['low'])
                                    except Exception:
                                        pass
                                low_cache[cache_key] = low_val
                            low_val = low_cache[cache_key]
                            high_val = close_val  # 高没用但保留结构
                            open_val = close_val
                        prices_by_date[str(dt_date)][code_num] = {
                            "close": close_val, "high": high_val, "low": low_val, "open": open_val,
                        }
                    except (ValueError, TypeError):
                        pass

        # 按日内数据有无分组
        no_intraday_codes = all_signal_codes - stocks_with_intraday
        log.info(f"日内回测: {len(stocks_with_intraday)}只有{period}数据, {len(no_intraday_codes)}只降级日线")

        if progress_cb:
            progress_cb(1, 4, f"{period}逐K线回放 ({len(bars_intra)}根K线, {len(stocks_with_intraday)}只{period} + {len(no_intraday_codes)}只日线)...")

        if stop_event and stop_event.is_set():
            return {"status": "stopped"}

        # 按时间排序5m bars
        bars_intra.sort(key=lambda x: x.get("datetime", ""))
        for b in bars_intra:
            dt_str = b.get("datetime", "")
            if len(dt_str) >= 10:
                b["date"] = date.fromisoformat(dt_str[:10])
            b.setdefault("high", b.get("close", 0))
            b.setdefault("low", b.get("close", 0))
            b.setdefault("open", b.get("close", 0))

        # ── 引擎状态 ─────────────────────────────────
        cash = params["initial_capital"]
        position_ratio = params["position_ratio"]
        positions = {}          # code -> Position (both 5m and no-5m)
        trades_all = []          # list of Trade
        equity_curve = []
        cooldown = {}
        pending_buys = defaultdict(list)
        sell_reasons = Counter()
        total_buy_signals = 0
        prev_day = None

        for code in sorted(sig_by_code.keys()):
            for dt_str, zp in sig_by_code[code].items():
                if zp == "1":
                    pending_buys[dt_str].append(code)

        # 收集所有交易日（5m bars 的日期 + prices 的日期）
        all_dates = set()
        for b in bars_intra:
            all_dates.add(str(b["date"]))
        for d_str in prices_by_date.keys():
            all_dates.add(d_str)
        sorted_dates = sorted(all_dates)

        # L26 修复: 交易日历实例, sorted_dates 为 trading dates
        _cal_for_hold = TradingCalendar(
            [date.fromisoformat(d) for d in sorted_dates if d]
        )

        # ── 逐 K 线循环（只处理有5m数据的股票） ──────
        bar_idx = 0
        for d_str in sorted_dates:
            if stop_event and stop_event.is_set():
                return {"status": "stopped"}

            d = date.fromisoformat(d_str)
            log_prefix = ""
            day_bars_start = bar_idx

            # ── 新的一天：执行买入信号 ──────────────
            if d_str in pending_buys:
                for code in pending_buys[d_str]:
                    if code in positions:
                        continue
                    if code in cooldown and (d - cooldown[code]).days < params.get("same_stock_cooldown", 20):
                        continue
                    # 动态仓位
                    mkt_value = 0
                    for pc, pp in positions.items():
                        if not pp.active: continue
                        if pc in stocks_with_intraday:
                            bar_px = next((b["close"] for b in bars_intra[max(0,bar_idx-50):bar_idx]
                                          if b["code"] == pc and str(b["date"]) == d_str), pp.entry_price)
                        else:
                            bar_px = prices_by_date.get(d_str, {}).get(pc, {}).get("close", pp.entry_price)
                        mkt_value += pp.shares * bar_px
                    current_equity = cash + mkt_value
                    dyn_size = current_equity * position_ratio
                    if cash < dyn_size * 0.5:
                        break

                    if code in stocks_with_intraday:
                        # 有5m数据：用当天第一根bar的close买入
                        bar_for_code = next(
                            (b for b in bars_intra[bar_idx:] if b["code"] == code and str(b["date"]) == d_str), None
                        )
                        if bar_for_code is None:
                            continue
                        px = bar_for_code["close"]
                    else:
                        # 无5m数据：用日线收盘价买入
                        day_price = prices_by_date.get(d_str, {}).get(code)
                        if day_price is None:
                            continue
                        px = day_price["close"]

                    if px <= 0:
                        continue
                    # L29 修复: 涨停买入过滤 - 委托给 execution.can_buy
                    # 优先从 prices_by_date 取前收(昨日 close), 取不到 fallback 0(放过)
                    prev_close = 0
                    if prev_day is not None:
                        prev_snap = prices_by_date.get(str(prev_day), {})
                        prev_bar = prev_snap.get(code, {})
                        if isinstance(prev_bar, dict):
                            prev_close = prev_bar.get("close", 0) or 0
                    can_buy_ok, _ = can_buy(code, prev_close, px)
                    if not can_buy_ok:
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

            # ── 处理当天的5分钟K线（有5m数据的股票） ──
            while bar_idx < len(bars_intra) and str(bars_intra[bar_idx]["date"]) == d_str:
                bar = bars_intra[bar_idx]
                code = bar["code"]
                code_num = code.split(".")[0] if "." in code else code

                pos = positions.get(code_num)
                if pos and pos.active and code_num in stocks_with_intraday:
                    # L29 修复: T+1 约束 - 当日买入不能当日卖出
                    # 原 bug: 5m 循环内会立刻检查止损/止盈导致 T+0 卖出
                    if not can_sell_today(pos.entry_date, d):
                        pass  # 当日买入持仓,不做任何卖出/止损检查
                    else:
                        h = bar["high"]
                        l = bar["low"]
                        c = bar["close"]
                        if h > pos.peak_price:
                            pos.peak_price = h

                        entry = pos.entry_price
                        # L26 修复: 用交易日计数(替代日历天)
                        hold_days = _cal_for_hold.trading_days_between(pos.entry_date, d)

                        # 用统一规则引擎
                        ctx = exit_rule_engine.build_context(
                            pos, bar, hold_days, params, use_high_for_tp=True,
                            first_day_hold_value=1
                        )
                        signal = exit_rule_engine.check(ctx)

                        reason = None
                        sell_px = None
                        partial_sell = None

                        if signal:
                            reason = signal.reason
                            sell_px = signal.sell_price
                            if signal.reason.startswith('TP'):
                                idx = int(signal.reason[2]) - 1
                                pos.tp_triggered.add(idx)
                                ss = int(pos.shares * signal.sell_ratio / 100) * 100
                                if ss < 100:
                                    ss = 100
                                partial_sell = min(ss, int(pos.shares))

                        if reason and sell_px and sell_px > 0:
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
                            trades_all.append(Trade(
                                code_num, pos.entry_date, d, entry, sell_px,
                                sell_shares, round(ret, 2), round(profit, 0), reason,
                                hold_days,
                            ))
                            sell_reasons[reason] += 1
                            cooldown[code_num] = d

                bar_idx += 1

            # ── 当天结束后：检查无5m数据的持仓 ────────
            for code_num in list(positions.keys()):
                if code_num in stocks_with_intraday:
                    continue  # 已在逐K线中处理
                pos = positions[code_num]
                if not pos.active:
                    continue

                day_price = prices_by_date.get(d_str, {}).get(code_num)
                if day_price is None:
                    continue
                close_p = day_price["close"]
                # 用 TDX 真实 high/low/open(不再用 close 近似)
                high_p = day_price.get("high", close_p)
                low_p = day_price.get("low", close_p)
                open_p = day_price.get("open", close_p)
                # L26 修复: 用交易日计数(替代日历天)
                hold_days = _cal_for_hold.trading_days_between(pos.entry_date, d)

                result = _check_stops_daily(pos, close_p, high_p, hold_days, params, low_p=low_p, open_p=open_p)
                if result:
                    reason, sell_px, partial = result
                    sell_shares = partial if partial else pos.shares
                    sell_shares = min(sell_shares, pos.shares)
                    if sell_shares <= 0:
                        sell_shares = pos.shares
                    ret = (sell_px / pos.entry_price - 1) * 100
                    profit = sell_shares * (sell_px - pos.entry_price)
                    cash += sell_shares * sell_px
                    if sell_shares >= pos.shares:
                        pos.active = False
                    else:
                        pos.shares -= sell_shares
                    trades_all.append(Trade(
                        code_num, pos.entry_date, d, pos.entry_price, sell_px,
                        sell_shares, round(ret, 2), round(profit, 0), reason,
                        hold_days,
                    ))
                    sell_reasons[reason] += 1
                    cooldown[code_num] = d

            # 清理已平仓
            positions = {k: v for k, v in positions.items() if v.active}

            # 记录净值
            pos_value = 0
            for pc, p in positions.items():
                if pc in stocks_with_intraday:
                    px = next((b["close"] for b in reversed(bars_intra[:bar_idx])
                              if b["code"] == pc and str(b["date"]) == d_str), p.entry_price)
                else:
                    px = prices_by_date.get(d_str, {}).get(pc, {}).get("close", p.entry_price)
                pos_value += p.shares * px
            equity_curve.append({
                "date": d_str, "equity": round(cash + pos_value, 2),
                "cash": round(cash, 2), "pos": len(positions),
            })

            # 更新 prev_day,供下一天买入时取前收
            prev_day = d

        # ── 最终清仓 ──────────────────────────────────
        for code, p in list(positions.items()):
            if not p.active:
                continue
            if code in stocks_with_intraday:
                code_bars = [b for b in bars_intra if b["code"] == code]
                px = code_bars[-1]["close"] if code_bars else p.entry_price
            else:
                last_snap = prices_by_date.get(str(sorted_dates[-1]), {})
                px = last_snap.get(code, {}).get("close", p.entry_price)
            ret = (px / p.entry_price - 1) * 100
            profit = p.shares * (px - p.entry_price)
            cash += p.shares * px
            p.active = False
            last_date = date.fromisoformat(sorted_dates[-1]) if sorted_dates else end
            trades_all.append(Trade(
                code, p.entry_date, last_date, p.entry_price, px,
                p.shares, round(ret, 2), round(profit, 0), "FE",
                (last_date - p.entry_date).days,
            ))
            sell_reasons["FE"] += 1

        # 补充净值终值 (L27: 用 close 而非 entry_price 计算持仓市值)
        active_positions = [pp for pp in positions.values() if pp.active]
        pos_value = 0
        for p in active_positions:
            if p.code in stocks_with_intraday:
                code_bars = [b for b in bars_intra if b["code"] == p.code]
                px = code_bars[-1]["close"] if code_bars else p.entry_price
            else:
                last_snap = prices_by_date.get(str(sorted_dates[-1]), {})
                px = last_snap.get(p.code, {}).get("close", p.entry_price)
            pos_value += p.shares * px
        equity_curve.append({
            "date": str(end), "equity": round(cash + pos_value, 2),
            "cash": round(cash, 2), "pos": len(active_positions),
        })

        # ── 不变式断言 ──────────────────────────────
        total_trade_profit = sum(t.profit for t in trades_all)
        expected_equity = params["initial_capital"] + total_trade_profit
        final_snapshot_equity = cash + pos_value
        if abs(final_snapshot_equity - expected_equity) > 2.0:
            log.error(
                f"混合回测资金不一致！equity={final_snapshot_equity:.2f} "
                f"expected={expected_equity:.2f} diff={final_snapshot_equity - expected_equity:.2f} "
                f"cash={cash:.2f} pos_value={pos_value:.2f} trades={len(trades_all)}"
            )

        # ── 指数 ──────────────────────────────────────
        indices = {}
        try:
            indices = load_index_data(start_date=start)
        except Exception:
            pass

        # ── 构建结果 ──────────────────────────────────
        trading_days = sorted(set(b["date"] for b in bars_intra))
        if not trading_days:
            trading_days = [date.fromisoformat(d) for d in sorted_dates]
        eng = _FakeEngine(trades_all, equity_curve, params)
        result = _build_result(eng, stock_names, params, trading_days,
                               total_buy_signals, start, end, indices)
        result["summary"]["exit_reasons"] = dict(sell_reasons)
        result["summary"]["data_source"] = f"hybrid({period}:{len(stocks_with_intraday)}/dl:{len(no_intraday_codes)})"

        if progress_cb:
            progress_cb(3, 4, f"{period}回测完成")
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
    """日线收盘价回测引擎（原有的 FastEngine 逻辑）"""
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
    # raw_prices 来自 TDX bridge，只有 Date 和 Close 字段，没有 low
    # 所以从本地 parquet 补 low 字段，确保 _check_stops_daily 用真实盘中 low
    prices_by_date = defaultdict(dict)
    daily_dir = Path(__file__).parent.parent.parent / "data" / "parquet" / "daily"
    low_cache = {}
    for tdx_code, d in raw_prices.items():
        code_num = tdx_code.split(".")[0] if "." in tdx_code else tdx_code
        dates_list = d.get("Date", [])
        closes = d.get("Close", [])
        highs = d.get("High", [])
        lows = d.get("Low", [])
        opens = d.get("Open", [])
        has_ohlc = len(highs) > 0 and len(lows) > 0 and len(opens) > 0
        for i, (dt_str, cl) in enumerate(zip(dates_list, closes)):
            try:
                dt_date = date(int(dt_str[:4]), int(dt_str[4:6]), int(dt_str[6:8]))
            except (ValueError, TypeError):
                continue
            if start <= dt_date <= end:
                try:
                    close_val = float(cl)
                    if has_ohlc and i < len(highs) and i < len(lows) and i < len(opens):
                        try:
                            high_val = float(highs[i])
                            low_val = float(lows[i])
                            open_val = float(opens[i])
                            if low_val <= 0 or high_val <= 0 or open_val <= 0:
                                raise ValueError("OHLC has zero, fallback to parquet")
                        except (ValueError, TypeError):
                            has_ohlc = False
                    if not has_ohlc:
                        cache_key = (dt_str, code_num)
                        if cache_key not in low_cache:
                            low_val = close_val
                            pq = daily_dir / f"{code_num}.parquet"
                            if pq.exists():
                                try:
                                    pdf = pd.read_parquet(str(pq), columns=['date', 'low'])
                                    pdf['date'] = pd.to_datetime(pdf['date']).dt.strftime('%Y-%m-%d')
                                    row = pdf[pdf['date'] == dt_str]
                                    if not row.empty:
                                        low_val = float(row.iloc[0]['low'])
                                except Exception:
                                    pass
                            low_cache[cache_key] = low_val
                        low_val = low_cache[cache_key]
                        high_val = close_val
                        open_val = close_val
                    prices_by_date[str(dt_date)][code_num] = {
                        "close": close_val, "high": high_val, "low": low_val, "open": open_val,
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
