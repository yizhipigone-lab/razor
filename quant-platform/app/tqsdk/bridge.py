"""
TDX 公式选股桥接层
通过 subprocess 调用 TDX PYPlugins 目录下的 worker 脚本执行公式选股
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

TDX_USER_DIR = Path(r"E:\NEW_TDX\PYPlugins\user")
WORKER_SCRIPT = TDX_USER_DIR / "tqsdk_bridge_worker.py"
FORMULA_NAME = "QUANTQQ"
FORMULA_ARG = ""
OUTPUT_VAR = "ZP"
MATCH_VALUE = "1"
TIMEOUT = 600  # 10分钟


class TdxBridge:
    """通达信公式选股桥接器"""

    def execute_screen(self, end_time: str, stock_list_override: list = None,
                       lookback_days: int = 30):
        """单日选股：返回当天 ZP=1 的股票列表"""
        task = {
            "formula_name": FORMULA_NAME,
            "formula_arg": FORMULA_ARG,
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
                              stock_list_override: list = None):
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
            "formula_name": FORMULA_NAME,
            "formula_arg": FORMULA_ARG,
            "output_var_name": OUTPUT_VAR,
            "end_time": end_time,
            "stock_list_override": stock_list_override,
            "kline_count": kline_count,
            "return_count": return_count,
        }
        return self._run_worker(task, timeout_multiplier=max(2, kline_count // 50))

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
                    if data.get("status") == "ok":
                        return data
                except json.JSONDecodeError:
                    continue

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
