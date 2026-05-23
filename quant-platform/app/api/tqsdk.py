"""
通达信 QUANTQQ 公式选股 API
"""
import json
import threading
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from core.logger import get_logger
from database.duckdb_manager import db
from server.websocket.manager import sync_broadcast
from app.utils.threading import run_in_thread

log = get_logger("API-TQsdk")
router = APIRouter()

_stop_events = {}
_stop_lock = threading.Lock()

_BT_CONFIG_FILE = Path(__file__).resolve().parent.parent.parent / "output" / "backtest_config.json"


class ScreenRequest(BaseModel):
    start_date: str = ""
    end_date: str = ""
    stock_list_override: list = None


class BacktestRequest(BaseModel):
    history_id: int
    strategy_name: str = "盘整突破"


# ── 1. 执行选股 ──

@router.post("/api/tqsdk/screen")
async def start_screen(body: dict):
    end_time = body.get("end_date", "")
    start_time = body.get("start_date", "")
    stock_list_override = body.get("stock_list_override")

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

    with _stop_lock:
        if "tqsdk" in _stop_events:
            return {"status": "error", "message": "已有选股任务在运行"}
        _stop_events["tqsdk"] = threading.Event()

    def _run():
        try:
            sync_broadcast({
                "type": "tqsdk_progress",
                "step": 1, "total": 3, "msg": "正在执行 QUANTQQ 选股..."
            })

            from app.tqsdk.bridge import TdxBridge
            bridge = TdxBridge()
            result = bridge.execute_screen(
                end_time=end_time or datetime.now().strftime("%Y%m%d"),
                stock_list_override=stock_list_override,
                lookback_days=500,
            )

            stop = _stop_events.get("tqsdk")
            if stop and stop.is_set():
                sync_broadcast({"type": "tqsdk_screen_done", "status": "stopped"})
                return

            if result.get("status") != "ok":
                sync_broadcast({
                    "type": "tqsdk_screen_done",
                    "status": "error",
                    "message": result.get("message", "选股失败"),
                })
                return

            matched = result.get("matched", [])
            sync_broadcast({
                "type": "tqsdk_progress",
                "step": 2, "total": 3, "msg": f"选股完成，共 {len(matched)} 只，正在保存..."
            })

            # 补充股票名称和板块
            stock_details = []
            if matched:
                try:
                    detail_df = db.conn.execute(
                        "SELECT code, name, sector FROM stocks WHERE code IN ({})".format(
                            ",".join(f"'{c.split('.')[0]}'" for c in matched)
                        )
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

            # 持久化
            history_id = db.save_tqsdk_screen_history(
                formula_name="QUANTQQ",
                formula_arg="",
                start_date=db_start,
                end_date=db_end,
                stock_count=len(matched),
                stock_codes=[c.split(".")[0] for c in matched],
                stock_details=stock_details,
            )

            sync_broadcast({
                "type": "tqsdk_screen_done",
                "status": "ok",
                "result_id": history_id,
                "count": len(matched),
                "results": stock_details,
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
    return {"status": "started"}


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
async def start_backtest(body: dict):
    """从选股历史直接启动回测"""
    from app.backtest.simple_runner import run_backtest

    history_id = body.get("history_id")
    if not history_id:
        return {"status": "error", "message": "history_id 不能为空"}

    # 获取选股结果
    detail = db.get_tqsdk_screen_history_detail(history_id)
    if not detail:
        return {"status": "error", "message": "选股记录不存在"}

    stock_list = body.get("stock_codes") or detail.get("stock_codes", [])
    if not stock_list:
        return {"status": "error", "message": "该选股结果为空，无法回测"}

    # 加载默认回测参数
    params = _load_bt_defaults()
    params["strategy_name"] = body.get("strategy_name", "盘整突破")
    params["start_date"] = body.get("start_date") or detail.get("start_date") or "2023-01-01"
    params["end_date"] = body.get("end_date") or detail.get("end_date") or str(date.today())
    params["stock_pool"] = stock_list

    stop_evt = threading.Event()
    with _stop_lock:
        _stop_events["tqsdk_bt"] = stop_evt

    def _run():
        try:
            def _prog(step, total, msg):
                sync_broadcast({
                    "type": "backtest_progress",
                    "step": step, "total": total, "msg": msg,
                    "context": "tqsdk_bt",
                })

            stock_names = {}
            try:
                df_names = db.conn.execute(
                    "SELECT code, name FROM stocks WHERE code IN ({})".format(
                        ",".join(f"'{c}'" for c in stock_list)
                    )
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
                "type": "simple_bt_done",
                "status": "error",
                "message": str(e),
            })
        finally:
            with _stop_lock:
                _stop_events.pop("tqsdk_bt", None)

    run_in_thread(_run)
    return {"status": "started"}


def _load_bt_defaults() -> dict:
    """加载回测默认参数"""
    try:
        if _BT_CONFIG_FILE.exists():
            with open(_BT_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {
        "strategy_name": "盘整突破",
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "hard_stop": -0.06,
        "take_profit_tiers": [{"profit_pct": 0.03, "sell_ratio": 0.10}],
        "use_atr_trail": False,
    }
