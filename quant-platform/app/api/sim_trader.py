"""
模拟盘交易 API 路由
- 手动触发买卖
- 查询持仓和交易记录
- 定时调度（14:52 选股买入，14:54 止盈止损）
"""
from fastapi import APIRouter
from datetime import date, datetime
from pathlib import Path
from core.logger import get_logger
from server.websocket.manager import sync_broadcast
import threading
import pandas as pd

log = get_logger("API-SimTrader")
router = APIRouter()

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
OUT_DIR = ROOT_DIR / "output" / "sim_trader"

# 引擎单例
_engine = None
_engine_lock = threading.Lock()


def get_engine():
    global _engine
    if _engine is None:
        from app.sim_trader.engine import SimTraderEngine
        _engine = SimTraderEngine()
    return _engine


# 交易日缓存
_trading_calendar: set = None
_trading_calendar_year: int = 0


def _load_trading_calendar():
    """从 baostock 获取交易所官方交易日历，按年缓存"""
    global _trading_calendar, _trading_calendar_year
    current_year = date.today().year
    if _trading_calendar is not None and _trading_calendar_year == current_year:
        return _trading_calendar

    calendar = set()
    try:
        import baostock as bs
        bs.login()
        for year in (current_year - 1, current_year):
            rs = bs.query_trade_dates(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
            if rs.error_code == '0':
                while rs.next():
                    row = rs.get_row_data()
                    if row[1] == '1':  # 1 = 交易日
                        calendar.add(date.fromisoformat(row[0]))
        bs.logout()
    except Exception as e:
        log.warning(f"baostock 交易日历获取失败: {e}，回退到 weekday 判断")

    _trading_calendar = calendar
    _trading_calendar_year = current_year
    return calendar


def is_trading_day(d: date = None) -> bool:
    """判断是否为交易所交易日"""
    if d is None:
        d = date.today()
    # 周末一定不是交易日
    if d.weekday() >= 5:
        return False
    # 查交易所日历
    try:
        calendar = _load_trading_calendar()
        if calendar:
            return d in calendar
    except Exception:
        pass
    # 兜底：工作日
    return True


def get_trading_dates():
    """获取交易日列表（实盘用交易所日历，回测兜底用 Parquet）"""
    daily_dir = ROOT_DIR / "data" / "parquet" / "daily"
    calendar = _load_trading_calendar()

    if calendar:
        # 交易所日历优先
        today = date.today()
        if today.weekday() < 5 and today not in calendar:
            calendar.add(today)  # 兜底加今天
        return sorted(calendar)

    # 兜底：从 Parquet 读
    if daily_dir.exists():
        try:
            for f in sorted(daily_dir.glob("*.parquet")):
                if f.stem.startswith('index_') or not (len(f.stem) == 6 and f.stem.isdigit()):
                    continue
                df = pd.read_parquet(str(f), columns=['date'])
                df['date'] = pd.to_datetime(df['date']).dt.date
                return sorted(df['date'].unique())
        except Exception:
            pass
    return []


@router.get("/api/sim-trader/status")
async def sim_trader_status():
    engine = get_engine()
    today = date.today()

    # 直接从 Parquet 读取今日收盘价（避免 DuckDB 锁）
    snapshot = {}
    try:
        daily_dir = ROOT_DIR / "data" / "parquet" / "daily"
        for p in engine.active_positions():
            f = daily_dir / f"{p.code}.parquet"
            if f.exists():
                df = pd.read_parquet(str(f), columns=['date', 'close'])
                df['date'] = pd.to_datetime(df['date']).dt.date
                row = df[df['date'] == today]
                if not row.empty:
                    snapshot[p.code] = {'close': float(row.iloc[0]['close'])}
    except Exception as e:
        log.warning(f"读取今日快照失败: {e}")

    positions = []
    for p in engine.active_positions():
        cur_price = snapshot.get(p.code, {}).get('close', p.entry_price)
        positions.append({
            'code': p.code,
            'entry_date': str(p.entry_date),
            'entry_price': p.entry_price,
            'shares': p.shares,
            'remaining': p.remaining_shares,
            'cost': p.cost,
            'current_price': cur_price,
            'profit_pct': round((cur_price / p.entry_price - 1) * 100, 2),
            'market_value': round(p.remaining_shares * cur_price, 2),
            'strategy_name': p.strategy_name,
        })

    from app.sim_trader.config import SELL_MODE as _sell_mode
    return {
        'status': 'ok',
        'cash': round(engine.cash, 2),
        'equity': round(engine.total_equity(snapshot), 2),
        'positions': positions,
        'position_count': engine.position_count,
        'trade_count': len(engine.trades),
        'consecutive_losses': engine.consecutive_losses,
        'paused': engine.pause_until is not None and today <= engine.pause_until,
        'today': str(today),
        'monitor_enabled': engine.monitor_enabled,
        'monitor_mode': engine.monitor.mode if engine.monitor else 'close',
        'auto_sell': engine.auto_sell,
        'auto_scan': engine.auto_scan,
        'auto_buy': engine.auto_buy,
        'sell_mode': _sell_mode,
    }


@router.post("/api/sim-trader/execute")
async def sim_trader_execute():
    """手动触发一次买卖（用于测试）"""
    today = date.today()

    if not is_trading_day(today):
        return {'status': 'error', 'message': f'{today} 不是交易日'}

    log.info(f"手动触发模拟盘交易: {today}")

    # 加载数据
    from app.sim_trader.data_loader import (
        load_all_bars, get_daily_snapshot, load_sh_index,
        generate_today_signals, augment_bars_with_realtime
    )
    from app.sim_trader.config import SAME_STOCK_COOLDOWN, STRATEGY_NAME

    engine = get_engine()
    bars = load_all_bars()
    bars, snapshot = augment_bars_with_realtime(bars, today)
    sh_idx = load_sh_index()

    # 生成当日信号
    signals = generate_today_signals(bars, today)
    trading_dates = get_trading_dates()

    # 14:52 卖出
    engine.sell_phase(today, snapshot, trading_dates)
    sell_count = len([t for t in engine.trades if t.exit_date == today])

    # 14:54 买入
    buy_count = 0
    paused = engine.pause_until is not None and today <= engine.pause_until
    if not paused and signals:
        max_new = int(engine.cash / engine.max_buy_amount()) + 1
        for code, price in signals[:max_new]:
            if any(t.code == code and (today - t.entry_date).days <= SAME_STOCK_COOLDOWN
                   for t in engine.trades):
                continue
            if engine.execute_buy(today, code, price, strategy_name=STRATEGY_NAME):
                buy_count += 1

    # 记录
    engine.record(today, snapshot)

    sync_broadcast({
        'type': 'sim_trader_update',
        'today': str(today),
        'buy_count': buy_count,
        'sell_count': sell_count,
        'equity': round(engine.total_equity(snapshot), 2),
        'cash': round(engine.cash, 2),
        'positions': engine.position_count,
    })

    return {
        'status': 'ok',
        'today': str(today),
        'signals_today': len(signals),
        'bought': buy_count,
        'sold': sell_count,
        'equity': round(engine.total_equity(snapshot), 2),
        'cash': round(engine.cash, 2),
        'positions': engine.position_count,
    }


@router.get("/api/sim-trader/trades")
async def sim_trader_trades(limit: int = 50):
    engine = get_engine()
    trades = engine.trades[-limit:]
    return {
        'status': 'ok',
        'trades': [{
            'code': t.code,
            'entry': str(t.entry_date),
            'exit': str(t.exit_date),
            'entry_px': t.entry_price,
            'exit_px': t.exit_price,
            'shares': t.shares,
            'ret_pct': round(t.return_pct, 2),
            'profit': round(t.profit_amount, 0),
            'reason': t.exit_reason,
            'hold_days': t.hold_days,
            'entry_reason': t.entry_reason,
            'exit_timing': t.exit_timing,
        } for t in reversed(trades)]
    }


@router.post("/api/sim-trader/reset")
async def sim_trader_reset():
    """重置模拟盘"""
    global _engine
    with _engine_lock:
        from app.sim_trader.engine import SimTraderEngine
        from app.sim_trader.store import SimTraderStore
        SimTraderStore().clear_all()
        _engine = SimTraderEngine(persist=True)
    log.info("模拟盘已重置")
    return {'status': 'ok', 'message': '模拟盘已重置为初始状态'}


@router.get("/api/sim-trader/config")
async def sim_trader_config():
    """获取模拟盘配置（当前策略、可用策略列表）"""
    from app.sim_trader.config import STRATEGY_NAME as _cur
    from pathlib import Path
    import os

    strat_dir = Path(__file__).resolve().parent.parent / "screener" / "strategies"
    available = []
    for f in sorted(strat_dir.glob("*.py")):
        name = f.stem
        if name in ("base", "__init__"):
            continue
        # 读取首行注释作为描述
        desc = ""
        try:
            first = f.read_text(encoding="utf-8").strip().split("\n")[0]
            if first.startswith("#"):
                desc = first.lstrip("# ").strip()
        except:
            pass
        available.append({"name": name, "desc": desc})

    return {
        "status": "ok",
        "current_strategy": _cur,
        "strategies": available,
    }


@router.post("/api/sim-trader/config")
async def sim_trader_set_config(data: dict):
    """切换模拟盘策略"""
    import app.sim_trader.config as _cfg
    new_name = data.get("strategy_name", "")
    if not new_name:
        return {"status": "error", "message": "缺少 strategy_name 参数"}
    _cfg.STRATEGY_NAME = new_name
    log.info(f"模拟盘策略已切换为: {new_name}")
    return {"status": "ok", "message": f"策略已切换为 {new_name}", "current_strategy": new_name}


@router.get("/api/sim-trader/monitor")
async def sim_trader_monitor_status():
    """获取自动执行开关状态"""
    from app.sim_trader.config import AUTO_SELL, AUTO_SCAN, AUTO_BUY, SELL_MODE, MONITOR_ENABLED, MONITOR_MODE
    engine = get_engine()
    return {
        "status": "ok",
        "auto_sell": AUTO_SELL,
        "auto_scan": AUTO_SCAN,
        "auto_buy": AUTO_BUY,
        "sell_mode": SELL_MODE,
        "monitor_enabled": engine.monitor_enabled if engine else False,
        "monitor_mode": engine.monitor.mode if engine and engine.monitor else MONITOR_MODE,
    }


@router.post("/api/sim-trader/monitor")
async def sim_trader_monitor_control(data: dict):
    """设置自动执行开关和盘中监控"""
    import app.sim_trader.config as _cfg
    if "auto_sell" in data:
        _cfg.AUTO_SELL = bool(data["auto_sell"])
    if "auto_scan" in data:
        _cfg.AUTO_SCAN = bool(data["auto_scan"])
    if "auto_buy" in data:
        _cfg.AUTO_BUY = bool(data["auto_buy"])
    if "sell_mode" in data:
        _cfg.SELL_MODE = data["sell_mode"]

    # 盘中监控控制
    engine = get_engine()
    if engine and engine.monitor:
        if "monitor_enabled" in data:
            if bool(data["monitor_enabled"]):
                if "monitor_mode" in data:
                    engine.monitor.mode = data["monitor_mode"]
                    _cfg.MONITOR_MODE = data["monitor_mode"]
                engine.monitor.start()
            else:
                engine.monitor.stop()
        elif "monitor_mode" in data:
            engine.monitor.mode = data["monitor_mode"]
            _cfg.MONITOR_MODE = data["monitor_mode"]

    log.info(f"开关: 卖出={'执行' if _cfg.AUTO_SELL else '告警'}({_cfg.SELL_MODE}) "
             f"选股={'开' if _cfg.AUTO_SCAN else '关'} "
             f"买入={'执行' if _cfg.AUTO_BUY else '不买'} "
             f"监控={'开' if (engine and engine.monitor_enabled) else '关'}")
    return {
        "status": "ok",
        "auto_sell": _cfg.AUTO_SELL,
        "auto_scan": _cfg.AUTO_SCAN,
        "auto_buy": _cfg.AUTO_BUY,
        "sell_mode": _cfg.SELL_MODE,
        "monitor_enabled": engine.monitor_enabled if engine else False,
        "monitor_mode": engine.monitor.mode if engine and engine.monitor else _cfg.MONITOR_MODE,
    }
