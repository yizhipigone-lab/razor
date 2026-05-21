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
        """
        执行 QUANTXX 公式选股

        Args:
            end_time: 截止日期 YYYYMMDD
            stock_list_override: 指定股票列表，None=全A股
            lookback_days: 回看天数

        Returns:
            dict: {status: 'ok', matched: [...], total: int} 或 {status: 'error', message: str}
        """
        task = {
            "formula_name": FORMULA_NAME,
            "formula_arg": FORMULA_ARG,
            "output_var_name": OUTPUT_VAR,
            "match_value": MATCH_VALUE,
            "end_time": end_time,
            "stock_list_override": stock_list_override,
            "lookback_days": lookback_days,
        }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(task, f)
            args_path = f.name

        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(TDX_USER_DIR)

            result = subprocess.run(
                ["python", str(WORKER_SCRIPT), "--args-file", args_path],
                cwd=str(TDX_USER_DIR),
                env=env,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
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
            return {"status": "error", "message": "选股超时（10分钟）"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        finally:
            Path(args_path).unlink(missing_ok=True)
