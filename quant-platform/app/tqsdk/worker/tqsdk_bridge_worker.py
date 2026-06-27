# tqsdk_bridge_worker.py - TDX QUANTQQ 公式选股 Worker
# 由 quant-platform 后端通过 subprocess 调用

import json
import sys
import os
import tempfile
import numpy as np
import pandas as pd

from tqcenter import tq

BATCH_SIZE = 50  # 分批大小，避免"返回数据过大"导致随机丢数据


def _is_signal_value(value_str):
    """
    判断 TDX 返回的 Value 是否代表"选中"。
    兼容:
      - '1', '100', '0.5' → 选中（非零）
      - '0', '0.0', '', None → 未选中（零或空）
    """
    if value_str is None:
        return False
    s = str(value_str).strip()
    if s == "" or s == "0" or s == "0.0":
        return False
    try:
        return float(s) != 0.0
    except (ValueError, TypeError):
        return False


def main():
    args_file = None
    for i, arg in enumerate(sys.argv):
        if arg == "--args-file" and i + 1 < len(sys.argv):
            args_file = sys.argv[i + 1]
            break

    if not args_file:
        print(json.dumps({"status": "error", "message": "Missing --args-file"}))
        sys.exit(1)

    with open(args_file, "r", encoding="utf-8") as f:
        task = json.load(f)

    tq.initialize(__file__)

    task_type = task.get("task_type", "screen")
    all_stocks = sorted(tq.get_stock_list(market="5"))
    stock_list = task.get("stock_list_override") or all_stocks
    output_var = task.get("output_var_name", "ZP")
    match_value = str(task.get("match_value", "1"))

    if task_type in ("range", "range_5m"):
        _do_range(task, stock_list, output_var)
    elif task_type == "fetch_5m":
        task.setdefault("period", "5m")
        _do_fetch_intraday(task)
    elif task_type == "fetch_intraday":
        _do_fetch_intraday(task)
    elif task_type == "probe_formulas":
        _do_probe_formulas(task)
    else:
        _do_screen(task, stock_list, output_var, match_value)


def _do_probe_formulas(task: dict) -> None:
    """
    探测一组候选公式名,返回每个名字的 ErrorId 与最近一次信号日。
    用单股小窗口探测,纯枚举探测公式是否被 TDX 引擎识别。
    ErrorId 含义: 0/19=公式存在且跑通; 4/6/12/14=公式不存在或编译失败;
                  5=公式需要参数; 7=超时; 其它=异常。
    """
    candidates: list = task.get("candidates", [])
    probe_code = task.get("probe_code", "605289.SH")
    probe_end = task.get("end_time", "")
    probe_start = task.get("start_time", probe_end)
    probe_count = int(task.get("count", 30))
    out: list = []
    for name in candidates:
        rec = {"name": name}
        try:
            r = tq.formula_process_mul_xg(
                formula_name=name,
                formula_arg="",
                return_count=0,
                return_date=True,
                stock_list=[probe_code],
                stock_period="1d",
                start_time=probe_start,
                end_time=probe_end,
                count=probe_count,
                dividend_type=1,
            )
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)[:200]
            rec["error_id"] = "EXC"
            out.append(rec)
            continue
        if not r:
            rec["error_id"] = "EMPTY"
            out.append(rec)
            continue
        eid = str(r.get("ErrorId", "?"))
        rec["error_id"] = eid
        rec["err_msg"] = r.get("Error", "") or r.get("error", "")
        sig_payload = r.get(probe_code) or r.get(probe_code.split(".")[0])
        if isinstance(sig_payload, dict):
            trigger_count, last_trigger, hit_var = _probe_check_signal(sig_payload, probe_code, name)
            rec["trigger_count"] = trigger_count
            rec["last_trigger"] = last_trigger
            rec["hit_var"] = hit_var
        out.append(rec)
    print(json.dumps(
        {"status": "ok", "results": out, "probe_code": probe_code},
        ensure_ascii=False,
    ))


def _do_screen(task, stock_list, output_var, match_value):
    """单日选股：只返回 end_time 当天 ZP=1 的股票"""
    end_time = task.get("end_time", "")

    # 统一转换代码格式（如 300806 → 300806.SZ），与 _do_range 保持一致
    stock_list = _to_tdx_codes(stock_list)

    matched = []
    for batch_start in range(0, len(stock_list), BATCH_SIZE):
        batch = stock_list[batch_start:batch_start + BATCH_SIZE]
        try:
            result = tq.formula_process_mul_xg(
                formula_name=task["formula_name"],
                formula_arg=task.get("formula_arg", ""),
                return_count=0,
                return_date=True,
                stock_list=batch,
                stock_period="1d",
                start_time=end_time,
                end_time=end_time,
                count=2000,
                dividend_type=1,
            )
        except Exception as e:
            print(json.dumps({"diag":"worker_batch_error","batch_idx":batch_start//BATCH_SIZE+1,"error":str(e)[:200],"batch_size":len(batch)}))
            continue

        if not result:
            print(json.dumps({"diag":"worker_batch_empty","batch_idx":batch_start//BATCH_SIZE+1,"batch_size":len(batch)}))
            continue
        error_id = result.get("ErrorId", "0")
        if error_id not in ("0", "19"):
            continue

        for code in _do_screen_parse(result, end_time):
            matched.append(code)

    seen = set()
    unique = []
    for c in matched:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    print(json.dumps({"status": "ok", "matched": unique, "total": len(stock_list)}))


def _do_screen_parse(result: dict, end_time: str) -> list:
    """
    从 TDX result 解析出 end_time 当天 ZP=1 的股票代码列表。
    - 遍历所有变量名 (兼容 ZP/ZT/中文/任意)
    - 非零即信号 (兼容 1/100/0.5/任意非零)
    """
    matched = []
    for code, val in result.items():
        if code == "ErrorId" or not val or not isinstance(val, dict):
            continue
        for var_name, entries in val.items():
            if var_name == "ErrorId" or not isinstance(entries, list) or len(entries) == 0:
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                dt = str(entry.get("Date", ""))
                if dt != end_time:
                    continue
                if _is_signal_value(entry.get("Value")):
                    matched.append(code)
                    break
            else:
                continue
            break  # 命中 → 跳出 var_name 循环
    return matched


def _do_range_check_signal(val: dict, code: str):
    """
    解析 TDX result 的某个 code 段, 返回 (has_signal, hit_var, dates, values)。
    - 遍历所有变量名 (兼容任意)
    - 非零即信号
    """
    if not val or not isinstance(val, dict):
        return False, None, [], []
    hit_var = None
    dates, values = [], []
    for var_name, entries in val.items():
        if var_name == "ErrorId" or not isinstance(entries, list) or len(entries) == 0:
            continue
        cur_dates = [str(item.get("Date", "")) for item in entries if isinstance(item, dict)]
        cur_values = [str(item.get("Value", "")) for item in entries if isinstance(item, dict)]
        if any(_is_signal_value(v) for v in cur_values):
            if not hit_var:
                hit_var = var_name
                dates = cur_dates
                values = cur_values
    return bool(hit_var), hit_var, dates, values


def _probe_check_signal(sig_payload: dict, code: str, name: str):
    """
    解析探测结果, 返回 (trigger_count, last_trigger, hit_var)。
    - 遍历所有变量名
    - 非零即信号
    """
    if not sig_payload or not isinstance(sig_payload, dict):
        return 0, None, None
    triggers = []
    hit_var = None
    for var_name, entries in sig_payload.items():
        if var_name == "ErrorId" or not isinstance(entries, list):
            continue
        cur = [(e.get("Date"), e.get("Value")) for e in entries
               if isinstance(e, dict) and _is_signal_value(e.get("Value"))]
        if cur and not hit_var:
            hit_var = var_name
            triggers = cur
    return len(triggers), (triggers[-1][0] if triggers else None), hit_var


def _do_range(task, stock_list, output_var):
    """区间模式：分批调 formula_process_mul_xg + formula_process_mul_zb"""
    start_time = task.get("start_time", "")
    end_time = task.get("end_time", "")

    # Step 1: 分批获取信号
    signals = {}
    signal_codes = set()
    total_batches = (len(stock_list) - 1) // BATCH_SIZE + 1

    for batch_start in range(0, len(stock_list), BATCH_SIZE):
        batch = stock_list[batch_start:batch_start + BATCH_SIZE]
        batch_idx = batch_start // BATCH_SIZE + 1
        try:
            sig_result = tq.formula_process_mul_xg(
                formula_name=task["formula_name"],
                formula_arg=task.get("formula_arg", ""),
                return_count=0,
                return_date=True,
                stock_list=batch,
                stock_period="1d",
                start_time=start_time,
                end_time=end_time,
                count=2000,
                dividend_type=1,
            )
        except Exception as e:
            print(json.dumps({"diag":"worker_signals","batch":batch_idx,"error":str(e)[:200],"stock_count":len(batch),"first_stock":batch[0] if batch else "none"}))
            continue

        if not sig_result:
            continue
        error_id = sig_result.get("ErrorId", "0")
        if error_id not in ("0", "19"):
            continue

        for code, val in sig_result.items():
            if code == "ErrorId" or not val or not isinstance(val, dict):
                continue
            has_signal, hit_var, dates, values = _do_range_check_signal(val, code)
            if not dates:
                continue
            signals[code] = {"Date": dates, hit_var or output_var: values}
            if has_signal:
                signal_codes.add(code)

    if not signal_codes:
        print(json.dumps({"status": "ok", "signals": signals, "prices": {},
                          "total": len(stock_list)}))
        return

    # Step 2: 从 TDX 取收盘价（用 get_market_data，不用 formula_process_mul_zb）
    tdx_codes = sorted(signal_codes)
    # 转换代码格式
    tdx_codes_full = _to_tdx_codes(tdx_codes)

    # 计算需要的K线数量
    from datetime import datetime
    try:
        end_dt = datetime.strptime(end_time, "%Y%m%d")
        start_dt = datetime.strptime(start_time, "%Y%m%d") if start_time else end_dt.replace(year=end_dt.year-1)
        est_days = max(100, int((end_dt - start_dt).days * 0.7))
    except Exception:
        est_days = 200
    bar_count = est_days + 50

    prices = {}
    for batch_start in range(0, len(tdx_codes_full), BATCH_SIZE):
        batch = tdx_codes_full[batch_start:batch_start + BATCH_SIZE]
        try:
            mk = tq.get_market_data(
                field_list=["open", "high", "low", "close"],
                stock_list=batch,
                period="1d",
                start_time=start_time or "",
                end_time=end_time,
                count=bar_count,
                dividend_type="front",
                fill_data=True,
            )
        except Exception as e:
            print(json.dumps({"diag":"worker_prices","error":str(e)[:200],"batch_size":len(batch)}))
            continue

        if not mk or "Close" not in mk:
            continue

        close_df = mk["Close"]
        # 兼容老版本 TDX（可能 low/high/open 字段缺失）
        high_df = mk.get("High") if "High" in mk else None
        low_df = mk.get("Low") if "Low" in mk else None
        open_df = mk.get("Open") if "Open" in mk else None

        def _col_to_values(df, col):
            """安全地把 pandas 列转成字符串列表(NaN -> '0')"""
            if df is None:
                return None
            vals = []
            for dt in df.index:
                try:
                    v = float(df.loc[dt, col])
                    if np.isnan(v) or v <= 0:
                        vals.append("0")
                    else:
                        vals.append(str(v))
                except Exception:
                    vals.append("0")
            return vals

        for col in close_df.columns:
            code_full = str(col)
            code_num = code_full.split(".")[0]
            dates = [str(dt)[:10].replace("-", "") for dt in close_df.index]
            entry = {"Date": dates, "Close": _col_to_values(close_df, col)}
            # 低/高/开字段如有则一并传出(供回测引擎做 OHLC 回放)
            if high_df is not None:
                entry["High"] = _col_to_values(high_df, col)
            if low_df is not None:
                entry["Low"] = _col_to_values(low_df, col)
            if open_df is not None:
                entry["Open"] = _col_to_values(open_df, col)
            prices[code_num] = entry

    print(json.dumps({
        "status": "ok", "signals": signals, "prices": prices,
        "total": len(stock_list),
    }))


def _do_fetch_intraday(task):
    """
    仅为指定股票列表获取 5 分钟 OHLC，写入临时 parquet。
    作为第二步独立调用，股票列表已由 range 结果的信号股过滤。
    """
    codes = task.get("stock_list_override", [])
    start_date = task.get("start_date")
    end_time = task.get("end_time")
    period = task.get("period", "5m")

    if not codes:
        print(json.dumps({"status": "ok", "bars_path": None}))
        return

    tdx_codes = _to_tdx_codes(codes)

    from datetime import datetime, timedelta
    if not start_date:
        end_dt = datetime.strptime(end_time, "%Y%m%d")
        start_dt = end_dt - timedelta(days=120)
        start_date = start_dt.strftime("%Y%m%d")

    from datetime import datetime, timedelta
    try:
        end_dt = datetime.strptime(end_time, "%Y%m%d")
        start_dt = datetime.strptime(start_date, "%Y%m%d")
        est_trading_days = max(1, int((end_dt - start_dt).days * 0.7))
    except Exception:
        est_trading_days = 30
    bars_per_day = 241 if period == "1m" else 48
    bar_count = min(est_trading_days * bars_per_day + 100, 20000)

    bars_path = None
    record_count = 0
    try:
        mk_result = tq.get_market_data(
            field_list=["open", "high", "low", "close"],
            stock_list=tdx_codes,
            period=task.get("period", "5m"),
            start_time=start_date,
            end_time=end_time,
            count=bar_count,
            dividend_type="front",
            fill_data=True,
        )

        if mk_result and "Close" in mk_result:
            close_df = mk_result["Close"]
            high_df = mk_result["High"]
            low_df = mk_result["Low"]
            open_df = mk_result["Open"]

            records = []
            for dt_idx in close_df.index:
                dt_str = str(dt_idx)
                for col in close_df.columns:
                    code_full = str(col)
                    code_num = code_full.split(".")[0]
                    try:
                        o = float(open_df.loc[dt_idx, col])
                        h = float(high_df.loc[dt_idx, col])
                        l_val = float(low_df.loc[dt_idx, col])
                        c = float(close_df.loc[dt_idx, col])
                        if np.isnan(c) or c <= 0:
                            continue
                        records.append({
                            "datetime": dt_str,
                            "code": code_num,
                            "open": o, "high": h, "low": l_val, "close": c,
                        })
                    except Exception:
                        continue

            if records:
                df_intra = pd.DataFrame(records)
                fd, bars_path = tempfile.mkstemp(
                    suffix=".parquet", prefix="tdx_intra_"
                )
                os.close(fd)
                df_intra.to_parquet(bars_path, index=False)
                record_count = len(records)
    except Exception:
        pass

    print(json.dumps({
        "status": "ok",
        "bars_path": bars_path,
        "count": record_count,
    }))


def _to_tdx_codes(codes):
    """将代码转为 tq 格式（如 000001 → 000001.SZ）"""
    tdx_codes = []
    for c in codes:
        code_num = c.split(".")[0] if "." in c else c
        prefix = code_num[:1]
        suffix = ".SH" if prefix in ("6", "9") else ".SZ"
        tdx_codes.append(f"{code_num}{suffix}")
    return tdx_codes


if __name__ == "__main__":
    main()
