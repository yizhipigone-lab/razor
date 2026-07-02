"""
通达信 TDX 公式选股 API
"""
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db
from server.websocket.manager import sync_broadcast
from app.utils.threading import run_in_thread

log = get_logger("API-TQsdk")
router = APIRouter()

_stop_events = {}
_stop_lock = threading.Lock()

_BT_CONFIG_FILE = Path(__file__).resolve().parent.parent.parent / "output" / "backtest_config.json"

# WS 单次广播结果上限，超过则只推 count，前端按需拉取详情
_WS_BROADCAST_LIMIT = 200


class ScreenRequest(BaseModel):
    start_date: str = ""
    end_date: str = ""
    stock_list_override: list = None
    formula_name: str = Field(default="", max_length=100)


class BacktestRequest(BaseModel):
    history_id: int
    strategy_name: str = "盘整突破"


# ── 1. 执行选股 ──

@router.post("/api/tqsdk/screen")
async def start_screen(req: ScreenRequest):
    end_time = req.end_date
    start_time = req.start_date
    stock_list_override = req.stock_list_override
    formula_name = (req.formula_name or "").strip()

    # TDX 格式 YYYYMMDD 转 DuckDB 格式 YYYY-MM-DD
    def _fmt_date(d):
        if not d:
            return datetime.now().strftime("%Y-%m-%d")
        d = d.replace("-", "")
        if len(d) == 8:
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return d
    db_start = _fmt_date(start_time)
    db_end = _fmt_date(end_time)

    # 解析最终公式名（前端传入 > settings > QUANTQQ）
    def _resolve_formula_name():
        if formula_name:
            return formula_name
        return settings.get("tqsdk", "formula_name", default="QUANTQQ") or "QUANTQQ"

    resolved_formula = _resolve_formula_name()

    with _stop_lock:
        if "tqsdk" in _stop_events:
            return {"status": "error", "message": "已有选股任务在运行"}
        _stop_events["tqsdk"] = threading.Event()

    def _run():
        try:
            from app.tqsdk.bridge import TdxBridge, _is_signal_value
            bridge = TdxBridge()

            # ── 严格交易日历：日历不可用直接报错，不降级 ──
            from app.api.sim_trader import _load_trading_calendar
            cal = _load_trading_calendar()
            if not cal:
                sync_broadcast({
                    "type": "tqsdk_screen_done",
                    "status": "error",
                    "message": "交易日历加载失败，无法选股。请检查 baostock 连接后重试。",
                })
                return

            # 判断是单日还是区间
            is_range = bool(start_time and start_time != end_time)

            sync_broadcast({
                "type": "tqsdk_progress",
                "step": 0, "total": 3,
                "msg": (f"区间选股 {db_start} ~ {db_end}" if is_range else f"单日选股 {db_end}")
                      + f", 公式: {resolved_formula}"
            })

            stop = _stop_events.get("tqsdk")
            if stop and stop.is_set():
                sync_broadcast({"type": "tqsdk_screen_done", "status": "stopped"})
                return

            all_matched = {}  # code -> first_date

            if is_range:
                # ── 区间模式：一次 subprocess 拿整个区间信号 ──
                sd = date.fromisoformat(db_start)
                ed = date.fromisoformat(db_end)
                # 仅用于日志展示的交易日数
                scan_dates = [d for d in (sd + timedelta(i) for i in range((ed - sd).days + 1)) if d in cal]
                sync_broadcast({
                    "type": "tqsdk_progress",
                    "step": 1, "total": 3,
                    "msg": f"调用 TDX 区间扫描（{len(scan_dates)} 个交易日）..."
                })

                end_str = end_time.replace("-", "")
                start_str = start_time.replace("-", "")
                result = bridge.execute_screen_range(
                    end_time=end_str,
                    start_time=start_str,
                    kline_count=500,
                    stock_list_override=stock_list_override,
                    formula_name=formula_name,
                )

                if result.get("status") != "ok":
                    sync_broadcast({
                        "type": "tqsdk_screen_done",
                        "status": "error",
                        "message": result.get("message", "TDX 区间扫描失败"),
                    })
                    return

                # 区间结果里 signals: {code: {Date:[...], <var>:[...]}}，取每个 code 的首个信号日
                signals = result.get("signals", {})
                for code, d in signals.items():
                    dates = d.get("Date", [])
                    var_name = next((k for k in d.keys() if k != "Date"), "ZP")
                    zps = d.get(var_name, [])
                    for dt, v in zip(dates, zps):
                        if _is_signal_value(v):
                            if code not in all_matched:
                                all_matched[code] = str(dt)
                            break  # 只取首个信号日
            else:
                # ── 单日模式 ──
                d = date.fromisoformat(db_end)
                if d not in cal:
                    sync_broadcast({
                        "type": "tqsdk_screen_done",
                        "status": "error",
                        "message": f"{db_end} 非交易日",
                    })
                    return
                sync_broadcast({
                    "type": "tqsdk_progress",
                    "step": 1, "total": 3,
                    "msg": f"调用 TDX 单日扫描 {d}..."
                })
                d_str = d.strftime("%Y%m%d")
                result = bridge.execute_screen(
                    end_time=d_str,
                    stock_list_override=stock_list_override,
                    lookback_days=500,
                    formula_name=formula_name,
                )
                if result.get("status") == "ok":
                    for code in result.get("matched", []):
                        if code not in all_matched:
                            all_matched[code] = str(d)

            matched = list(all_matched.keys())
            sync_broadcast({
                "type": "tqsdk_progress",
                "step": 2, "total": 3,
                "msg": f"选股完成，共 {len(matched)} 只，正在保存..."
            })

            # 补充股票名称和板块（参数化查询，防 SQL 注入）
            stock_details = []
            if matched:
                try:
                    codes_clean = [c.split(".")[0] for c in matched]
                    placeholders = ",".join(["?"] * len(codes_clean))
                    detail_df = db.conn.execute(
                        f"SELECT code, name, sector FROM stocks WHERE code IN ({placeholders})",
                        codes_clean,
                    ).fetchdf()
                    detail_map = {}
                    for _, row in detail_df.iterrows():
                        detail_map[row["code"]] = {
                            "code": row["code"],
                            "name": row.get("name", ""),
                            "sector": row.get("sector", ""),
                        }
                    for c in matched:
                        clean = c.split(".")[0]
                        stock_details.append(detail_map.get(clean, {"code": clean, "name": "", "sector": ""}))
                except Exception as e:
                    log.warning(f"补充股票信息失败: {e}")
                    stock_details = [{"code": c.split(".")[0], "name": "", "sector": ""} for c in matched]

            # 持久化（用真实公式名，不硬编码）
            history_id = db.save_tqsdk_screen_history(
                formula_name=resolved_formula,
                formula_arg="",
                start_date=db_start,
                end_date=db_end,
                stock_count=len(matched),
                stock_codes=[c.split(".")[0] for c in matched],
                stock_details=stock_details,
            )

            # WS 广播：结果超限时只推 count，前端按需拉详情，避免大 payload 阻塞
            broadcast_results = stock_details[:_WS_BROADCAST_LIMIT]
            sync_broadcast({
                "type": "tqsdk_screen_done",
                "status": "ok",
                "result_id": history_id,
                "count": len(matched),
                "results": broadcast_results,
                "truncated": len(matched) > _WS_BROADCAST_LIMIT,
                "formula_name": resolved_formula,
            })

        except Exception as e:
            log.error(f"选股异常: {e}")
            sync_broadcast({
                "type": "tqsdk_screen_done",
                "status": "error",
                "message": str(e),
            })
        finally:
            with _stop_lock:
                _stop_events.pop("tqsdk", None)

    run_in_thread(_run)
    return {"status": "started", "formula_name": resolved_formula}


# ── 2. 停止选股 ──

@router.post("/api/tqsdk/screen/stop")
async def stop_screen():
    with _stop_lock:
        if "tqsdk" in _stop_events:
            _stop_events["tqsdk"].set()
            return {"status": "ok", "message": "已发送停止信号"}
    return {"status": "ok", "message": "无运行中的任务"}


# ── 3. 选股历史列表 ──

@router.get("/api/tqsdk/screen/history")
async def list_history(limit: int = 20, offset: int = 0):
    df = db.list_tqsdk_screen_history(limit=limit, offset=offset)
    if df.empty:
        return {"status": "ok", "data": [], "total": 0}
    records = []
    for _, row in df.iterrows():
        records.append({
            "id": int(row["id"]),
            "formula_name": row.get("formula_name", "QUANTQQ"),
            "formula_arg": row.get("formula_arg", ""),
            "start_date": str(row.get("start_date", "")),
            "end_date": str(row.get("end_date", "")),
            "stock_count": int(row.get("stock_count", 0)),
            "executed_at": str(row.get("executed_at", "")),
        })
    return {"status": "ok", "data": records, "total": len(records)}


# ── 4. 选股历史详情 ──

@router.get("/api/tqsdk/screen/history/{hist_id}")
async def get_history_detail(hist_id: int):
    detail = db.get_tqsdk_screen_history_detail(hist_id)
    if not detail:
        return {"status": "error", "message": "记录不存在"}
    return {"status": "ok", "data": detail}


# ── 5. 删除选股历史 ──

@router.delete("/api/tqsdk/screen/history/{hist_id}")
async def delete_history(hist_id: int):
    db.delete_tqsdk_screen_history(hist_id)
    return {"status": "ok", "message": "已删除"}


# ── 6. 一键回测 ──

@router.post("/api/tqsdk/backtest")
async def run_tqsdk_bt(body: dict):
    """一键回测：选股 + 轻量回测 (formula_name 可选)"""
    result = None

    def _run():
        nonlocal result
        import threading as _th
        from app.backtest.simple_runner import run_backtest
        from app.backtest.tdx_runner import run_tdx_backtest
        from datetime import date as _date

        try:
            history_id = body.get("history_id")
            params = body.get("params", body)
            formula_name = (body.get("formula_name") or "").strip()  # 新增

            strategy_type = params.get("strategy_type", "tdx")
            stock_list_override = params.get("stock_list_override")
            n_min = params.get("n_min", 100)
            n_max = params.get("n_max", 300)
            start = params.get("start_date", "2023-01-01")
            end = params.get("end_date", str(_date.today()))

            # 如果传了 formula_name，通过 settings.set 覆盖（不直接戳内部字典）
            if formula_name:
                settings.set("tqsdk", "formula_name", formula_name, save=False)
                params["strategy_name"] = formula_name  # 同时给 params 用
                log.info(f"回测公式覆盖: {formula_name}")

            if isinstance(start, str):
                start = _date.fromisoformat(start)
            if isinstance(end, str):
                end = _date.fromisoformat(end)

            sync_broadcast({
                "type": "tqsdk_progress",
                "step": 1, "total": 4, "msg": "加载股票列表..."
            })

            # 从历史记录加载股票池
            stock_list = stock_list_override
            if not stock_list and history_id:
                detail = db.get_tqsdk_screen_history_detail(int(history_id))
                if detail and detail.get("stock_codes"):
                    stock_list = detail["stock_codes"]

            if not stock_list:
                # 使用全市场
                stocks_df = db.get_all_stocks()
                stock_list = stocks_df["code"].tolist()

            sync_broadcast({
                "type": "tqsdk_progress",
                "step": 2, "total": 4,
                "msg": f"股票池 {len(stock_list)} 只，执行回测..."
            })

            stop_evt = _th.Event()

            def _prog(step, total, msg):
                sync_broadcast({
                    "type": "backtest_progress",
                    "step": step, "total": total, "msg": msg,
                    "context": "tqsdk_bt"
                })

            stock_names = {}
            try:
                placeholders = ",".join(["?"] * len(stock_list))
                df_names = db.conn.execute(
                    f"SELECT code, name FROM stocks WHERE code IN ({placeholders})",
                    stock_list,
                ).fetchdf()
                stock_names = dict(zip(df_names["code"], df_names["name"]))
            except Exception:
                pass

            result = run_backtest(
                params, progress_cb=_prog, stop_event=stop_evt,
                stock_names=stock_names, stock_pool=stock_list,
            )

            if result.get("status") == "stopped":
                sync_broadcast({"type": "log", "level": "warn", "msg": "回测已停止"})
                return

            sync_broadcast({
                "type": "simple_bt_done",
                "summary": result["summary"],
                "equity": result["equity"],
                "trades": result["trades"],
                "indices": result.get("indices", {}),
                "params": params,
                "context": "tqsdk_bt",
            })

        except Exception as e:
            log.error(f"回测异常: {e}")
            sync_broadcast({
                "type": "log", "level": "error", "msg": f"回测失败: {e}"
            })
        finally:
            # 清理：移除 BacktestEngine._intraday_cache
            try:
                from app.backtest.engine import BacktestEngine
                if hasattr(BacktestEngine, '_intraday_cache'):
                    delattr(BacktestEngine, '_intraday_cache')
            except Exception:
                pass

    run_in_thread(_run)
    return {"status": "started"}


# ── 7. 停止回测 ──

@router.post("/api/tqsdk/backtest/stop")
async def stop_tqsdk_bt():
    try:
        from app.api.backtest import stop_events
        if "simple_bt" in stop_events:
            stop_events["simple_bt"].set()
        return {"status": "ok", "message": "已发送停止信号"}
    except Exception:
        return {"status": "ok", "message": "已发送停止信号"}
