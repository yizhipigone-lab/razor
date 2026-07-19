"""实盘交易 - 行情/查询类路由。

阶段 1 第 2 步(2026-07-19) 从 main.py 抽离,共 9 个路由:
  GET /live/status         (原 main.py:482)
  GET /live/asset          (原 main.py:499)
  GET /live/positions      (原 main.py:533, 用 _resolve_instrument_name)
  GET /live/orders         (原 main.py:560)
  GET /live/deals          (原 main.py:573, 用 _load_trading_calendar)
  GET /live/quotes         (原 main.py:626)
  GET /live/equity         (原 main.py:1567)
  GET /live/stocklist      (原 main.py:1617)
  GET /live/index/members  (原 main.py:1666)

依赖策略:
  - 顶部 import: _state(独立模块)、fastapi、logger
  - positions 函数内 import _resolve_instrument_name(与 get_risk_status 共用,
    _instrument_name_cache 暂留 main,阶段1后续 get_risk_status 搬走时统一归属)
  - deals 函数内 import _load_trading_calendar(跨 app.api.sim_trader 包,绝对路径不变)
"""
from fastapi import APIRouter, HTTPException

from core.logger import get_logger

from .._state import state as _state

logger = get_logger("live_trader.routers.market")

router = APIRouter()


@router.get("/live/status")
async def status():
    """实盘总览状态"""
    config = _state.get("config")
    qmt = _state.get("qmt")
    ks = _state.get("kill_switch")
    store = _state.get("store")
    runtime_state = _state.get("runtime_state")
    return {
        "mode": runtime_state.mode if runtime_state else (config.mode if config else "unknown"),
        "qmt_connected": qmt.connected if qmt else False,
        "kill_switch": ks.status() if ks else {"activated": False},
        "live_capital": config.live_capital if config else 0,
        "account_id": config.qmt_account_id if config else "",
    }


@router.get("/live/asset")
async def asset():
    """资金查询"""
    qmt = _state.get("qmt")
    if not qmt or not qmt.connected:
        raise HTTPException(503, "QMT 未连接")
    return qmt.query_asset()


@router.get("/live/positions")
async def positions():
    """持仓查询(含 managed 标记 + today_buy_volume + 简称)"""
    store = _state.get("store")
    if not store:
        raise HTTPException(503, "Store 未初始化")
    positions = store.get_positions(managed_only=False)
    # 补 today_buy_volume(今日买入量,从 live_deals 算)
    # 前端按"今日买入部分按买入价、过夜部分按昨收"拆分今日盈亏
    if positions and store._conn:
        from datetime import date as _date
        today = _date.today().isoformat()
        rows = store._conn.execute(
            "SELECT code, SUM(filled_volume) FROM live_deals "
            "WHERE direction = 'buy' AND traded_at >= ? "
            "GROUP BY code", [today]
        ).fetchall()
        buy_map = {r[0]: int(r[1] or 0) for r in rows}
        for p in positions:
            p['today_buy_volume'] = buy_map.get(p.get('code'), 0)
    # 补股票简称(stocks 基础表不含 ETF,改用 xtdata.get_instrument_detail 全覆盖)
    from ..main import _resolve_instrument_name  # 与 get_risk_status 共用,cache 暂留 main
    qmt = _state.get("qmt")
    for p in positions:
        p['name'] = _resolve_instrument_name(p.get('code', ''), qmt)
    return positions


@router.get("/live/orders")
async def orders(limit: int = 50):
    """委托查询"""
    store = _state.get("store")
    if not store or not store._conn:
        raise HTTPException(503, "Store 未初始化")
    rows = store._conn.execute(
        "SELECT * FROM live_orders ORDER BY created_at DESC LIMIT ?", [limit]
    ).fetchall()
    cols = [d[0] for d in store._conn.description]
    return [dict(zip(cols, r)) for r in rows]


@router.get("/live/deals")
async def deals(limit: int = 50):
    """成交查询(每条附 entry_date/hold_days,便于前端展示持仓天数)
    hold_days 算法:买入后第二天起算第1天(交易日计数)
    """
    store = _state.get("store")
    if not store:
        return []
    deals = store.get_deals(limit=limit)
    if not deals:
        return deals
    from datetime import date as _date, datetime as _dt
    from app.api.sim_trader import _load_trading_calendar
    cal = _load_trading_calendar() or set()
    today = _date.today()

    def _calc_hold(entry_d: _date) -> int:
        # 买入后第二天起算第1天:只算 entry_d 之后的交易日
        if not cal:
            return max(0, (today - entry_d).days - 1) if entry_d else 0
        window = sorted(d for d in cal if entry_d < d <= today)
        return len(window)

    # 一次性批量查 entry_date(避免逐只 SQL)
    entry_map = {}
    try:
        rows = store._conn.execute(
            "SELECT code, entry_date FROM live_positions"
        ).fetchall()
        entry_map = {r[0]: r[1] for r in rows if r[1]}
    except Exception:
        pass
    for d in deals:
        entry = entry_map.get(d.get("code"))
        if entry is None:
            # 成交记录里若有 traded_at 兜底(买入成交)
            ta = d.get("traded_at")
            try:
                if isinstance(ta, str):
                    entry = _dt.fromisoformat(ta).date()
                elif isinstance(ta, _dt):
                    entry = ta.date()
            except Exception:
                entry = None
        if entry:
            d["entry_date"] = entry.isoformat()
            d["hold_days"] = _calc_hold(entry)
        else:
            d["entry_date"] = None
            d["hold_days"] = 0
    return deals


@router.get("/live/quotes")
async def quotes(codes: str):
    """行情查询(API 服务端 qmt_gateway 调用,替代旧 qmt_proxy:8081)"""
    qmt = _state.get("qmt")
    if not qmt:
        raise HTTPException(503, "QMT 未初始化")
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    return qmt.get_realtime_quotes(code_list)


@router.get("/live/equity")
async def get_equity(days: int = 1):
    """净值曲线数据(从 live_assets_backup 快照)。

    days<=1: 最近交易日盘中分时(09:25-15:05);days>=2: 每日 EOD 点。
    聚合逻辑见 store.get_equity_points(2026-07-16 修:按档聚合+过滤 stray 点)。
    """
    store = _state.get("store")
    if not store:
        raise HTTPException(503, "未初始化")
    try:
        pts = store.get_equity_points(int(days))
        return {"points": pts}
    except Exception as e:
        logger.error(f"净值查询失败: {e}")
        return {"points": [], "error": str(e)}


@router.get("/live/stocklist")
async def stocklist(details: bool = False, codes: str = ""):
    """获取 QMT 全市场股票列表(替代 qmt_proxy /api/stocklist)"""
    qmt = _state.get("qmt")
    if not qmt:
        raise HTTPException(503, "QMT 未初始化")

    try:
        markets = ['上证A股', '深证A股']
        try:
            test_codes = qmt.get_stock_list_in_sector('北证A股')
            if test_codes:
                markets.append('北证A股')
        except Exception:
            pass

        all_codes = []
        for m in markets:
            sector_codes = qmt.get_stock_list_in_sector(m)
            if sector_codes:
                all_codes.extend(sector_codes)
        all_codes = sorted(set(all_codes))
    except Exception as e:
        logger.error(f"获取QMT股票列表失败: {e}")
        return {"status": "error", "message": str(e)}

    if not details:
        return {"status": "ok", "count": len(all_codes), "codes": all_codes}

    # 筛选特定代码(增量详情查询)
    target = [c.strip() for c in codes.split(",") if c.strip()] if codes else all_codes

    stocks = []
    for code in target:
        d = qmt.get_instrument_detail(code)
        open_date = d.get("OpenDate", "")
        if open_date:
            od_str = str(open_date)
            open_date = f"{od_str[:4]}-{od_str[4:6]}-{od_str[6:]}" if len(od_str) == 8 else ""
        stocks.append({
            "code": code,
            "name": d.get("InstrumentName", ""),
            "sector": d.get("ProductName", ""),
            "list_date": open_date,
        })

    return {"status": "ok", "count": len(stocks), "stocks": stocks}


@router.get("/live/index/members")
async def index_members(index: str = "沪深300"):
    """获取指定指数的成分股列表(替代 qmt_proxy /api/index/members)"""
    qmt = _state.get("qmt")
    if not qmt:
        raise HTTPException(503, "QMT 未初始化")

    try:
        codes = qmt.get_stock_list_in_sector(index)
        if not codes:
            return {"status": "ok", "index": index, "count": 0, "codes": [], "stocks": []}

        codes = sorted(set(codes))
        stocks = []
        for c in codes:
            d = qmt.get_instrument_detail(c)
            stocks.append({
                "code": c,
                "name": d.get("InstrumentName", "") if d else "",
            })

        logger.info(f"index_members | {index}: 返回 {len(codes)} 只成分股")
        return {
            "status": "ok",
            "index": index,
            "count": len(codes),
            "codes": codes,
            "stocks": stocks,
        }
    except Exception as e:
        logger.error(f"获取指数 {index} 成分股失败: {e}")
        return {"status": "error", "index": index, "message": str(e)}
