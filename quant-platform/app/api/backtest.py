import os
from fastapi import APIRouter
from datetime import date, datetime, timedelta
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
        start_date = date.fromisoformat(start_str) if start_str else (date.today() - timedelta(days=365))
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
    strategy_type = body.get("strategy_type", "python")   # "python" | "tdx"
    formula_name = body.get("formula_name", "")

    # TDX 模式：公式名必填
    if strategy_type == "tdx" and not formula_name.strip():
        _update_state(running=False, phase="idle")
        return {"status": "error", "message": "TDX 模式下通达信公式名不能为空"}

    def _do_ai_backtest():
        try:
            from app.backtest.ai_optimizer import AIBacktestOptimizer
            optimizer = AIBacktestOptimizer(
                use_llm=use_llm,
                n_exploration=n_exploration,
                n_bayesian=n_bayesian,
                strategy_type=strategy_type,
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
                strategy_type=strategy_type,
                formula_name=formula_name,
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
    # P0-1 单位约定: take_profit_tiers.profit_pct 统一为小数(0.03=3%)。
    # 本端点 params 来自 best_params，tp*_profit 与 search_space 同量纲=百分比(如 3.0 或 0.83)，
    # 无条件 /100 转小数。不可用 "v>1才除" 猜测——search_space 允许 0.83% 这种 <1 的百分比值。
    def _to_decimal_pct(v):
        return float(v) / 100.0
    if "tp1_profit" in params and "tp2_profit" in params:
        tp_plan = [
            {"profit_pct": _to_decimal_pct(params["tp1_profit"]), "sell_ratio": params.get("tp1_ratio", 0.33),
             "label": "分阶止盈1"},
            {"profit_pct": _to_decimal_pct(params["tp2_profit"]), "sell_ratio": params.get("tp2_ratio", 0.33),
             "label": "分阶止盈2"},
        ]
        if "tp3_profit" in params:
            tp_plan.append(
                {"profit_pct": _to_decimal_pct(params["tp3_profit"]), "sell_ratio": params.get("tp3_ratio", 0.34),
                 "label": "分阶止盈3", "sell_all": True},
            )
        else:
            tp_plan[-1]["sell_all"] = True
        settings.set("risk", "take_profit_tiers", tp_plan)
        applied.append(f"tp_plan=[{params['tp1_profit']}%/{params['tp2_profit']}%]")

    # 同时更新搜索空间基线（以应用后的参数为新的中心值）
    # L21 修复: 缺键时从 schema (唯一真相源) 读,不再硬编码假默认
    from app.config.risk_params import load_risk_params
    _risk = load_risk_params()
    _baseline = {
        "tp1_profit": params.get("tp1_profit", 3.0),
        "tp2_profit": params.get("tp2_profit", 5.0),
        "tp1_ratio": params.get("tp1_ratio", 0.30),
        "tp2_ratio": params.get("tp2_ratio", 0.30),
        "hard_stop_loss_pct": params.get("hard_stop_loss_pct", _risk.hard_stop * 100),
        "trailing_activate_pct": params.get("trailing_activate_pct", _risk.trail_activate * 100),
        "trailing_drawdown_pct": params.get("trailing_drawdown_pct", _risk.trail_dd * 100),
        "breakeven_threshold_pct": params.get("breakeven_threshold_pct", _risk.breakeven_threshold_pct * 100),
        "breakeven_stop_pnl_pct": params.get("breakeven_stop_pnl_pct", _risk.breakeven_stop_pnl_pct * 100),
        "time_exit_days": params.get("time_exit_days", _risk.time_exit_days),
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
        # §3.3: step 按参数类型选取。ratio 类(比例,0~1)用 0.05 且 clip≤1.0; pct 类 0.5; 整数 1
        if is_int:
            step = 1
        elif k.endswith("_ratio"):
            step = 0.05
            hi = min(hi, 1.0)
            lo = min(lo, hi)
        else:
            step = 0.5
        _new_space[k] = {"min": round(lo, 2) if not is_int else lo,
                         "max": round(hi, 2) if not is_int else hi,
                         "step": step}
    settings.set("optimizer", "search_space", _new_space)
    applied.append("search_space (同步更新)")

    settings.save()
    log.info(f"AI 最优参数已应用: {applied}")
    return {"status": "ok", "message": f"已成功应用 {len(applied)} 项参数", "applied": applied}





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


# ═══════════════════════════════════════════════════════════
# 简化回测 API（日线收盘价，纯 parquet，无 DuckDB）
# ═══════════════════════════════════════════════════════════

import copy
import json as _json
from pathlib import Path as _Path

_BT_CONFIG_FILE = ROOT_DIR / "output" / "backtest_config.json"
_BT_RESULTS_DIR = ROOT_DIR / "output" / "backtest_results"
_BT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def _default_bt_config() -> dict:
    """系统默认回测配置。

    以 sim_trader.config 硬编码常量为底，再用「系统设置」页保存到
    app_setting.json 的最新风控参数覆盖存在的字段。
    这样「系统配置」按钮拉取到的值能真实反映用户在系统设置页的保存，
    重启后也不会回退到硬编码默认（修复历史上「系统配置形同虚设」的问题）。
    """
    from app.sim_trader.config import (
        INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
        HARD_STOP, TAKE_PROFIT_TIERS,
        TRAIL_ACTIVATE, TRAIL_DD,
        TIME_EXIT_DAYS, TIME_EXIT_PROFIT, TIME_FORCE_DAYS,
        LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE, PAUSE_DAYS,
        SAME_STOCK_COOLDOWN, SIGNAL_PARAMS, STRATEGY_NAME,
        USE_ATR_TRAIL, ATR_TRAIL_MULTIPLIER,
        FIRST_DAY_EXIT_MIN_PROFIT,
        FIRST_DAY_EXIT_DAYS,
    )
    cfg = {
        "strategy_name": STRATEGY_NAME,
        "initial_capital": INITIAL_CAPITAL,
        "position_size": POSITION_SIZE,
        "min_buy_amt": MIN_BUY_AMT,
        "hard_stop": HARD_STOP,
        "take_profit_tiers": copy.deepcopy(TAKE_PROFIT_TIERS),
        "trail_activate": TRAIL_ACTIVATE,
        "trail_dd": TRAIL_DD,
        "time_exit_days": TIME_EXIT_DAYS,
        "time_exit_profit": TIME_EXIT_PROFIT,
        "time_force_days": TIME_FORCE_DAYS,
        "loss_streak_halve": LOSS_STREAK_HALVE,
        "loss_streak_pause": LOSS_STREAK_PAUSE,
        "pause_days": PAUSE_DAYS,
        "same_stock_cooldown": SAME_STOCK_COOLDOWN,
        "use_atr_trail": USE_ATR_TRAIL,
        "atr_trail_multiplier": ATR_TRAIL_MULTIPLIER,
        "first_day_exit_min_profit": FIRST_DAY_EXIT_MIN_PROFIT,
        "first_day_exit_days": FIRST_DAY_EXIT_DAYS,
        "signal_params": copy.deepcopy(SIGNAL_PARAMS),
        "start_date": "2023-01-01",
        "end_date": str(date.today()),
    }
    # 用系统设置页保存的最新值覆盖（来源: app_setting.json 的 backtest 段，
    # 由 /api/settings/risk-params 与 /api/backtest/apply-to-system 写入）
    # H7(2026-07-15 全项目审计): 风险键(hard_stop/trail_*/take_profit_tiers/time_exit*/
    # first_day_exit*/use_atr*)不再从 backtest 段覆盖——它们已由 config.py 从 risk 段
    # 单源派生(H6), backtest 段重复覆盖会造成"前端改风险档回测仍用旧档"的漂移。
    # 此处只覆盖回测专属键(资金/仓位/连亏/冷却)。
    try:
        from core.settings import settings
        settings.reload()
        bt = settings.get("backtest", default={}) or {}
        _OVERRIDE_KEYS = (
            "initial_capital", "position_size", "min_buy_amt",
            "same_stock_cooldown", "loss_streak_halve", "loss_streak_pause",
            "pause_days",
        )
        for k in _OVERRIDE_KEYS:
            if k in bt and bt[k] is not None:
                cfg[k] = bt[k]
    except Exception as e:
        log.warning(f"读取系统设置回测默认值失败，回退硬编码默认: {e}")
    return cfg


def _load_bt_config() -> dict:
    try:
        if _BT_CONFIG_FILE.exists():
            with open(_BT_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return _json.load(f)
    except Exception:
        pass
    cfg = _default_bt_config()
    _save_bt_config(cfg)
    return cfg


def _save_bt_config(cfg: dict):
    _BT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_BT_CONFIG_FILE, 'w', encoding='utf-8') as f:
        _json.dump(cfg, f, ensure_ascii=False, indent=2)


@router.get("/api/backtest/simple-config")
async def get_simple_bt_config():
    """获取回测配置 — 核心交易参数始终读取系统实时配置"""
    cfg = _load_bt_config()
    sys_cfg = _default_bt_config()
    # 核心参数强制从系统配置刷新，不读缓存
    # 回测配置独立，不从系统配置覆写（用户保存的值优先）
    for k in ['end_date']:
        if k in sys_cfg:
            cfg[k] = sys_cfg[k]
    cfg['end_date'] = str(date.today())
    return {"status": "ok", "config": cfg, "system_config": sys_cfg}


@router.post("/api/backtest/simple-config")
async def save_simple_bt_config(body: dict):
    """保存回测配置（独立于系统交易配置）"""
    cfg = body.get("config", body)
    _save_bt_config(cfg)
    log.info("回测配置已保存")
    return {"status": "ok", "message": "回测配置已保存"}


@router.post("/api/backtest/simple-config/reset")
async def reset_simple_bt_config():
    """重置回测配置为系统默认值（仅在用户显式点「重置」时调用，会落盘覆盖）"""
    cfg = _default_bt_config()
    _save_bt_config(cfg)
    return {"status": "ok", "message": "已重置为系统配置", "config": cfg}


@router.post("/api/backtest/apply-to-system")
async def apply_bt_to_system():
    """将当前回测配置应用到系统交易配置（只写 settings，v5.5 不再写 module 变量）"""
    cfg = _load_bt_config()
    try:
        # 2026-07-14 v5.5: 不再写 app.sim_trader.config 模块变量，交易逻辑统一走 risk_params → settings
        # 直接从 cfg 读值写入 settings（不再经由 sc.HARD_STOP 中转）
        from core.settings import settings
        settings.reload()
        risk = settings._data.get('risk', {})
        # 止盈止损核心参数：cfg(backtest 段)是小数约定，risk 段是百分数约定，写回需 *100。
        risk['hard_stop_loss_pct']       = float(cfg.get('hard_stop', -0.06)) * 100.0
        risk['trailing_stop_activate_pct'] = float(cfg.get('trail_activate', 0.05)) * 100.0
        risk['trailing_stop_drawdown_pct'] = float(cfg.get('trail_dd', 0.02)) * 100.0
        risk['time_exit_days']            = int(cfg.get('time_exit_days', 7))
        risk['time_exit_min_profit_pct']  = float(cfg.get('time_exit_profit', 0.03)) * 100.0
        risk['time_exit_force_days']      = int(cfg.get('time_force_days', 12))
        risk['first_day_exit_min_profit'] = float(cfg.get('first_day_exit_min_profit', 0.0)) * 100.0
        risk['first_day_exit_days']       = int(cfg.get('first_day_exit_days', 1))
        risk['take_profit_tiers'] = cfg.get('take_profit_tiers', [
            {'profit_pct': 0.03, 'sell_ratio': 0.30}
        ])
        settings._data['risk'] = risk
        # 回测参数快照（供回测 tab "系统配置"按钮读取，不参与实盘交易）
        settings._data['backtest'] = {**settings._data.get('backtest', {}),
            'hard_stop': risk['hard_stop_loss_pct'] / 100.0,
            'take_profit_tiers': risk['take_profit_tiers'],
            'trail_activate': risk['trailing_stop_activate_pct'] / 100.0,
            'trail_dd': risk['trailing_stop_drawdown_pct'] / 100.0,
            'time_exit_days': risk['time_exit_days'],
            'time_exit_profit': risk['time_exit_min_profit_pct'] / 100.0,
            'time_force_days': risk['time_exit_force_days'],
            'first_day_exit_min_profit': risk['first_day_exit_min_profit'],
            'first_day_exit_days': risk['first_day_exit_days'],
        }
        settings.save()
        log.info("回测配置已应用到系统配置")
        return {"status": "ok", "message": "回测配置已应用到系统配置"}
    except Exception as e:
        log.error(f"应用配置失败: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/api/settings/risk-params")
async def save_risk_params(body: dict):
    """直接更新 risk 段参数（只写 settings，v5.5 不再写 module 变量）"""
    try:
        from core.settings import settings
        settings.reload()
        risk = settings._data.get('risk', {})
        if 'hard_stop' in body:
            risk['hard_stop_loss_pct'] = float(body['hard_stop'])
        if 'trail_activate' in body:
            risk['trailing_stop_activate_pct'] = float(body['trail_activate'])
        if 'trail_dd' in body:
            risk['trailing_stop_drawdown_pct'] = float(body['trail_dd'])
        if 'time_exit_days' in body:
            risk['time_exit_days'] = int(body['time_exit_days'])
        if 'time_exit_profit' in body:
            risk['time_exit_min_profit_pct'] = float(body['time_exit_profit'])
        if 'time_force_days' in body:
            risk['time_exit_force_days'] = int(body['time_force_days'])
        if 'loss_streak_halve' in body:
            risk['loss_streak_halve'] = int(body['loss_streak_halve'])
        if 'loss_streak_pause' in body:
            risk['loss_streak_pause'] = int(body['loss_streak_pause'])
        if 'pause_days' in body:
            risk['pause_days'] = int(body['pause_days'])
        if 'same_stock_cooldown' in body:
            risk['same_stock_cooldown'] = int(body['same_stock_cooldown'])
        if 'first_day_exit_min_profit' in body:
            risk['first_day_exit_min_profit'] = float(body['first_day_exit_min_profit'])
        if 'first_day_exit_days' in body:
            risk['first_day_exit_days'] = int(body['first_day_exit_days'])
        if 'take_profit_tiers' in body:
            risk['take_profit_tiers'] = body['take_profit_tiers']
        settings._data['risk'] = risk
        # 回测参数快照同步更新
        settings._data['backtest'] = {**settings._data.get('backtest', {}),
            'hard_stop': risk.get('hard_stop_loss_pct', -6.0) / 100.0,
            'trail_activate': risk.get('trailing_stop_activate_pct', 5.0) / 100.0,
            'trail_dd': risk.get('trailing_stop_drawdown_pct', 2.0) / 100.0,
            'time_exit_days': risk.get('time_exit_days', 7),
            'time_exit_profit': risk.get('time_exit_min_profit_pct', 3.0) / 100.0,
            'time_force_days': risk.get('time_exit_force_days', 12),
            'first_day_exit_min_profit': risk.get('first_day_exit_min_profit', 3.0),
            'first_day_exit_days': risk.get('first_day_exit_days', 1),
            'take_profit_tiers': risk.get('take_profit_tiers', []),
        }
        settings.save()
        log.info("止盈止损参数已保存到 settings（risk 段）")
        return {"status": "ok", "message": "止盈止损参数已保存"}
    except Exception as e:
        log.error(f"保存止盈止损参数失败: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/api/backtest/run-simple")
async def run_simple_backtest(body: dict):
    """执行日线收盘价回测"""
    from app.backtest.simple_runner import run_backtest
    import threading as _th

    params = body.get("params", body)
    strategy_type = params.get("strategy_type", "python")
    # 确保日期正确
    if 'start_date' not in params:
        params['start_date'] = date(2023, 1, 1)
    if 'end_date' not in params:
        params['end_date'] = date.today()

    # 保存配置（仅 Python 策略）
    if strategy_type != "tdx":
        _save_bt_config(params)

    stop_evt = _th.Event()
    with _stop_events_lock:
        stop_events['simple_bt'] = stop_evt

    def _run():
        try:
            _formula_name = params.get("strategy_name") or "QUANTQQ"
            _start = params.get('start_date')
            _end = params.get('end_date')
            _period = params.get('intraday_freq', 'daily')
            _init_cap = params.get('initial_capital', 1000000)
            _pos_size = params.get('position_size', 50000)

            def _prog(step, total, msg):
                sync_broadcast({
                    "type": "backtest_progress",
                    "step": step, "total": total, "msg": msg,
                    "context": "simple_bt"
                })

            # 启动日志
            sync_broadcast({"type": "log", "level": "info",
                "msg": f"[回测开始] 公式:{_formula_name} | {_start} ~ {_end} | 精度:{_period} | "
                       f"初始:{_init_cap:,.0f} 仓位:{_pos_size:,.0f}"})

            # 加载股票名称映射
            stock_names = {}
            try:
                df_names = db.conn.execute("SELECT code, name FROM stocks").fetchdf()
                stock_names = dict(zip(df_names['code'], df_names['name']))
                sync_broadcast({"type": "log", "level": "info",
                    "msg": f"[数据加载] 股票池 {len(stock_names):,} 只"})
            except Exception:
                pass

            if strategy_type == "tdx":
                from app.backtest.tdx_runner import run_tdx_backtest
                sync_broadcast({"type": "log", "level": "info",
                    "msg": f"[引擎选择] TDX 公式回测 (公式:{_formula_name})"})
                result = run_tdx_backtest(params, progress_cb=_prog,
                                          stop_event=stop_evt, stock_names=stock_names)
            else:
                sync_broadcast({"type": "log", "level": "info",
                    "msg": f"[引擎选择] Python 策略回测 (策略:{_formula_name})"})
                result = run_backtest(params, progress_cb=_prog, stop_event=stop_evt,
                                      stock_names=stock_names)

            if result.get('status') == 'stopped':
                sync_broadcast({"type": "log", "level": "warn", "msg": "回测已停止"})
                sync_broadcast({"type": "simple_bt_stopped"})
                return

            # 检查 TDX 信号为 0 的情况
            trades_count = len(result.get('trades', []))
            if strategy_type == "tdx" and trades_count == 0:
                sync_broadcast({"type": "log", "level": "warn",
                    "msg": f"[无信号] 公式 '{_formula_name}' 在 {_start} ~ {_end} 区间内无任何信号/交易。请检查公式是否存在或区间是否合理"})

            # 持久化结果
            result_id = None
            try:
                import time as _time
                result_id = f"bt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(_time.time())}"
                save_data = {
                    'id': result_id,
                    'created_at': str(datetime.now()),
                    'summary': result['summary'],
                    'equity': result['equity'],
                    'trades': result['trades'],
                    'indices': result['indices'],
                    'daily_trades': result.get('daily_trades', {}),
                    'params': result.get('params', {}),
                    'message': result.get('message', ''),
                }
                save_path = _BT_RESULTS_DIR / f"{result_id}.json"
                with open(save_path, 'w', encoding='utf-8') as f:
                    _json.dump(save_data, f, ensure_ascii=False, default=str)
                log.info(f"回测结果已保存: {result_id}")
            except Exception as _se:
                log.warning(f"回测结果保存失败: {_se}")

            sync_broadcast({
                "type": "simple_bt_done",
                "result_id": result_id,
                "summary": result.get('summary', {'total_return': 0, 'max_drawdown': 0, 'win_rate': 0, 'sharpe': 0}),
                "equity": result.get('equity', []),
                "trades": result.get('trades', []),
                "indices": result.get('indices', []),
                "daily_trades": result.get('daily_trades', {}),
                "params": result.get('params', {}),
            })

            # 额外的丰富完成日志（前端 simple_bt_done 已记基本日志，这里加更详细）
            try:
                s = result.get('summary', {})
                trades = result.get('trades', [])
                sync_broadcast({"type": "log", "level": "info",
                    "msg": f"[数据源] {s.get('data_source', '?')} | 数据区间: {s.get('start_date', '')} ~ {s.get('end_date', '')}"})
                # 月度盈亏
                monthly = {}
                for t in trades:
                    key = str(t.get('exit_date', ''))[:7]
                    if key:
                        monthly[key] = monthly.get(key, 0) + float(t.get('profit', 0))
                if monthly:
                    monthly_lines = []
                    for m in sorted(monthly.keys()):
                        p = monthly[m]
                        sign = "+" if p >= 0 else ""
                        monthly_lines.append(f"{m}:{sign}{p:,.0f}")
                    sync_broadcast({"type": "log", "level": "info",
                        "msg": f"[月度盈亏] {' | '.join(monthly_lines)}"})
                # Top 3 盈利
                if trades:
                    top = sorted(trades, key=lambda t: float(t.get('profit', 0)), reverse=True)[:3]
                    top_lines = []
                    for t in top:
                        top_lines.append(f"{t.get('code', '')} +{float(t.get('profit', 0)):,.0f} (+{float(t.get('ret_pct', 0)):.1f}%) {t.get('reason', '')}")
                    sync_broadcast({"type": "log", "level": "info",
                        "msg": f"[Top 3 盈利] {' | '.join(top_lines)}"})
                    # Worst 3
                    worst = sorted(trades, key=lambda t: float(t.get('profit', 0)))[:3]
                    worst_lines = []
                    for t in worst:
                        worst_lines.append(f"{t.get('code', '')} {float(t.get('profit', 0)):,.0f} ({float(t.get('ret_pct', 0)):.1f}%) {t.get('reason', '')}")
                    sync_broadcast({"type": "log", "level": "info",
                        "msg": f"[Worst 3] {' | '.join(worst_lines)}"})
                # 资金轨迹
                eq = result.get('equity', [])
                if eq:
                    max_eq = max(eq, key=lambda e: float(e.get('equity', 0)))
                    min_eq = min(eq, key=lambda e: float(e.get('equity', 0)))
                    sync_broadcast({"type": "log", "level": "info",
                        "msg": f"[资金轨迹] 最高:{max_eq.get('date', '')} {float(max_eq.get('equity', 0)):,.0f} | 最低:{min_eq.get('date', '')} {float(min_eq.get('equity', 0)):,.0f}"})
            except Exception as _e:
                log.debug(f"完成详细日志失败: {_e}")
        except Exception as e:
            import traceback
            log.error(f"简化回测崩溃: {e}\n{traceback.format_exc()}")
            sync_broadcast({"type": "log", "level": "error", "msg": f"回测失败: {e}"})
        finally:
            with _stop_events_lock:
                if 'simple_bt' in stop_events:
                    del stop_events['simple_bt']

    run_in_thread(_run)
    return {"status": "started", "message": "回测已开始"}


@router.post("/api/backtest/run-simple/stop")
async def stop_simple_backtest():
    """中断正在运行的简化回测（用户在 UI 点停止时调用）"""
    with _stop_events_lock:
        evt = stop_events.get('simple_bt')
        if evt:
            evt.set()
    # 跨进程 stop signal: touch 文件让 TDX worker 立即优雅退出(不等 proc.kill)
    try:
        os.makedirs("output", exist_ok=True)
        open("output/bt_stop.signal", "w").close()
    except Exception as e:
        log.warning(f"创建 stop signal 文件失败(可能不影响主进程 stop_event): {e}")
    sync_broadcast({"type": "log", "level": "warning", "msg": "🛑 简化回测停止指令已发送"})
    return {"status": "ok", "message": "停止信号已发送"}


@router.get("/api/backtest/simple/history")
async def list_simple_bt_history(limit: int = 20):
    """列出历史回测结果"""
    files = sorted(_BT_RESULTS_DIR.glob("bt_*.json"), reverse=True)
    items = []
    for f in files[:limit]:
        try:
            with open(f, 'r', encoding='utf-8') as fp:
                data = _json.load(fp)
            items.append({
                'id': data.get('id', f.stem),
                'created_at': data.get('created_at', ''),
                'total_return': data['summary'].get('total_return'),
                'max_drawdown': data['summary'].get('max_drawdown'),
                'win_rate': data['summary'].get('win_rate'),
                'calmar': data['summary'].get('calmar'),
                'trades': data['summary'].get('trades'),
                'start_date': data['summary'].get('start_date'),
                'end_date': data['summary'].get('end_date'),
            })
        except Exception:
            pass
    return {"status": "ok", "data": items, "total": len(files)}


@router.get("/api/backtest/simple/history/{result_id}")
async def get_simple_bt_history(result_id: str):
    """加载指定回测结果的完整数据"""
    import re
    if not re.match(r'^bt_[A-Za-z0-9_]+$', result_id):
        return {"status": "error", "message": "非法 result_id 格式"}
    fp = _BT_RESULTS_DIR / f"{result_id}.json"
    if not fp.resolve().is_relative_to(_BT_RESULTS_DIR.resolve()):
        return {"status": "error", "message": "非法路径"}
    if not fp.exists():
        return {"status": "error", "message": "结果不存在"}
    with open(fp, 'r', encoding='utf-8') as f:
        data = _json.load(f)
    return {"status": "ok", "data": data}


@router.delete("/api/backtest/simple/history/{result_id}")
async def delete_simple_bt_history(result_id: str):
    """删除回测结果"""
    import re
    if not re.match(r'^bt_[A-Za-z0-9_]+$', result_id):
        return {"status": "error", "message": "非法 result_id 格式"}
    fp = _BT_RESULTS_DIR / f"{result_id}.json"
    if not fp.resolve().is_relative_to(_BT_RESULTS_DIR.resolve()):
        return {"status": "error", "message": "非法路径"}
    if fp.exists():
        fp.unlink()
        return {"status": "ok", "message": "已删除"}
    return {"status": "error", "message": "文件不存在"}


