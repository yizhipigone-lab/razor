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
import json
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
        with _engine_lock:
            if _engine is None:
                from app.sim_trader.engine import SimTraderEngine
                from app.sim_trader.store import JsonSimStore
                # 从 app_setting.json 恢复持久化的开关设置
                from core.settings import settings as _settings
                import app.sim_trader.config as _sc
                sim = _settings._data.get('sim_trader', {})
                if sim:
                    _sc.AUTO_SELL = sim.get('auto_sell', _sc.AUTO_SELL)
                    _sc.AUTO_SCAN = sim.get('auto_scan', _sc.AUTO_SCAN)
                    _sc.AUTO_BUY = sim.get('auto_buy', _sc.AUTO_BUY)
                    _sc.MONITOR_ENABLED = sim.get('monitor_enabled', _sc.MONITOR_ENABLED)
                    _sc.MONITOR_MODE = sim.get('monitor_mode', _sc.MONITOR_MODE)
                    _sc.STRATEGY_NAME = sim.get('strategy_name', _sc.STRATEGY_NAME)
                _engine = SimTraderEngine(store=JsonSimStore())
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


@router.post("/api/settings/sim-switches")
async def save_sim_switches(body: dict):
    """保存执行开关到 config.py"""
    import app.sim_trader.config as sc
    try:
        if 'auto_sell' in body:
            sc.AUTO_SELL = bool(body['auto_sell'])
        if 'auto_scan' in body:
            sc.AUTO_SCAN = bool(body['auto_scan'])
        if 'auto_buy' in body:
            sc.AUTO_BUY = bool(body['auto_buy'])
        if 'monitor_enabled' in body:
            sc.MONITOR_ENABLED = bool(body['monitor_enabled'])
        if 'monitor_mode' in body:
            sc.MONITOR_MODE = str(body['monitor_mode'])
        if 'strategy_name' in body:
            sc.STRATEGY_NAME = str(body['strategy_name'])
        # 同步更新已创建的引擎和监控器实例
        if _engine is not None:
            from app.sim_trader.engine import SimTraderEngine
            _engine._mode = sc.MONITOR_MODE
            if _engine._monitor is not None:
                _engine._monitor.mode = sc.MONITOR_MODE
                _engine._monitor.enabled = sc.MONITOR_ENABLED
        # 持久化到 app_setting.json，重启不丢
        from core.settings import settings
        sim = settings._data.setdefault('sim_trader', {})
        sim['auto_sell'] = sc.AUTO_SELL
        sim['auto_scan'] = sc.AUTO_SCAN
        sim['auto_buy'] = sc.AUTO_BUY
        sim['monitor_enabled'] = sc.MONITOR_ENABLED
        sim['monitor_mode'] = sc.MONITOR_MODE
        sim['strategy_name'] = sc.STRATEGY_NAME
        settings._data['sim_trader'] = sim
        settings.save()

        log.info(f"执行开关已更新并持久化: SELL={sc.AUTO_SELL} SCAN={sc.AUTO_SCAN} BUY={sc.AUTO_BUY} MON={sc.MONITOR_ENABLED}/{sc.MONITOR_MODE} STRAT={sc.STRATEGY_NAME}")
        return {"status": "ok", "message": "已保存"}
    except Exception as e:
        log.error(f"保存执行开关失败: {e}")
        return {"status": "error", "message": str(e)}


_stock_names_cache = None

def _load_stock_names():
    global _stock_names_cache
    if _stock_names_cache is not None:
        return _stock_names_cache
    _stock_names_cache = {}
    try:
        from database.duckdb_manager import db
        df = db.conn.execute("SELECT code, name FROM stocks").fetchdf()
        _stock_names_cache = dict(zip(df['code'].astype(str), df['name'].astype(str)))
    except Exception:
        pass
    return _stock_names_cache


@router.get("/api/sim-trader/status")
async def sim_trader_status():
    engine = get_engine()
    today = date.today()
    names = _load_stock_names()

    # 优先 QMT 实时行情，失败回退 Parquet
    active_codes = [p.code for p in engine.active_positions()]
    snapshot = {}
    missing = set(active_codes)

    if active_codes:
        try:
            from app.data_manager.engine import get_realtime_quote
            rt = get_realtime_quote(active_codes)
            if not rt.empty:
                for _, row in rt.iterrows():
                    code = str(row.get('code', ''))
                    price = float(row.get('price', 0))
                    if price > 0:
                        snapshot[code] = {'close': price}
                        missing.discard(code)
        except Exception as e:
            log.warning(f"QMT实时行情失败: {e}")

    # 回退：从 Parquet 读取今日收盘价
    if missing:
        try:
            daily_dir = ROOT_DIR / "data" / "parquet" / "daily"
            for code in list(missing):
                f = daily_dir / f"{code}.parquet"
                if f.exists():
                    df = pd.read_parquet(str(f), columns=['date', 'close'])
                    df['date'] = pd.to_datetime(df['date']).dt.date
                    row = df[df['date'] == today]
                    if not row.empty:
                        snapshot[code] = {'close': float(row.iloc[0]['close'])}
        except Exception as e:
            log.warning(f"Parquet回退失败: {e}")

    positions = []
    for p in engine.active_positions():
        cur_price = snapshot.get(p.code, {}).get('close', p.entry_price)
        positions.append({
            'code': p.code,
            'name': names.get(p.code, ''),
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

    total_unrealized_pnl = sum(
        (p['current_price'] - p['entry_price']) * p['remaining']
        for p in positions
    )

    return {
        'status': 'ok',
        'cash': round(engine.cash, 2),
        'equity': round(engine.total_equity(snapshot), 2),
        'total_unrealized_pnl': round(total_unrealized_pnl, 2),
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
    }


@router.post("/api/sim-trader/execute")
async def sim_trader_execute():
    today = date.today()

    # 执行日期 fallback：非交易日回退到最近交易日
    if not is_trading_day(today):
        trade_dates = get_trading_dates()
        past_dates = [d for d in trade_dates if d < today]
        if not past_dates:
            return {'status': 'error', 'message': f'{today} 不是交易日，且无历史交易日数据'}
        today = max(past_dates)

    # 信号日期：盘前(<9:30)用昨天信号，盘中/盘后用今天
    now = datetime.now()
    if is_trading_day(date.today()) and now.hour * 60 + now.minute >= 9 * 60 + 30:
        signal_date = today
    else:
        trade_dates = get_trading_dates()
        past_dates = [d for d in trade_dates if d < today]
        signal_date = max(past_dates) if past_dates else today
        if signal_date != today:
            log.info(f"手动触发: {date.today()} 盘前，信号回退到最近交易日 {signal_date}")
    log.info(f"手动触发模拟盘交易: 执行日={today} 信号日={signal_date}")
    sync_broadcast({'type': 'log', 'level': 'info', 'msg': f'手动触发: 执行日={today} 信号日={signal_date}，后台执行中...'})

    # 盘前(<9:25)手动触发用 Parquet 昨收价
    pre_market = is_trading_day(date.today()) and datetime.now().hour * 60 + datetime.now().minute < 9 * 60 + 25

    def _run():
        try:
            from app.backtest.simple_runner import load_daily_bars
            from app.sim_trader.data_loader import get_daily_snapshot
            engine = get_engine()
            # 盘前用最近完整交易日收盘价，盘中用 QMT 实时价
            price_date = max(d for d in get_trading_dates() if d < today) if pre_market else today
            bars = load_daily_bars(end=today)
            snapshot = get_daily_snapshot(bars, price_date)
            trading_dates = get_trading_dates()
            sell_count = 0
            if engine.auto_sell:
                engine.sell_phase(today, snapshot, trading_dates)
                sell_count = len([t for t in engine.trades if t.exit_date == today])

            # 买入：通过 TDX 桥接获取 QUANTQQ 信号
            buy_count = 0
            log.info(f"买入检查: auto_buy={engine.auto_buy} auto_scan={engine.auto_scan}")
            if engine.auto_buy and engine.auto_scan:
                try:
                    from app.tqsdk.bridge import TdxBridge
                    bridge = TdxBridge()
                    sig_result = bridge.execute_screen(
                        end_time=signal_date.strftime('%Y%m%d'),
                        lookback_days=500,
                    )
                    if sig_result.get('status') == 'ok':
                        matched = sig_result.get('matched', [])
                        log.info(f'QUANTQQ选股: {len(matched)}只')
                        from app.sim_trader.config import SAME_STOCK_COOLDOWN
                        paused = engine.pause_until is not None and today <= engine.pause_until
                        if not paused and matched:
                            for code in matched:
                                code_num = code.split('.')[0] if '.' in code else code
                                px = snapshot.get(code_num, {}).get('close', 0)
                                if px <= 0:
                                    continue
                                if any(t.code == code_num and (today - t.entry_date).days <= SAME_STOCK_COOLDOWN
                                       for t in engine.trades):
                                    continue
                                if engine.execute_buy(today, code_num, px, strategy_name=f'手动-{STRATEGY_NAME}'):
                                    buy_count += 1
                except Exception as e:
                    log.warning(f'QUANTQQ选股失败: {e}')

            engine.record(today, snapshot)
            sync_broadcast({'type': 'sim_trader_update', 'today': str(today), 'buy_count': buy_count, 'sell_count': sell_count,
                'equity': round(engine.total_equity(snapshot), 2), 'cash': round(engine.cash, 2), 'positions': engine.position_count})
            sync_broadcast({'type': 'done', 'msg': f'手动触发完成: 持仓{engine.position_count}笔 本次买入{buy_count}笔 卖出{sell_count}笔'})
        except Exception as e:
            log.error(f"手动触发异常: {e}")
            sync_broadcast({'type': 'error', 'msg': f'手动触发失败: {e}'})

    threading.Thread(target=_run, daemon=True).start()
    return {'status': 'started', 'message': '手动触发已启动，结果将通过WebSocket推送'}


@router.get("/api/sim-trader/trades")
async def sim_trader_trades(limit: int = 50):
    engine = get_engine()
    names = _load_stock_names()
    today = date.today()

    # 已完成交易
    trades = engine.trades[-limit:]
    result = []
    for t in reversed(trades):
        result.append({
            'code': t.code, 'name': names.get(t.code, ''),
            'entry': str(t.entry_date), 'entry_time': getattr(t, 'entry_time', '15:00'),
            'exit': str(t.exit_date), 'exit_time': getattr(t, 'exit_time', '15:00'),
            'entry_px': t.entry_price, 'exit_px': t.exit_price,
            'shares': t.shares, 'ret_pct': round(t.return_pct, 2),
            'profit': round(t.profit_amount, 0), 'reason': t.exit_reason,
            'hold_days': t.hold_days, 'entry_reason': t.entry_reason,
            'exit_timing': t.exit_timing, 'status': '已平仓',
        })

    # 当前持仓（买入记录，尚未卖出）
    try:
        daily_dir = ROOT_DIR / "data" / "parquet" / "daily"
        for p in engine.active_positions():
            cur_px = p.entry_price
            f = daily_dir / f"{p.code}.parquet"
            if f.exists():
                df_snap = pd.read_parquet(str(f), columns=['date', 'close'])
                df_snap['date'] = pd.to_datetime(df_snap['date']).dt.date
                df_snap = df_snap.sort_values('date')
                row = df_snap[df_snap['date'] == today]
                if row.empty:
                    past = df_snap[df_snap['date'] < today]
                    if not past.empty:
                        row = past.iloc[[-1]]
                if not row.empty:
                    cur_px = float(row.iloc[0]['close'])
            ret = (cur_px / p.entry_price - 1) * 100
            result.append({
                'code': p.code, 'name': names.get(p.code, ''),
                'entry': str(p.entry_date), 'entry_time': getattr(p, 'entry_time', '15:00'),
                'exit': '持仓中', 'exit_time': '',
                'entry_px': p.entry_price, 'exit_px': cur_px,
                'shares': p.shares, 'ret_pct': round(ret, 2),
                'profit': round(p.shares * (cur_px - p.entry_price), 0),
                'reason': '', 'hold_days': (today - p.entry_date).days,
                'entry_reason': p.strategy_name, 'exit_timing': '',
                'status': '持仓中',
            })
    except Exception:
        pass

    return {'status': 'ok', 'trades': result}


@router.get("/api/sim-trader/equity")
async def sim_trader_equity():
    engine = get_engine()
    equity = engine.equity_curve
    if not equity:
        return {'status': 'ok', 'equity': [], 'indices': {}}
    indices = {}
    try:
        from app.backtest.simple_runner import load_index_data
        indices = load_index_data()
    except Exception:
        pass

    # QMT 实时指数补位：Tushare 当天数据有延迟，用 QMT 实时价补齐最新一天
    today_str = str(date.today())
    try:
        idx_code_map = {
            '上证指数': '000001.SH', '沪深300': '000300.SH', '中证500': '000905.SH',
            '中证1000': '000852.SH', '中证A500': '000510.SH', '创业板指': '399006.SZ',
        }
        need_today = any(
            name in indices and (not indices[name] or indices[name][-1]['date'] < today_str)
            for name in idx_code_map
        )
        if need_today:
            import urllib.request, json as _json
            from datetime import timedelta
            codes = ','.join(idx_code_map.values())
            url = f'http://localhost:8081/api/quotes?codes={codes}'
            resp = _json.loads(urllib.request.urlopen(url, timeout=5).read())
            for name, qmt_code in idx_code_map.items():
                tick = resp.get(qmt_code, {})
                px = float(tick.get('lastPrice', 0))
                if px > 0:
                    if name not in indices:
                        indices[name] = []
                    series = indices[name]
                    # 补齐 Parquet 最后日期到今天之间的缺口
                    if series:
                        last_d = date.fromisoformat(series[-1]['date'])
                        last_close = series[-1]['close']
                        d = last_d + timedelta(days=1)
                        while d < date.today():
                            series.append({'date': str(d), 'close': last_close, 'norm': round(last_close / (series[0]['close'] or 1), 4)})
                            d += timedelta(days=1)
                    # 追加/更新今天
                    if series and series[-1]['date'] == today_str:
                        series[-1]['close'] = px
                        series[-1]['norm'] = round(px / (series[0]['close'] or 1), 4)
                    else:
                        base = series[0]['close'] if series else px
                        series.append({'date': today_str, 'close': px, 'norm': round(px / base if base else 1, 4)})
    except Exception:
        pass

    return {
        'status': 'ok',
        'equity': [{'date': str(e['date']), 'equity': e['equity'], 'cash': e.get('cash', 0), 'pos': e.get('pos', 0)} for e in equity],
        'indices': indices,
    }


@router.get("/api/sim-trader/log-dates")
async def sim_trader_log_dates():
    """列出可用的日志日期"""
    log_dir = ROOT_DIR / "output" / "sim_trader" / "logs"
    if not log_dir.exists():
        return {'status': 'ok', 'dates': []}
    dates = sorted(
        [f.stem for f in log_dir.glob("*.jsonl") if f.stem.count('-') == 2],
        reverse=True,
    )
    return {'status': 'ok', 'dates': dates}


@router.get("/api/sim-trader/logs")
async def sim_trader_logs(log_date: str = "", limit: int = 200):
    """读取指定日期的交易日志"""
    if not log_date:
        log_date = str(date.today())
    log_dir = ROOT_DIR / "output" / "sim_trader" / "logs"
    log_file = log_dir / f"{log_date}.jsonl"
    if not log_file.exists():
        return {'status': 'ok', 'entries': []}
    entries = []
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
    return {'status': 'ok', 'entries': entries[-limit:]}


@router.post("/api/sim-trader/reset")
async def sim_trader_reset():
    """重置模拟盘"""
    global _engine
    with _engine_lock:
        from app.sim_trader.engine import SimTraderEngine
        from app.sim_trader.store import JsonSimStore
        store = JsonSimStore()
        store._data = {}  # 清空
        store._save()
        _engine = SimTraderEngine(store=store)
    log.info("模拟盘已重置")
    return {'status': 'ok', 'message': '模拟盘已重置为初始状态'}


@router.get("/api/sim-trader/config")
async def sim_trader_config():
    """获取模拟盘配置（当前策略、可用策略列表）"""
    from app.sim_trader.config import STRATEGY_NAME as _cur
    from pathlib import Path
    import inspect

    strat_dir = Path(__file__).resolve().parent.parent / "screener" / "strategies"
    available = []
    for f in sorted(strat_dir.glob("*.py")):
        fname = f.stem
        if fname in ("base", "__init__"):
            continue
        # 尝试从策略类获取 name 属性
        try:
            module_path = f"app.screener.strategies.{fname}"
            mod = __import__(module_path, fromlist=[""])
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if inspect.isclass(attr) and hasattr(attr, 'name') and getattr(attr, 'name', '') not in ('BaseStrategy', ''):
                    available.append({"name": attr.name, "desc": getattr(attr, 'description', ''), "file": fname})
                    break
            else:
                available.append({"name": fname, "desc": "", "file": fname})
            # 策略变体（如同一文件通过 version 参数支持原版/改进版）
            variants = getattr(mod, 'STRATEGY_VARIANTS', {})
            for vname in variants:
                if vname not in {a['name'] for a in available}:
                    available.append({"name": vname, "desc": "", "file": fname})
        except Exception:
            available.append({"name": fname, "desc": "", "file": fname})
    # 下拉菜单显示文件名，同文件多策略时加 [名称] 区分
    for a in available:
        fname = a.get('file', '')
        a['label'] = f'{fname}.py'
    # 同一文件有多个条目时，追加策略名以区分
    file_counts = {}
    for a in available:
        f = a.get('file', '')
        file_counts[f] = file_counts.get(f, 0) + 1
    for a in available:
        if file_counts.get(a.get('file', ''), 0) > 1:
            a['label'] = f'{a["file"]}.py [{a["name"]}]'

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
    from app.sim_trader.config import AUTO_SELL, AUTO_SCAN, AUTO_BUY, MONITOR_ENABLED, MONITOR_MODE, BROKER_ENABLED
    engine = get_engine()
    return {
        "status": "ok",
        "auto_sell": AUTO_SELL,
        "auto_scan": AUTO_SCAN,
        "auto_buy": AUTO_BUY,
        "broker_enabled": BROKER_ENABLED,
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
    if "broker_enabled" in data:
        _cfg.BROKER_ENABLED = bool(data["broker_enabled"])

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

    log.info(f"开关: 卖出={'执行' if _cfg.AUTO_SELL else '告警'} "
             f"选股={'开' if _cfg.AUTO_SCAN else '关'} "
             f"买入={'执行' if _cfg.AUTO_BUY else '不买'} "
             f"券商={'开' if _cfg.BROKER_ENABLED else '关'} "
             f"监控={'开' if (engine and engine.monitor_enabled) else '关'}"
             f"({engine.monitor.mode if engine and engine.monitor else _cfg.MONITOR_MODE})")
    return {
        "status": "ok",
        "auto_sell": _cfg.AUTO_SELL,
        "auto_scan": _cfg.AUTO_SCAN,
        "auto_buy": _cfg.AUTO_BUY,
        "broker_enabled": _cfg.BROKER_ENABLED,
        "monitor_enabled": engine.monitor_enabled if engine else False,
        "monitor_mode": engine.monitor.mode if engine and engine.monitor else _cfg.MONITOR_MODE,
    }


@router.post("/api/quotes/live")
async def get_live_quotes(body: dict):
    """批量获取实时行情（自选股+持仓轮询用）"""
    codes = body.get("codes", [])
    if not codes:
        return {"status": "ok", "data": {}}

    try:
        from app.data_manager.engine import get_realtime_quote
        df = get_realtime_quote(codes)
        if df is None or df.empty:
            return {"status": "ok", "data": {}}

        result = {}
        for _, row in df.iterrows():
            code = str(row.get("code", ""))
            price = float(row.get("price", 0))
            if not code or price <= 0:
                continue
            last_close = float(row.get("last_close", 0))
            result[code] = {
                "price": round(price, 2),
                "last_close": round(last_close, 2),
                "change_pct": round((price - last_close) / last_close * 100, 2) if last_close > 0 else 0,
                "high": round(float(row.get("high", price)), 2),
                "low": round(float(row.get("low", price)), 2),
            }
        return {"status": "ok", "data": result}
    except Exception as e:
        log.warning(f"实时行情批量获取失败: {e}")
        return {"status": "ok", "data": {}}
