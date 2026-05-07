from fastapi import APIRouter
from datetime import date
from typing import Optional, List
from pathlib import Path
from pydantic import BaseModel
from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db
import threading
from server.websocket.manager import sync_broadcast
from app.utils.threading import run_in_thread

log = get_logger("API-Backtest")
router = APIRouter()
stop_events = {}
_stop_events_lock = threading.Lock()
ROOT_DIR = Path(__file__).resolve().parent.parent.parent

@router.get("/api/backtest/strategies")
async def list_backtest_strategies():
    """扫描物理策略文件，返回 AI 回测可用的策略列表"""
    STRAT_DIR = ROOT_DIR / "app" / "screener" / "strategies"
    EXCLUDED = {"base.py", "__init__.py"}
    result = []
    for f in sorted(STRAT_DIR.glob("*.py")):
        if f.name in EXCLUDED:
            continue
        name = f.stem
        # 读首行 docstring 作为显示名
        try:
            lines = f.read_text(encoding="utf-8").split("\n")
            desc = next((l.strip(' "\'') for l in lines[:10]
                         if l.strip().strip('"\'') and not l.startswith("#")
                         and not l.startswith("from") and not l.startswith("import")
                         and not l.startswith("class") and not l.startswith("def")
                         and len(l.strip().strip('"\'')) > 2), name)
        except Exception:
            desc = name
        result.append({"name": name, "label": desc[:40]})
    return {"status": "ok", "strategies": result}


@router.post("/api/backtest/ai/start")
async def ai_backtest_start(body: dict):
    """启动 AI 参数优化任务（异步线程，全程非阻塞）"""
    from app.backtest.ai_optimizer import get_optimizer, get_task_state, _update_state
    state = get_task_state()
    if state.get("running"):
        return {"status": "error", "message": "已有 AI 优化任务正在运行，请等待完成或先停止"}

    # 先标记 running=True，防止前端轮询在线程启动前读到 false
    _update_state(running=True, phase="loading", phase_detail="",
                  trial_current=0, trial_total=62,
                  top10=[], llm_report="", best_params=None, error=None)

    strategy_name = body.get("strategy_name", "")
    if not strategy_name:
        return {"status": "error", "message": "strategy_name 不能为空"}

    from datetime import date
    try:
        start_str = body.get("start")
        end_str   = body.get("end")
        start_date = date.fromisoformat(start_str) if start_str else (date.today() - __import__('datetime').timedelta(days=365))
        end_date   = date.fromisoformat(end_str) if end_str else date.today()
    except Exception as e:
        return {"status": "error", "message": f"日期格式错误: {e}"}

    exchanges    = body.get("exchanges", [])
    sectors      = body.get("sectors", [])
    use_llm      = body.get("use_llm", True)
    n_exploration= int(body.get("n_exploration", 12))
    n_bayesian   = int(body.get("n_bayesian", 50))
    index_filter = body.get("index_filter", [])
    min_mv       = body.get("min_mv")
    max_mv       = body.get("max_mv")
    initial_capital = body.get("initial_capital")
    position_size   = body.get("position_size")
    use_portfolio   = body.get("use_portfolio")
    streak_pause    = body.get("streak_pause")
    pause_days      = body.get("pause_days")
    intraday_freq   = body.get("intraday_freq")
    risk_params     = body.get("risk_params", {})
    use_atr_stop    = body.get("use_atr_stop")
    atr_stop_mult   = body.get("atr_stop_multiplier")
    use_hot_concept = body.get("use_hot_concept", False)
    hot_concept_top_n = body.get("hot_concept_top_n", 5)

    def _do_ai_backtest():
        try:
            from app.backtest.ai_optimizer import AIBacktestOptimizer
            optimizer = AIBacktestOptimizer(
                use_llm=use_llm,
                n_exploration=n_exploration,
                n_bayesian=n_bayesian,
            )
            # 更新全局单例
            import app.backtest.ai_optimizer as _aio
            _aio._optimizer_instance = optimizer

            def _log_cb(msg):
                sync_broadcast({"type": "log", "level": "info", "msg": msg})

            optimizer.run(
                strategy_name=strategy_name,
                strategy_params=body.get("strategy_params", {}),
                start=start_date,
                end=end_date,
                exchanges=exchanges if exchanges else None,
                sectors=sectors if sectors else None,
                index_filter=index_filter if index_filter else None,
                min_mv=min_mv,
                max_mv=max_mv,
                log_callback=_log_cb,
                initial_capital=initial_capital,
                position_size=position_size,
                use_portfolio=use_portfolio,
                streak_pause=streak_pause,
                pause_days=pause_days,
                intraday_freq=intraday_freq,
                params_override=risk_params if risk_params else None,
                use_atr_stop=use_atr_stop,
                atr_stop_multiplier=atr_stop_mult,
                use_hot_concept=use_hot_concept,
                hot_concept_top_n=hot_concept_top_n,
            )

            # 💾 自动保存 AI 回测历史
            try:
                import json as _json
                from app.backtest.ai_optimizer import get_task_state
                state = get_task_state()
                _top10 = state.get("top10", [])
                _best = state.get("best_params", {})
                _best_pnl = _top10[0].get("avg_pnl") if _top10 else None
                _best_wr  = _top10[0].get("win_rate") if _top10 else None
                _hist_id = db.save_ai_backtest_history(
                    strategy_name=strategy_name,
                    start_date=str(start_date), end_date=str(end_date),
                    exchanges=exchanges, sectors=sectors,
                    index_filter=index_filter,
                    min_mv=min_mv, max_mv=max_mv,
                    use_llm=use_llm, n_exploration=n_exploration, n_bayesian=n_bayesian,
                    best_avg_pnl=_best_pnl, best_win_rate=_best_wr,
                    best_params=_json.dumps(_best or {}, ensure_ascii=False),
                    top10_json=_json.dumps(_top10, ensure_ascii=False, default=str),
                    wfo_json=_json.dumps(state.get("wfo_results", []), ensure_ascii=False, default=str),
                    llm_report=state.get("llm_report", ""),
                    regime_summary=_json.dumps(state.get("regime_summary", {}), ensure_ascii=False)
                )
                log.info(f"AI 回测历史已保存 (id={_hist_id})")
                sync_broadcast({"type": "log", "level": "info", "msg": f"💾 AI 回测历史已自动保存 (id={_hist_id})"})
            except Exception as _e:
                log.warning(f"AI 回测历史保存失败: {_e}")

            sync_broadcast({"type": "done", "msg": "AI 回测优化任务完成"})
        except Exception as e:
            log.error(f"AI 回测线程崩溃: {e}")
            sync_broadcast({"type": "log", "level": "error", "msg": f"❌ AI 优化失败: {e}"})

    run_in_thread(_do_ai_backtest)
    return {"status": "started", "message": "AI 回测优化任务已启动"}


@router.get("/api/backtest/ai/status")
async def ai_backtest_status():
    """查询 AI 优化任务的实时状态与结果"""
    from app.backtest.ai_optimizer import get_task_state
    state = get_task_state()
    # 序列化（确保 JSON 可序列化）
    def _safe(v):
        if isinstance(v, float) and (v != v):  # NaN
            return None
        return v
    top10 = state.get("top10", [])
    # 格式化 top10 保证前端友好
    formatted = []
    for i, r in enumerate(top10):
        formatted.append({
            "rank": i + 1,
            "avg_pnl": _safe(r.get("avg_pnl")),
            "win_rate": _safe(r.get("win_rate")),
            "max_dd": _safe(r.get("max_dd")),
            "score": _safe(r.get("score")),
            "n_trades": r.get("n_trades"),
            "wfe": r.get("wfe", "N/A"),
            "wfe_status": r.get("wfe_status", ""),
            "oos_pnl": _safe(r.get("oos_pnl")),
            "params": r.get("params", {}),
        })
    return {
        "running": state.get("running"),
        "phase": state.get("phase"),
        "phase_detail": state.get("phase_detail", ""),
        "trial_current": state.get("trial_current", 0),
        "trial_total": state.get("trial_total", 0),
        "top10": formatted,
        "llm_report": state.get("llm_report", ""),
        "best_params": state.get("best_params"),
        "error": state.get("error"),
    }


@router.post("/api/backtest/ai/stop")
async def ai_backtest_stop():
    """中断正在运行的 AI 优化任务"""
    from app.backtest.ai_optimizer import stop_optimization, get_task_state
    state = get_task_state()
    if not state.get("running"):
        return {"status": "ok", "message": "当前无 AI 优化任务在运行"}
    stop_optimization()
    sync_broadcast({"type": "log", "level": "warning", "msg": "🛑 AI 优化停止指令已发送"})
    return {"status": "ok", "message": "停止信号已发送，将在当前 trial 完成后退出"}


@router.post("/api/backtest/ai/apply")
async def ai_backtest_apply(body: dict):
    """将选定的参数集写入 settings，立即生效（同时持久化），并更新搜索空间基线"""
    params = body.get("params")
    if not params:
        return {"status": "error", "message": "params 不能为空"}

    mapping = {
        "hard_stop_loss_pct":       ("risk", "hard_stop_loss_pct"),
        "breakeven_threshold_pct":  ("risk", "breakeven_threshold_pct"),
        "breakeven_stop_pnl_pct":   ("risk", "breakeven_stop_pnl_pct"),
        "trailing_activate_pct":    ("risk", "trailing_stop_activate_pct"),
        "trailing_drawdown_pct":    ("risk", "trailing_stop_drawdown_pct"),
        "time_exit_days":           ("risk", "time_exit_days"),
    }

    applied = []
    for param_key, (section, setting_key) in mapping.items():
        if param_key in params:
            settings.set(section, setting_key, params[param_key])
            applied.append(f"{param_key}={params[param_key]}")

    # 重建 staged_take_profit（支持 2 档或 3 档）
    if "tp1_profit" in params and "tp2_profit" in params:
        tp_plan = [
            {"profit_pct": params["tp1_profit"], "sell_ratio": params.get("tp1_ratio", 0.33),
             "label": "分阶止盈1"},
            {"profit_pct": params["tp2_profit"], "sell_ratio": params.get("tp2_ratio", 0.33),
             "label": "分阶止盈2"},
        ]
        if "tp3_profit" in params:
            tp_plan.append(
                {"profit_pct": params["tp3_profit"], "sell_ratio": params.get("tp3_ratio", 0.34),
                 "label": "分阶止盈3", "sell_all": True},
            )
        else:
            tp_plan[-1]["sell_all"] = True
        settings.set("risk", "staged_take_profit", tp_plan)
        applied.append(f"tp_plan=[{params['tp1_profit']}%/{params['tp2_profit']}%]")

    # 同时更新搜索空间基线（以应用后的参数为新的中心值）
    _baseline = {
        "tp1_profit": params.get("tp1_profit", 3.0),
        "tp2_profit": params.get("tp2_profit", 5.0),
        "tp1_ratio": params.get("tp1_ratio", 0.30),
        "tp2_ratio": params.get("tp2_ratio", 0.30),
        "hard_stop_loss_pct": params.get("hard_stop_loss_pct", -6.0),
        "trailing_activate_pct": params.get("trailing_activate_pct", 6.0),
        "trailing_drawdown_pct": params.get("trailing_drawdown_pct", 3.0),
        "breakeven_threshold_pct": params.get("breakeven_threshold_pct", 5.0),
        "breakeven_stop_pnl_pct": params.get("breakeven_stop_pnl_pct", 1.0),
        "time_exit_days": params.get("time_exit_days", 5),
    }
    _new_space = {}
    for k, v in _baseline.items():
        is_int = isinstance(v, int)
        lo = max(v * 0.3, v - abs(v)) if v > 0 else v * 1.5  # handle negative values
        hi = v * 2.0 if v > 0 else v * 0.5
        if lo > hi:
            lo, hi = hi, lo
        if is_int:
            lo, hi = int(lo), int(hi)
        step = 0.5 if isinstance(v, float) else 1
        _new_space[k] = {"min": round(lo, 2) if not is_int else lo,
                         "max": round(hi, 2) if not is_int else hi,
                         "step": step}
    settings.set("optimizer", "search_space", _new_space)
    applied.append("search_space (同步更新)")

    settings.save()
    log.info(f"AI 最优参数已应用: {applied}")
    return {"status": "ok", "message": f"已成功应用 {len(applied)} 项参数", "applied": applied}




class BacktestRequest(BaseModel):
    strategy_name: str
    strategy_params: dict = {}
    start: Optional[str] = None
    end: Optional[str] = None
    exchanges: Optional[List[str]] = None
    sectors: Optional[List[str]] = None
    index_filter: Optional[List[str]] = None   # e.g. ['HS300', 'ZZ500']
    min_mv: Optional[float] = None              # 流通市值下限（亿元）
    max_mv: Optional[float] = None              # 流通市值上限（亿元）
    # 资金与仓位
    initial_capital: Optional[float] = None
    position_size: Optional[float] = None
    use_portfolio: Optional[bool] = None
    streak_pause: Optional[int] = None
    pause_days: Optional[int] = None
    intraday_freq: Optional[str] = None
    # 风险参数覆盖（前端直接传递）
    risk_params: dict = {}
    use_atr_stop: Optional[bool] = None
    atr_stop_multiplier: Optional[float] = None
    # 热门概念过滤
    use_hot_concept: Optional[bool] = None
    hot_concept_top_n: Optional[int] = None

@router.post("/api/backtest")
async def run_backtest(body: BacktestRequest):
    try:
        # 为当前请求创建唯一的终止事件
        stop_event = threading.Event()
        with _stop_events_lock:
            stop_events['backtest'] = stop_event # 同一时间只允许一个回测任务存在对应的终止信号
        
        def _do_backtest():
            try:
                from app.backtest.engine import backtest_engine
                def _prog(step, total, msg):
                    sync_broadcast({"type": "progress", "step": step, "total": total, "msg": msg, "context": "backtest"})

                result = backtest_engine.run(
                    strategy_name=body.strategy_name,
                    strategy_params=body.strategy_params,
                    start=date.fromisoformat(body.start) if body.start else None,
                    end=date.fromisoformat(body.end) if body.end else None,
                    exchanges=body.exchanges,
                    sectors=body.sectors,
                    index_filter=body.index_filter,
                    min_mv=body.min_mv,
                    max_mv=body.max_mv,
                    progress_callback=_prog,
                    stop_event=stop_event,
                    initial_capital=body.initial_capital,
                    position_size=body.position_size,
                    use_portfolio=body.use_portfolio,
                    streak_pause=body.streak_pause,
                    pause_days=body.pause_days,
                    intraday_freq=body.intraday_freq,
                    params_override=body.risk_params if body.risk_params else None,
                    use_atr_stop=body.use_atr_stop if body.use_atr_stop is not None else None,
                    atr_stop_multiplier=body.atr_stop_multiplier,
                    use_hot_concept=body.use_hot_concept if body.use_hot_concept is not None else False,
                    hot_concept_top_n=body.hot_concept_top_n if body.hot_concept_top_n is not None else 5,
                )
                
                summary = {
                    "total_trades": result.total_trades,
                    "win_rate": round(result.win_rate, 1),
                    "avg_pnl_pct": round(result.total_pnl_pct, 2),
                }
                stocks = []
                for s in result.trades:
                    row = dict(s)
                    for k, v in row.items():
                        if hasattr(v, "isoformat") or hasattr(v, "strftime"): 
                            row[k] = str(v)
                    stocks.append(row)
                
                if stop_event.is_set():
                    sync_broadcast({"type": "log", "level": "warn", "msg": "🛑 回测任务已被用户中止"})
                else:
                    # 💾 自动保存回测历史
                    try:
                        import json as _json
                        _risk = settings.get("risk") or {}
                        _hist_id = db.save_backtest_history(
                            strategy_name=body.strategy_name,
                            start_date=body.start, end_date=body.end,
                            exchanges=body.exchanges, sectors=body.sectors,
                            index_filter=body.index_filter,
                            min_mv=body.min_mv, max_mv=body.max_mv,
                            risk_params=_json.dumps(_risk, ensure_ascii=False),
                            total_trades=result.total_trades,
                            win_rate=result.win_rate,
                            avg_pnl_pct=result.total_pnl_pct,
                            trades_json=_json.dumps(stocks, ensure_ascii=False),
                        )
                        log.info(f"回测历史已保存 (id={_hist_id})")
                    except Exception as _e:
                        log.warning(f"回测历史保存失败: {_e}")

                # 投资组合结果（如果有）
                portfolio = None
                if getattr(result, "portfolio_initial_capital", None):
                    portfolio = {
                        "initial_capital": result.portfolio_initial_capital,
                        "final_value": round(result.portfolio_final_value, 2),
                        "total_return": round(result.portfolio_total_return, 2),
                        "funded_trades": len(result.portfolio_trades) if result.portfolio_trades else 0,
                        "skipped": result.portfolio_skipped,
                        "monthly": getattr(result, "portfolio_monthly", None),
                    }

                sync_broadcast({"type": "backtest_done", "summary": summary, "stocks": stocks, "portfolio": portfolio})
            except Exception as e:
                import traceback
                err_msg = f"回测后台任务崩溃: {str(e)}\n{traceback.format_exc()}"
                log.error(err_msg)
                sync_broadcast({"type": "log", "level": "error", "msg": f"回测崩溃: {str(e)}"})
            finally:
                with _stop_events_lock:
                    if 'backtest' in stop_events: del stop_events['backtest']

        run_in_thread(_do_backtest)
        return {"status": "started"}
    except Exception as e:
        log.error(f"回测接口异常: {e}")
        return {"status": "error", "message": str(e)}

# ─── 任务控制 API ─────────────────────────────────────────────
@router.post("/api/tasks/stop")
async def stop_task(req: dict):
    task_type = req.get("type", "backtest")
    if task_type in stop_events:
        with _stop_events_lock:
            if task_type in stop_events:
                stop_events[task_type].set()
                log.info(f"收到用户指令，正在叫停任务: {task_type}")
                return {"status": "ok", "message": f"已向 {task_type} 引擎发送中止信号"}
    return {"status": "error", "message": "未发现正在运行的任务或无需停止"}

# ─── 指数成分股 API ──────────────────────────────────────────────



@router.get("/api/backtest/history")
async def list_backtest_history(limit: int = 20, offset: int = 0):
    df = db.list_backtest_history(limit=limit, offset=offset)
    if df.empty:
        return {"status": "ok", "data": [], "total": 0}
    df = df.astype(str)
    return {"status": "ok", "data": df.to_dict(orient="records"), "total": len(df)}


@router.get("/api/backtest/history/{hist_id}")
async def get_backtest_history_detail(hist_id: int):
    rec = db.get_backtest_history_detail(hist_id)
    if not rec:
        return {"status": "error", "message": f"记录 {hist_id} 不存在"}
    return {"status": "ok", "data": rec}


@router.delete("/api/backtest/history/{hist_id}")
async def delete_backtest_history(hist_id: int):
    db.delete_backtest_history(hist_id)
    return {"status": "ok", "message": f"已删除回测历史 {hist_id}"}


@router.get("/api/backtest/ai/history")
async def list_ai_backtest_history(limit: int = 20, offset: int = 0):
    df = db.list_ai_backtest_history(limit=limit, offset=offset)
    if df.empty:
        return {"status": "ok", "data": [], "total": 0}
    df = df.astype(str)
    return {"status": "ok", "data": df.to_dict(orient="records"), "total": len(df)}


@router.get("/api/backtest/ai/history/{hist_id}")
async def get_ai_backtest_history_detail(hist_id: int):
    rec = db.get_ai_backtest_history_detail(hist_id)
    if not rec:
        return {"status": "error", "message": f"AI 历史记录 {hist_id} 不存在"}
    return {"status": "ok", "data": rec}


@router.delete("/api/backtest/ai/history/{hist_id}")
async def delete_ai_backtest_history(hist_id: int):
    db.delete_ai_backtest_history(hist_id)
    return {"status": "ok", "message": f"已删除 AI 回测历史 {hist_id}"}


