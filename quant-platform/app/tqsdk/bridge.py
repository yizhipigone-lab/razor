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
import threading
import time
from pathlib import Path

from core.logger import get_logger
from core.settings import settings
from app.tqsdk import result_cache
log = get_logger("TdxBridge")


def _resolve_tdx_user_dir() -> Path:
    """解析 TDX 用户插件目录：环境变量 > 配置文件 > 默认值。"""
    default_dir = r"E:\NEW_TDX\PYPlugins\user"
    try:
        cfg_dir = settings.get("tqsdk", "tdx_user_dir", default=default_dir)
    except Exception:
        cfg_dir = default_dir
    raw = os.environ.get("TDX_USER_DIR") or cfg_dir or default_dir
    return Path(raw)


TDX_USER_DIR = _resolve_tdx_user_dir()
WORKER_SCRIPT = TDX_USER_DIR / "tqsdk_bridge_worker.py"


def _tdx_root() -> Path:
    """推导通达信根目录：PYPlugins/user → PYPlugins → NEW_TDX"""
    return TDX_USER_DIR.parent.parent


def _formula_fp() -> str:
    """通达信公式库文件指纹。改公式内容（即使不改名）→ 指纹变 → 缓存失效。
    读不到时返回空串（调用方应禁用缓存以保证正确性）。"""
    try:
        return result_cache.formula_fingerprint(_tdx_root())
    except Exception as e:
        log.debug(f"公式指纹读取失败: {e}")
        return ""

# 仓库内 worker 源（唯一真源，进 git）
WORKER_SOURCE = Path(__file__).parent / "worker" / "tqsdk_bridge_worker.py"

# 陈旧临时 parquet 文件保留时长（小时），超过则在部署时清理
_STALE_TEMP_HOURS = 24


def _cleanup_stale_temp_parquets():
    """清理 temp 目录下超过 _STALE_TEMP_HOURS 的 tdx_intra_*.parquet 残留。"""
    try:
        tmp = Path(tempfile.gettempdir())
        import time as _t
        now = _t.time()
        for p in tmp.glob("tdx_intra_*.parquet"):
            try:
                age_h = (now - p.stat().st_mtime) / 3600
                if age_h > _STALE_TEMP_HOURS:
                    p.unlink(missing_ok=True)
                    log.info(f"[temp清理] 删除陈旧文件 {p.name} (age {age_h:.1f}h)")
            except Exception:
                pass
    except Exception as e:
        log.debug(f"[temp清理] 跳过: {e}")


def _ensure_worker_deployed():
    """确保 TDX 目录的 worker 与仓库内源文件一致（hash 不一致直接覆盖）。"""
    try:
        if not WORKER_SOURCE.exists():
            return  # 仓库内没有 worker，跳过
        if not TDX_USER_DIR.exists():
            log.warning(f"[worker 部署] TDX 目录不存在: {TDX_USER_DIR}，通达信选股功能不可用")
            return
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


def _is_signal_value(value_str) -> bool:
    """非零即视为信号 (兼容 1/100/0.5/任意非零)

    注意: 此函数与 worker/tqsdk_bridge_worker.py 中的 _is_signal_value 必须保持逻辑一致。
    worker 端运行在 TDX 子进程内无法 import 本模块，故重复定义（DRY 让位于进程隔离）。
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


# 模块加载时清理一次陈旧临时文件（部署改为实例化时触发，避免 import 副作用）
_cleanup_stale_temp_parquets()

def _get_formula_name(override: str = None) -> str:
    """获取公式名。优先级: override > settings > QUANTQQ"""
    if override and override.strip():
        return override.strip()
    try:
        name = settings.get("tqsdk", "formula_name", default="QUANTQQ")
        return name if name else "QUANTQQ"
    except Exception:
        return "QUANTQQ"

OUTPUT_VAR = "ZP"
TIMEOUT = 600  # 10分钟
MATCH_VALUE = ""  # 默认匹配值（空字符串=不限制）
# subprocess 内存上限（字节），None 表示不限。Linux 下通过 preexec_fn 生效。
WORKER_MEM_LIMIT_BYTES = 2 * 1024 ** 3  # 2GB
# 后台读线程 join 超时(秒): 留足时间 json.loads 巨大 status==ok 行(数据量增长时 100MB+ JSON)
READER_JOIN_TIMEOUT = 120


class TdxBridge:
    """通达信公式选股桥接器"""

    def __init__(self):
        # 实例化时部署 worker（避免 import 即写外部文件系统的副作用）
        _ensure_worker_deployed()

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
                              formula_name: str = None,
                              use_cache: bool = True,
                              progress_cb=None, stop_event=None):
        """
        区间选股：返回信号 + 价格

        数据源仍是 TDX。use_cache=True 时，按 (公式名+区间+K线数) 哈希缓存
        TDX 返回的 signals/prices 到本地 parquet，命中则跳过 subprocess。
        同一公式同一区间的选股结果是确定函数，可安全缓存；换公式/区间自动失效。

        Returns:
            {status: 'ok', signals: {code: {ZP: [...], Date: [...]}},
                          prices: {code: {Close: [...], Date: [...]}},
                          parquet_path: str (缓存文件路径，供向量化解析用),
                          cache_hit: bool}
        """
        if return_count is None:
            return_count = kline_count
        fname = _get_formula_name(formula_name)

        # ── 公式指纹：改公式内容（即使不改名）会让指纹变 → 缓存自动失效 ──
        # 读不到公式库文件时禁用缓存，避免改公式后命中旧缓存返回错误结果
        fp = _formula_fp()
        if use_cache and not fp:
            log.warning("无法读取通达信公式库指纹，本次禁用缓存（避免改公式后命中旧缓存）")
            use_cache = False

        # ── A: 缓存命中 → 跳过 subprocess ──
        if use_cache:
            cached = result_cache.get_cache(
                fname, start_time, end_time, kline_count,
                return_count, stock_list_override, formula_fp=fp)
            if cached is not None:
                try:
                    import pandas as pd
                    df = pd.read_parquet(cached)
                    signals, prices = result_cache.df_to_signals_prices(df)
                    total = int(df["code"].nunique()) if not df.empty else 0
                    log.info(f"缓存命中: {cached.name} ({len(df)}行, {total}只) 跳过 subprocess")
                    return {
                        "status": "ok", "signals": signals, "prices": prices,
                        "total": total, "parquet_path": str(cached),
                        "cache_hit": True,
                    }
                except Exception as e:
                    log.warning(f"缓存读取失败，回退 worker: {e}")

        # ── 未命中：subprocess 调 worker ──
        task = {
            "task_type": "range",
            "formula_name": fname,
            "formula_arg": "",
            "output_var_name": OUTPUT_VAR,
            "end_time": end_time,
            "start_time": start_time,
            "stock_list_override": stock_list_override,
            "kline_count": kline_count,
            "return_count": return_count,
        }
        result = self._run_worker(task, timeout_multiplier=max(2, kline_count // 50),
                                  progress_cb=progress_cb, stop_event=stop_event)

        if result.get("status") != "ok":
            return result

        # ── A: 写缓存（worker 返回的 dict → parquet 副本）──
        if use_cache and result.get("signals"):
            try:
                cache_path = result_cache.save_cache_from_dict(
                    result["signals"], result.get("prices", {}),
                    fname, start_time, end_time,
                    kline_count, return_count, stock_list_override, formula_fp=fp)
                result["parquet_path"] = str(cache_path)
                log.info(f"缓存写入: {cache_path.name}")
            except Exception as e:
                log.warning(f"缓存写入失败（不影响本次回测）: {e}")

        return result

    def execute_screen_range_intraday(self, end_time: str, kline_count: int,
                                       start_date: str = None,
                                       return_count: int = None,
                                       stock_list_override: list = None,
                                       start_time: str = "",
                                       signal_start: str = "",
                                       period: str = "5m",
                                       formula_name: str = None,
                                       progress_cb=None, stop_event=None):
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

        # ── Step 1: 获取信号 + 日线收盘价（走 execute_screen_range 享受缓存）──
        result = self.execute_screen_range(
            end_time=end_time, kline_count=kline_count, return_count=return_count,
            stock_list_override=stock_list_override, start_time=start_time,
            formula_name=formula_name,
            progress_cb=progress_cb, stop_event=stop_event)
        result["bars_intraday"] = None

        if result.get("status") != "ok":
            return result

        # ── Step 2: 获取 5 分钟 OHLC（仅对信号股，且信号在回测区间内） ──
        signals = result.get("signals", {})
        signal_codes = []
        for code, d in signals.items():
            dates = d.get("Date", [])
            # 探测变量名 (兼容 ZP/ZT/中文/任意)
            var_name = next((k for k in d.keys() if k != "Date"), "ZP")
            zps = d.get(var_name, [])
            for dt, v in zip(dates, zps):
                if _is_signal_value(v):
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
            result_intraday = self._run_worker(task2, timeout_multiplier=min(5, max(2, len(signal_codes) // 50)),
                                               progress_cb=progress_cb, stop_event=stop_event)

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

    def _run_worker(self, task: dict, timeout_multiplier: int = 1,
                    progress_cb=None, stop_event=None):
        """执行 worker 脚本并解析结果(实时转发 worker 进度到 progress_cb)。

        subprocess.run 改 Popen + 后台线程逐行读 stdout, 让 worker 的 progress 行
        能实时转发前端; 主线程轮询 proc.wait(0.5) 检查超时/stop_event, 避免 readline
        阻塞导致超时失效(回归)。
        """
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(task, f)
            args_path = f.name

        timeout = TIMEOUT * timeout_multiplier

        # 资源限制：Linux 用 preexec_fn 限制内存；Windows 暂不支持（subprocess 无原生内存限制）
        preexec = None
        if os.name == "posix":
            def _limit_resources():
                import resource as _r
                _r.setrlimit(_r.RLIMIT_AS, (WORKER_MEM_LIMIT_BYTES, WORKER_MEM_LIMIT_BYTES))
            preexec = _limit_resources

        proc = None
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(TDX_USER_DIR)
            env["PYTHONUNBUFFERED"] = "1"

            proc = subprocess.Popen(
                ["python", str(WORKER_SCRIPT), "--args-file", args_path],
                cwd=str(TDX_USER_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                preexec_fn=preexec,  # None on Windows
            )

            # 后台线程逐行读 stdout: 解析 progress/diag/status, 转发进度
            result_holder = {"ok": None}
            stderr_chunks = []  # 后台线程实时收 stderr, 防 64KB PIPE buffer 满阻塞 worker

            def _read_stdout():
                try:
                    for line in proc.stdout:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if data.get("progress"):
                            log.debug(f"worker 进度: {data.get('msg', '')}")
                            if progress_cb:
                                try:
                                    progress_cb(data.get("done", 0), data.get("total", 0),
                                                data.get("msg", ""))
                                except Exception as e:
                                    log.warning(f"progress_cb 异常: {e}")
                            continue
                        if data.get("diag") == "worker_signals":
                            log.info(f"Worker diag: batch={data.get('batch')} "
                                     f"stock_count={data.get('stock_count')} "
                                     f"first_stock={data.get('first_stock')}")
                            continue
                        if data.get("status") == "ok":
                            result_holder["ok"] = data
                except Exception as e:
                    log.warning(f"读 worker stdout 异常: {e}")

            def _read_stderr():
                # 实时读 stderr 清空 PIPE buffer, 防 worker 因 stderr 64KB 满而阻塞
                try:
                    for line in proc.stderr:
                        stderr_chunks.append(line)
                except Exception:
                    pass

            reader = threading.Thread(target=_read_stdout, daemon=True)
            reader.start()
            stderr_reader = threading.Thread(target=_read_stderr, daemon=True)
            stderr_reader.start()

            # 主线程轮询等进程结束, 检查超时 + stop_event (readline 阻塞在线程, 不影响超时判断)
            deadline = time.time() + timeout
            stopped_by_user = False
            while True:
                if stop_event is not None and stop_event.is_set():
                    stopped_by_user = True
                    break
                try:
                    proc.wait(timeout=0.5)
                    break  # 进程结束
                except subprocess.TimeoutExpired:
                    if time.time() > deadline:
                        break  # 超时

            # 进程还活着 → kill (超时或用户取消)
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass

            reader.join(timeout=READER_JOIN_TIMEOUT)  # 留足时间 json.loads 巨大 status==ok 行
            stderr_reader.join(timeout=5)
            stderr_text = "".join(stderr_chunks)

            if stopped_by_user:
                return {"status": "stopped", "message": "用户取消回测"}
            if result_holder["ok"] is not None:
                return result_holder["ok"]
            return {
                "status": "error",
                "message": stderr_text or "No output from TDX worker",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            Path(args_path).unlink(missing_ok=True)
