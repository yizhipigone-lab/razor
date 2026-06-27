"""
TDX 公式选股桥接层
通过 subprocess 调用 TDX PYPlugins 目录下的 worker 脚本执行公式选股
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from core.logger import get_logger
log = get_logger("TdxBridge")

TDX_USER_DIR = Path(r"E:\NEW_TDX\PYPlugins\user")
WORKER_SCRIPT = TDX_USER_DIR / "tqsdk_bridge_worker.py"

# 仓库内 worker 源（唯一真源，进 git）
WORKER_SOURCE = Path(__file__).parent / "worker" / "tqsdk_bridge_worker.py"


def _ensure_worker_deployed():
    """确保 TDX 目录的 worker 与仓库内源文件一致（hash 不一致直接覆盖）。"""
    try:
        if not WORKER_SOURCE.exists():
            return  # 仓库内没有 worker，跳过
        source_hash = hashlib.md5(WORKER_SOURCE.read_bytes()).hexdigest()
        if WORKER_SCRIPT.exists():
            target_hash = hashlib.md5(WORKER_SCRIPT.read_bytes()).hexdigest()
            if source_hash == target_hash:
                return  # 已一致，无需操作
        # 部署
        WORKER_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(WORKER_SOURCE, WORKER_SCRIPT)
        log.info(f"[worker 部署] 已更新 {WORKER_SCRIPT.name} 到 TDX 目录")
    except Exception as e:
        log.warning(f"[worker 部署] 失败: {e}")


# 模块加载时自动部署一次（确保后续 _run_worker 用到的是新版本）
_ensure_worker_deployed()

def _get_formula_name(override: str = None) -> str:
    """获取公式名。优先级: override > settings > QUANTQQ"""
    if override and override.strip():
        return override.strip()
    try:
        from core.settings import settings
        name = settings.get("tqsdk", "formula_name", default="QUANTQQ")
        return name if name else "QUANTQQ"
    except Exception:
        return "QUANTQQ"

OUTPUT_VAR = "ZP"
MATCH_VALUE = "1"
TIMEOUT = 600  # 10分钟


class TdxBridge:
    """通达信公式选股桥接器"""

    def execute_screen(self, end_time: str, stock_list_override: list = None,
                       lookback_days: int = 30, formula_name: str = None):
        """单日选股：返回当天 ZP=1 的股票列表"""
        task = {
            "formula_name": _get_formula_name(formula_name),
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
                              start_time: str = "",
                              formula_name: str = None):
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
            "formula_name": _get_formula_name(formula_name),
            "formula_arg": "",
            "output_var_name": OUTPUT_VAR,
            "end_time": end_time,
            "start_time": start_time,
            "stock_list_override": stock_list_override,
            "kline_count": kline_count,
            "return_count": return_count,
        }
        return self._run_worker(task, timeout_multiplier=max(2, kline_count // 50))

    def execute_screen_range_intraday(self, end_time: str, kline_count: int,
                                       start_date: str = None,
                                       return_count: int = None,
                                       stock_list_override: list = None,
                                       start_time: str = "",
                                       signal_start: str = "",
                                       period: str = "5m",
                                       formula_name: str = None):
        """
        区间选股 + 日内K线增强版：两步调用 worker
        Step 1: range → 信号 + 日线收盘价 (快)
        Step 2: fetch_intraday → 仅对信号股获取日内 OHLC (限 300 只)
        失败自动降级，bars_intraday 为 None 时走日线回退
        period: '5m' 或 '1m'
        """
        bars_per_day = 48 if period == "5m" else 241
        MAX_5M_BARS = 50_000_000  # 最多5000万根K线（写到磁盘，不限内存）

        if return_count is None:
            return_count = kline_count

        # ── Step 1: 获取信号 + 日线收盘价 ──────────────────
        task1 = {
            "task_type": "range",
            "formula_name": _get_formula_name(formula_name),
            "formula_arg": "",
            "output_var_name": OUTPUT_VAR,
            "end_time": end_time,
            "start_time": start_time,
            "stock_list_override": stock_list_override,
            "kline_count": kline_count,
            "return_count": return_count,
        }
        result = self._run_worker(task1, timeout_multiplier=min(10, max(2, kline_count // 50)))
        result["bars_intraday"] = None

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
        est_bars = len(signal_codes) * est_days * bars_per_day
        log.info(f"{period} fetch: {len(signal_codes)} stocks x {est_days:.0f} days = {est_bars/1e3:.0f}K bars (limit {MAX_5M_BARS/1e6:.1f}M)")
        if est_bars > MAX_5M_BARS:
            log.warning(f"{period} data estimate {est_bars/1e6:.1f}M > limit {MAX_5M_BARS/1e6:.1f}M, fallback daily")
            return result

        try:
            task2 = {
                "task_type": "fetch_intraday",
                "period": period,
                "stock_list_override": signal_codes,
                "start_date": start_date,
                "end_time": end_time,
            }
            log.info(f"{period} fetch worker starting for {len(signal_codes)} stocks...")
            result_intraday = self._run_worker(task2, timeout_multiplier=min(5, max(2, len(signal_codes) // 50)))

            bars_path = result_intraday.get("bars_path")
            if bars_path and os.path.exists(bars_path):
                try:
                    import pandas as pd
                    df = pd.read_parquet(bars_path)
                    result["bars_intraday"] = df.to_dict(orient="records")
                    result["bars_intraday_count"] = len(df)
                    log.info(f"{period} data loaded: {len(df)} bars, {df['code'].nunique()} stocks, {df['datetime'].str[:10].nunique()} days")
                except Exception as e:
                    log.warning(f"{period} parquet read failed: {e}")
                finally:
                    Path(bars_path).unlink(missing_ok=True)
        except Exception as e:
            log.warning(f"{period}数据获取失败，降级日线: {e}")

        return result

    def execute_screen_range_5m(self, end_time: str, kline_count: int,
                                 start_date: str = None, return_count: int = None,
                                 stock_list_override: list = None, start_time: str = "",
                                 signal_start: str = ""):
        """[兼容] 等价于 execute_screen_range_intraday(period='5m')"""
        r = self.execute_screen_range_intraday(
            end_time=end_time, kline_count=kline_count, start_date=start_date,
            return_count=return_count, stock_list_override=stock_list_override,
            start_time=start_time, signal_start=signal_start, period="5m")
        if "bars_intraday" in r:
            r["bars_5m"] = r.pop("bars_intraday")
        return r

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
