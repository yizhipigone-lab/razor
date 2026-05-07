import pandas as pd
from fastapi import APIRouter
from database.duckdb_manager import db
from core.logger import get_logger
from server.websocket.manager import manager
from app.api.trade import QuotesPushReq
from pypinyin import lazy_pinyin, Style

log = get_logger("API-Market")
router = APIRouter(tags=["Market"])

@router.get("/api/stock/search")
async def api_stock_search(q: str = ""):
    """提供模糊搜索与简易拼音匹配"""
    q = q.strip()
    if not q: return []
    ALIASES = {
        "BYD": "002594.SZ", "GZMT": "600519.SH", "HS": "000001.SH",
        "SZ": "399001.SZ", "CY": "399006.SZ", "GZJT": "601238.SH",
    }
    INDEX_LIST = [
        {"code": "000001.SH", "name": "上证指数", "exchange": "SH", "sector": "指数板块"},
        {"code": "000016.SH", "name": "上证50",   "exchange": "SH", "sector": "指数板块"},
        {"code": "000010.SH", "name": "上证180",  "exchange": "SH", "sector": "指数板块"},
        {"code": "000009.SH", "name": "上证380",  "exchange": "SH", "sector": "指数板块"},
        {"code": "000300.SH", "name": "沪深300",  "exchange": "SH", "sector": "指数板块"},
        {"code": "000905.SH", "name": "中证500",  "exchange": "SH", "sector": "指数板块"},
        {"code": "000852.SH", "name": "中证1000", "exchange": "SH", "sector": "指数板块"},
        {"code": "000510.SH", "name": "中证A500", "exchange": "SH", "sector": "指数板块"},
        {"code": "399001.SZ", "name": "深证成指", "exchange": "SZ", "sector": "指数板块"},
        {"code": "399004.SZ", "name": "深证100",  "exchange": "SZ", "sector": "指数板块"},
        {"code": "399009.SZ", "name": "深证200",  "exchange": "SZ", "sector": "指数板块"},
        {"code": "399007.SZ", "name": "深证300",  "exchange": "SZ", "sector": "指数板块"},
        {"code": "000688.SH", "name": "科创50",   "exchange": "SH", "sector": "指数板块"},
        {"code": "399006.SZ", "name": "创业板指", "exchange": "SZ", "sector": "指数板块"},
    ]
    q_upper = q.upper(); results = []

    def get_py_initials(text: str) -> str:
        """取中文拼音首字母串, e.g. '赛意信息' → 'SYXX'"""
        return ''.join(p[0].upper() for p in lazy_pinyin(text, style=Style.FIRST_LETTER) if p)

    def match_p(name, query):
        """多字拼音首字母匹配: query='SYXX', name='赛意信息' → 匹对名字各字拼音首字母"""
        if not query.isalpha(): return False
        initials = get_py_initials(name)
        return initials.startswith(query.upper())

    # 别名优先级最高
    if q_upper in ALIASES:
        c = ALIASES[q_upper]
        if any(i["code"]==c for i in INDEX_LIST):
            results.extend([i for i in INDEX_LIST if i["code"]==c])
        else:
            row = db.conn.execute("SELECT code, name, sector FROM stocks WHERE code=?", [c[:6]]).fetchone()
            if row: results.append({"code": c, "name": row[1], "exchange": c[-2:], "sector": row[2] or "A股"})

    # 匹配指数
    for idx in INDEX_LIST:
        if idx not in results and (q_upper in idx["code"] or q in idx["name"]): results.append(idx)

    # 3. 内存匹配 (解决 LIKE 无法实现拼音搜索的问题)
    try:
        all_stocks = db.conn.execute("SELECT code, name, sector, exchange FROM stocks WHERE status='active'").df()
        for _, row in all_stocks.iterrows():
            nm = str(row["name"]); co = str(row["code"]).strip()
            full_co = f"{co}.SH" if co.startswith("6") else f"{co}.SZ"

            # 命中逻辑：代码前缀、名称包含、拼音首字母匹配
            hit = (q in co) or (q in nm) or (q.isalpha() and match_p(nm, q))
            if hit and not any(r["code"] == full_co for r in results):
                results.append({
                    "code": full_co, "name": nm, "exchange": str(row["exchange"]), "sector": str(row["sector"] or "A股")
                })
                if len(results) > 25: break
    except Exception as e:
        log.warning(f"Search Engine Error: {e}")

    return results[:25]

@router.get("/api/stocks")
async def get_stocks(exchange: str = None, sector: str = None):
    stocks = db.get_all_stocks()
    if exchange:
        stocks = stocks[stocks["exchange"] == exchange]
    if sector:
        stocks = stocks[stocks["sector"].str.contains(sector, na=False)]
    return stocks.to_dict(orient="records")


@router.get("/api/meta/stocks/search")
async def search_stocks(query: str = ""):
    if not query:
        return {"status": "ok", "data": []}
    wildcard = f"%{query}%"
    sql = "SELECT code, name, sector FROM stocks WHERE code LIKE ? OR name LIKE ? OR sector LIKE ? LIMIT 50"
    df = db.conn.execute(sql, [wildcard, wildcard, wildcard]).df()
    return {"status": "ok", "data": df.fillna("").to_dict(orient="records")}

@router.get("/api/meta/stocks/name/{code}")
async def get_stock_name(code: str):
    """根据股票代码获取股票简称"""
    code = code.replace(".SH", "").replace(".SZ", "").strip()
    try:
        result = db.conn.execute("SELECT name FROM stocks WHERE code = ?", [code]).fetchone()
        name = result[0] if result else code
        return {"status": "ok", "name": name}
    except Exception as e:
        log.error(f"Failed to get stock name for code {code}: {e}")
        return {"status": "error", "name": code}

@router.get("/api/meta/sectors/hierarchy")
async def get_sector_hierarchy():
    """获取分级行业树 (门类 -> 大类)"""
    try:
        # 门类映射
        TYPE_MAP = {
            'A': '农、林、牧、渔业', 'B': '采矿业', 'C': '制造业', 
            'D': '电力、热力、燃气及水生产和供应业', 'E': '建筑业',
            'F': '批发和零售业', 'G': '交通运输、仓储和邮政业', 'H': '住宿和餐饮业',
            'I': '信息传输、软件和信息技术服务业', 'J': '金融业', 'K': '房地产业',
            'L': '租赁和商务服务业', 'M': '科学研究和技术服务业', 'N': '水利、环境和公共设施管理业',
            'O': '居民服务、修理和其他服务业', 'P': '教育', 'Q': '卫生和社会工作',
            'R': '文化、体育和娱乐业', 'S': '综合'
        }
        
        # 改进归类逻辑：参照申万一级行业口径扩容关键词
        df = db.conn.execute("SELECT DISTINCT sector FROM stocks WHERE sector IS NOT NULL").df()
        raw_sectors = sorted([s for s in df['sector'].tolist() if s])
        
        keywords = {
            '工业制造/重工': ['制造', '工业', '生产', '机械', '设备', '仪器', '仪表', '泵', '阀', '机电', '重工', '紧固件'],
            '医药生物/健康': ['医药', '医疗', '保健', '生物', '药', '诊', '医院', '疫苗', '器械'],
            '信息技术/芯片': ['软件', '信息', '互联网', 'IT', '计算机', '通信', '芯片', '半导体', '集成电路', '电子'],
            '汽车/零部件': ['汽车', '零部件', '车桥', '轮胎', '车辆'],
            '金融/证券/保险': ['银行', '保险', '金融', '证券', '信托', '期货'],
            '消费/商贸/旅游': ['零售', '百货', '餐饮', '旅游', '食品', '乳制', '白酒', '饮料', '超市', '家电', '纺织', '服装'],
            '能源/资源/化工': ['采矿', '石油', '煤炭', '有色', '化工', '钢铁', '贵金属', '石化'],
            '公用事业/环保': ['环保', '电力', '燃气', '水务', '光伏', '风能', '新能源', '回收'],
            '房产/建材/建筑': ['地产', '房产', '建筑', '装饰', '建材', '水泥', '玻璃'],
            '农林牧渔': ['农', '林', '牧', '渔', '饲料', '种植', '养殖']
        }

        tree = {}
        for s in raw_sectors:
            found = False
            # 1. 尝试按预设关键词归类
            for cat_name, keys in keywords.items():
                if any(k in s for k in keys):
                    if cat_name not in tree: tree[cat_name] = set()
                    tree[cat_name].add(s)
                    found = True
                    break
            
            # 2. 如果没匹配到，按原有的 A-S 逻辑或首字
            if not found:
                cat_char = s[0].upper()
                cat_name = TYPE_MAP.get(cat_char, "其他行业")
                if cat_name not in tree: tree[cat_name] = set()
                tree[cat_name].add(s)

        # 转换为前端级联格式
        result = []
        for cat_name, subs in tree.items():
            children = [{"value": sub, "label": sub} for sub in sorted(list(subs))]
            result.append({
                "value": f"CAT:{cat_name}",
                "label": cat_name,
                "children": children
            })
            
        return sorted(result, key=lambda x: x['label'])
    except Exception as e:
        log.error(f"构建行业树失败: {e}")
        return []



@router.post("/api/internal/quotes_push")
async def quotes_push_webhook(req: QuotesPushReq):
    """接收外部 QMT Proxy 的极速推送行情数据包并群发"""
    try:
        await manager.broadcast({
            "type": req.type,
            "data": req.data
        })
        return {"status": "ok"}
    except Exception as e:
        log.error(f"处理行情极速推送错误: {e}")
        return {"status": "error"}

# ─── 实时行情 API ─────────────────────────────────────────────
@router.get("/api/market/quotes")
async def get_quotes(codes: str = ""):
    from app.data_manager.engine import get_realtime_quote, get_index_realtime
    try:
        code_list = [c for c in codes.split(",") if c.strip()]
        df = get_realtime_quote(code_list) if code_list else pd.DataFrame()
        indices = get_index_realtime()
        return {
            "quotes": df.to_dict(orient="records") if not df.empty else [],
            "indices": indices if indices else {}
        }
    except Exception as e:
        log.error(f"行情接口崩溃: {e}")
        return {"quotes": [], "indices": {}, "error": str(e)}

@router.get("/api/market/sectors")
async def get_sectors():
    """获取行业板块热度排名（从热点板块引擎读取）"""
    try:
        from app.hot_sector.engine import hot_sector_engine
        df = hot_sector_engine.get_top_sectors(limit=30)
        return df.to_dict(orient="records") if not df.empty else []
    except Exception as e:
        log.error(f"获取板块热度失败: {e}")
        return []



# ─── 监控控制 API ─────────────────────────────────────────────

