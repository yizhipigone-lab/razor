"""
TDX 公式选股桥接层
通过 subprocess 调用 TDX PYPlugins 目录下的 worker 脚本执行公式选股
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from core.logger import get_logger
log = get_logger("TdxBridge")

TDX_USER_DIR = Path(r"E:\NEW_TDX\PYPlugins\user")
WORKER_SCRIPT = TDX_USER_DIR / "tqsdk_bridge_worker.py"

def _get_formula_name():
    try:
        from core.settings import settings
        name = settings.get("tqsdk", "formula_name", default="QUANTQQ")
        return name
    except Exception:
        return "QUANTQQ"

OUTPUT_VAR = "ZP"
MATCH_VALUE = "1"
TIMEOUT = 600  # 10分钟


class TdxBridge:
    """通达信公式选股桥接器"""

    def execute_screen(self, end_time: str, stock_list_override: list = None,
                       lookback_days: int = 30):
        """单日选股：返回当天 ZP=1 的股票列表"""
        task = {
            "formula_name": _get_formula_name(),
            "formula_arg": "",
            "output_var_name": OUTPUT_VAR,
            "match_value": MATCH_VALUE,
            "end_time": end_time,
            "stock_list_override": stock_list_override,
            "lookback_days": lookback_days,
            "return_date": False,
        }
        return self._run_worker(task)

    def execute_screen_range(self, end_time: str, kline_count: int,
                              return_count: int = None,
                              stock_list_override: list = None,
                              start_time: str = ""):
        """
        区间选股：返回信号 + 价格

        Returns:
            {status: 'ok', signals: {code: {ZP: [...], Date: [...]}},
                          prices: {code: {Close: [...], Date: [...]}}}
        """
        if return_count is None:
            return_count = kline_count
        task = {
            "task_type": "range",
            "formula_name": _get_formula_name(),
            "formula_arg": "",
            "output_var_name": OUTPUT_VAR,
            "end_time": end_time,
            "start_time": start_time,
            "stock_list_override": stock_list_override,
            "kline_count": kline_count,
            "return_count": return_count,
        }
        return self._run_worker(task, timeout_multiplier=max(2, kline_count // 50))

    def execute_screen_range_5m(self, end_time: str, kline_count: int,
                                 start_date: str = None,
                                 return_count: int = None,
                                 stock_list_override: list = None,
                                 start_time: str = "",
                                 signal_start: str = ""):
        """
        区间选股 5分钟增强版：两步调用 worker
        Step 1: range → 信号 + 日线收盘价 (快)
        Step 2: fetch_5m → 仅对信号股获取 5 分钟 OHLC (限 300 只)
        失败自动降级，bars_5m 为 None 时走日线回退
        """
        MAX_5M_BARS = 5_000_000  # 最多500万根5分钟K线

        if return_count is None:
            return_count = kline_count

        # ── Step 1: 获取信号 + 日线收盘价 ──────────────────
        task1 = {
            "task_type": "range",
            "formula_name": _get_formula_name(),
            "formula_arg": "",
            "output_var_name": OUTPUT_VAR,
            "end_time": end_time,
            "start_time": start_time,
            "stock_list_override": stock_list_override,
            "kline_count": kline_count,
            "return_count": return_count,
        }
        result = self._run_worker(task1, timeout_multiplier=min(10, max(2, kline_count // 50)))
        result["bars_5m"] = None

        if result.get("status") != "ok":
            return result

        # ── Step 2: 获取 5 分钟 OHLC（仅对信号股，且信号在回测区间内） ──
        signals = result.get("signals", {})
        signal_codes = []
        for code, d in signals.items():
            dates = d.get("Date", [])
            zps = d.get("ZP", d.get(OUTPUT_VAR, []))
            for dt, v in zip(dates, zps):
                if str(v) == "1":
                    if signal_start and str(dt) < signal_start:
                        continue  # 信号在回测区间之前，不拿5m数据
                    signal_codes.append(code)
                    break

        if not signal_codes:
            return result

        # 估算数据量
        from datetime import datetime
        try:
            end_dt = datetime.strptime(end_time, "%Y%m%d")
            if signal_start:
                start_dt = datetime.strptime(signal_start, "%Y%m%d")
            elif start_date:
                start_dt = datetime.strptime(start_date, "%Y%m%d")
            else:
                start_dt = end_dt.replace(day=1)
            est_days = max(1, (end_dt - start_dt).days * 0.7)
        except Exception:
            est_days = 60
        est_bars = len(signal_codes) * est_days * 48
        log.info(f"5m fetch: {len(signal_codes)} stocks x {est_days:.0f} days = {est_bars/1e3:.0f}K bars (limit {MAX_5M_BARS/1e6:.1f}M)")
        if est_bars > MAX_5M_BARS:
            log.warning(f"5m data estimate {est_bars/1e6:.1f}M > limit {MAX_5M_BARS/1e6:.1f}M, fallback daily")
            return result

        try:
            task2 = {
                "task_type": "fetch_5m",
                "stock_list_override": signal_codes,
                "start_date": start_date,
                "end_time": end_time,
            }
            log.info(f"5m fetch worker starting for {len(signal_codes)} stocks...")
            result_5m = self._run_worker(task2, timeout_multiplier=min(5, max(2, len(signal_codes) // 50)))

            bars_5m_path = result_5m.get("bars_5m_path")
            if bars_5m_path and os.path.exists(bars_5m_path):
                try:
                    import pandas as pd
                    df = pd.read_parquet(bars_5m_path)
                    result["bars_5m"] = df.to_dict(orient="records")
                    result["bars_5m_count"] = len(df)
                    log.info(f"5m data loaded: {len(df)} bars, {df['code'].nunique()} stocks, {df['datetime'].str[:10].nunique()} days")
                except Exception as e:
                    log.warning(f"5m parquet read failed: {e}")
                finally:
                    Path(bars_5m_path).unlink(missing_ok=True)
        except Exception as e:
            log.warning(f"5分钟数据获取失败，降级日线: {e}")

        return result

    def _run_worker(self, task: dict, timeout_multiplier: int = 1):
        """执行 worker 脚本并解析结果"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(task, f)
            args_path = f.name

        timeout = TIMEOUT * timeout_multiplier

        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(TDX_USER_DIR)
            env["PYTHONUNBUFFERED"] = "1"

            result = subprocess.run(
                ["python", str(WORKER_SCRIPT), "--args-file", args_path],
                cwd=str(TDX_USER_DIR),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            for line in result.stdout.strip().split("\n"):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("diag") == "worker_signals":
                    log.info(f"Worker diag: count={data['count']} first10={data['first10']}")
                    continue
                if data.get("status") == "ok":
                    return data

            return {
                "status": "error",
                "message": result.stderr or result.stdout or "No output from TDX worker",
            }
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": f"选股超时（{timeout}秒）"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        finally:
            Path(args_path).unlink(missing_ok=True)
