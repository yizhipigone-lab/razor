# -*- coding: utf-8 -*-
import pandas as pd
import time
import json
import requests
import baostock as bs
from datetime import datetime, timedelta, date
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pytdx2.hq import TdxHq_API

from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db

log = get_logger("DataManager")

# 全球最快通达信主站 (用户验证可用)
TDX_SERVER = ("180.153.18.170", 7709)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

def get_all_stock_list() -> pd.DataFrame:
    """获取全市场股票列表 (Tushare 优先, TDX 备份)"""
    import os
    import tushare as ts
    ts_key = os.getenv("TUSHARE_KEY")
    if ts_key:
        try:
            ts.set_token(ts_key)
            pro = ts.pro_api()
            df = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,industry')
            if not df.empty:
                stocks = []
                for _, row in df.iterrows():
                    c = row['symbol']
                    if c.startswith(('6', '0', '3')):
                        stocks.append({'code': c, 'name': row['name'], 'sector': row['industry']})
                if stocks:
                    df_out = pd.DataFrame(stocks)
                    db.update_stock_list(df_out)
                    log.info("从 Tushare 获取全市场股票及行业分布成功")
                    return df_out
        except Exception as e:
            log.warning(f"Tushare 获取股票列表失败: {e}，回退到 TDX")

    try:
        from pytdx2.hq import TdxHq_API
        api = TdxHq_API()
        if api.connect(*TDX_SERVER, time_out=3):
            stocks = []
            # 沪深市场扫描 (市场 0:SZ, 1:SH)
            for m in [0, 1]:
                count = api.get_security_count(m)
                for i in range(0, count, 1000):
                    data = api.get_security_list(m, i)
                    if data:
                        for s in data:
                            # 过滤非股票 (简单规则: 6/600/000/002/300)
                            c = s['code']
                            if c.startswith(('6', '0', '3')):
                                stocks.append({'code': c, 'name': s['name']})
            api.disconnect()
            if stocks:
                df = pd.DataFrame(stocks)
                # 入库
                db.update_stock_list(df)
                return df
    except Exception as e:
        log.warning(f"TDX 股票列表扫描失败: {e},回退到 DB")

    # 兜底从 DB 取
    return db.get_all_stocks()

def download_daily_bars(code: str, years: int = 1) -> pd.DataFrame:
    """下载日线 K 线 (Tushare 优先，然后通达信TCP极速专线)"""
    import os
    try:
        import tushare as ts
        ts_key = os.getenv("TUSHARE_KEY")
        if ts_key:
            ts.set_token(ts_key)
            ts_code = f"{code}.{'SH' if str(code).startswith('6') else 'SZ'}"
            # pro_bar 处理了前复权，非常适合回测及选股
            # 获取最近的数据
            df = ts.pro_bar(ts_code=ts_code, adj='qfq')
            if df is not None and not df.empty:
                df = df.head(years * 250)
                df = df.rename(columns={'trade_date': 'date', 'vol': 'volume'})
                df['date'] = pd.to_datetime(df['date'])
                # Tushare amount 字段单位是元，无需转换（#7 修复）
                # 数据-C3: pro_bar(qfq) 已是前复权价, 标记 adj_factor=1.0 防读取层二次复权
                df['adj_factor'] = 1.0
                df = df[['date','open','high','low','close','volume','amount','adj_factor']].sort_values('date')
                return df
    except Exception as e:
        log.debug(f"Tushare D1 {code} 失败: {e}，尝试使用 TDX 兜底")

    api = TdxHq_API()
    try:
        if api.connect(*TDX_SERVER, time_out=3):
            market = 1 if code.startswith('6') else 0
            # 获取最近 800 根 (~3 年)
            data = api.get_security_bars(4, market, code, 0, 800)  # category=4 前复权日线
            api.disconnect()
            if data:
                df = api.to_df(data)
                df = df.rename(columns={'datetime': 'date', 'vol': 'volume'})
                df['date'] = pd.to_datetime(df['date'])
                # 通达信金额在有的主站不显示，用估算法
                df['amount'] = df['close'] * df['volume'] * 100
                df['adj_factor'] = 1.0  # 数据-C3: TDX category=4 已前复权
                return df[['date','open','high','low','close','volume','amount','adj_factor']]
    except Exception as e:
        log.debug(f"TDX D1 {code} 失败: {e}")
    
    # 模拟腾讯备份 (HTTP 80)
    try:
        full_code = f"{'sh' if code.startswith('6') else 'sz'}{code}"
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param={full_code},day,,,320,qfq"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            content = resp.text.split('=', 1)[-1]
            raw = json.loads(content)
            k_list = raw.get('data', {}).get(full_code, {}).get('qfqday') or raw.get('data', {}).get(full_code, {}).get('day')
            if k_list:
                df = pd.DataFrame(k_list, columns=['date','open','close','high','low','volume','ext'])
                df['date'] = pd.to_datetime(df['date'])
                df['amount'] = df['close'].astype(float) * df['volume'].astype(float) * 100
                df['adj_factor'] = 1.0  # 数据-C3: 腾讯 qfqday 已前复权
                return df[['date','open', 'high', 'low', 'close', 'volume', 'amount', 'adj_factor']]
    except Exception:
        log.warning(f"腾讯 HTTP 日线下载失败 {code}")
    return None

def download_min5_bars(code: str, count: int = 800) -> pd.DataFrame:
    """下载 5 分钟 K 线 (Tushare优先，然后通达信TCP极速专线)"""
    import os
    try:
        import tushare as ts
        ts_key = os.getenv("TUSHARE_KEY")
        if ts_key:
            ts.set_token(ts_key)
            ts_code = f"{code}.{'SH' if str(code).startswith('6') else 'SZ'}"
            # Tushare 获取分钟线
            df = ts.pro_bar(ts_code=ts_code, freq='5min', adj='qfq')
            if df is not None and not df.empty:
                df = df.head(count)
                # 数据-C1: min5 统一输出 datetime 列(load_all_bars 对 min5 读 datetime)。
                # 原双键 rename {'trade_time':'date','trade_time':'datetime'} 第一键被pandas静默丢弃。
                df = df.rename(columns={'trade_time': 'datetime', 'vol': 'volume'})
                df['datetime'] = pd.to_datetime(df['datetime'])
                # Tushare amount 字段单位是元，无需转换
                df = df[['datetime', 'open', 'high', 'low', 'close', 'volume', 'amount']].sort_values('datetime')
                return df
    except Exception as e:
        log.debug(f"Tushare M5 {code} 失败: {e}，尝试使用 TDX兜底")

    api = TdxHq_API()
    try:
        if api.connect(*TDX_SERVER, time_out=3):
            market = 1 if code.startswith('6') else 0
            # 获取指定根数 (category=0 为 5分钟)
            data = api.get_security_bars(0, market, code, 0, count)
            api.disconnect()
            if data:
                df = api.to_df(data)
                # 数据-C1: TDX 兜底路径同样统一输出 datetime 列
                df = df.rename(columns={'datetime': 'datetime', 'vol': 'volume'})
                df['datetime'] = pd.to_datetime(df['datetime'])
                df['amount'] = df['close'] * df['volume'] * 100
                return df[['datetime', 'open', 'high', 'low', 'close', 'volume', 'amount']]
    except Exception:
        log.warning(f"TDX 5分钟线下载失败 {code}")
    return None

def download_bars(code: str, freq: str, count: int) -> pd.DataFrame:
    """通用 K 线下载器"""
    if freq == "daily":
        return download_daily_bars(code, count)
    elif freq == "min5":
        return download_min5_bars(code, count)
    else:
        log.error(f"不支持的 K 线频率: {freq}")
        return None

def update_sectors_from_baostock():
    """从 BaoStock 补全行业信息 (全量对齐)"""
    import baostock as bs
    log.info("📡 正在通过 BaoStock 接口补全行业元数据...")
    bs.login()
    rs = bs.query_stock_industry()
    industry_list = []
    while (rs.error_code == '0') & rs.next():
        industry_list.append(rs.get_row_data())
    bs.logout()
    
    if industry_list:
        df = pd.DataFrame(industry_list, columns=rs.fields)
        # 转换代码格式 sh.600000 -> 600000
        df['code'] = df['code'].apply(lambda x: x.split('.')[1])
        db.update_stock_sectors(df[['code', 'industry']])
        log.info(f"✅ 行业信息补全完成，共处理 {len(df)} 条。")

def get_realtime_quote(code_list: list) -> pd.DataFrame:
    """[极速重构] 批量获取实时行情快照 (优先尝试 QMT Proxy，完全废弃 Tushare 轮询)"""
    from core.settings import settings
    
    # 强制路由到 QMT 代理 (劫持原有的 Tushare/TDX 流程)
    if settings.get("gateway", "active_gateway") == "qmt":
        try:
            from app.trader.gateways.qmt import qmt_gateway
            quotes = qmt_gateway.get_realtime_quotes(code_list)
            if quotes:
                results = []
                for code, q in quotes.items():
                    price = float(q.get('lastPrice', 0))
                    # 昨收价核心修正：尝试 lastClose 和 preClose。由于刚开盘时 lastClose 可能为 0
                    last_close = float(q.get('lastClose', 0) or q.get('preClose', 0))
                    # 兼容性字段补齐 (保持跟旧版 DataFrame 结构一致以支持前端显示)
                    results.append({
                        "code": code,
                        "price": price,
                        "open": float(q.get('open', price)),
                        "high": float(q.get('high', price)),
                        "low": float(q.get('low', price)),
                        "volume": float(q.get('volume', 0)),
                        "amount": float(q.get('amount', 0)),
                        "last_close": last_close if last_close > 0 else price,
                        "change_pct": round((price - last_close) / last_close * 100, 2) if last_close > 0 else 0
                    })
                return pd.DataFrame(results)
        except Exception as e:
            log.debug(f"尝试劫持 QMT 行情快照失败(可能是未开市或Proxy未响应): {e}")

    # 万一 QMT 挂了，极简回退到 TDX 高速通道 (作为最后一道物理防线，不调 Tushare)
    from pytdx2.hq import TdxHq_API
    api = TdxHq_API()
    try:
        TDX_SERVER = ('119.147.212.81', 7709)
        if api.connect(*TDX_SERVER, time_out=2):
            results = []
            for i in range(0, len(code_list), 80):
                batch_codes = code_list[i : i + 80]
                tdx_queries = []
                tdx_to_orig = {}
                for c in batch_codes:
                    c_str = str(c)
                    if '.' in c_str:
                        parts = c_str.split('.')
                        clean_code = parts[0]
                        # 优先用后缀判断市场，避免 000858.SZ 被误判为沪市
                        suffix = parts[1].upper()
                        market = 1 if suffix == 'SH' else 0
                    else:
                        clean_code = c_str
                        # 无后缀时用前缀推断：6xx/000(指数)→沪市，其余→深市
                        market = 1 if (clean_code.startswith('6') or
                                       (clean_code.startswith('000') and len(clean_code) <= 6)) else 0
                    tdx_queries.append((market, clean_code))
                    tdx_to_orig[clean_code] = c_str
                
                quotes = api.get_security_quotes(tdx_queries)
                if quotes:
                    for q in quotes:
                        orig = tdx_to_orig.get(q['code'], q['code'])
                        price = float(q['price']) if q['price'] > 0 else float(q['last_close'])
                        # 优先取 lastClose, 没值取 preClose (QMT 大盘指数常用), 再没值取 price (涨幅为0)
                        lc = q.get('last_close', 0) or q.get('pre_close', 0)
                        results.append({
                            "code": orig,
                            "price": price,
                            "open": float(q.get('open', price)),
                            "high": float(q.get('high', price)),
                            "low": float(q.get('low', price)),
                            "volume": float(q.get('vol', 0)),
                            "amount": float(q.get('amount', 0)),
                            "last_close": float(lc if lc > 0 else price)
                        })
            api.disconnect()
            return pd.DataFrame(results)
    except Exception:
        log.warning("TDX 实时行情获取失败，回退到腾讯 HTTP")

    # 腾讯 HTTP 极速通道 (终极防线)
    try:
        tenc_codes = []
        for c in code_list:
            clean = str(c).split('.')[0]
            prefix = "sh" if clean.startswith(('6', '000')) else "sz"
            tenc_codes.append(f"s_{prefix}{clean}")

        url = f"http://qt.gtimg.cn/q={','.join(tenc_codes)}"
        resp = requests.get(url, timeout=2)
        if resp.status_code == 200:
            results = []
            lines = resp.text.split(';')
            for line in lines:
                if '~' not in line or '=' not in line: continue
                raw = line.split('=')[1].replace('"', '').strip()
                parts = raw.split('~')
                if len(parts) < 6: continue
                code_raw = parts[2]
                if not code_raw: continue
                # 恢复带后缀的原始 code
                orig = None
                for c in code_list:
                    if c.split('.')[0] == code_raw or c == code_raw:
                        orig = c
                        break
                if not orig:
                    orig = code_raw

                price = float(parts[3])
                chg = float(parts[4])  # 涨跌额
                last_close = price - chg  # 昨收 = 现价 - 涨跌额
                chg_pct = float(parts[5])

                results.append({
                    "code": orig,
                    "price": price,
                    "open": price,
                    "high": price,
                    "low": price,
                    "volume": float(parts[6]) if len(parts) > 6 else 0,
                    "amount": 0,
                    "last_close": last_close if last_close > 0 else price,
                    "change_pct": chg_pct
                })
            if results:
                return pd.DataFrame(results)
    except Exception as e:
        log.debug(f"腾讯 HTTP 回退行情失败: {e}")

    return pd.DataFrame()

def get_all_market_quotes() -> list:
    """拉取全市场实时快照 (用于选股缝合)"""
    stocks = db.get_all_stocks()
    if stocks.empty: return []
    codes = stocks["code"].tolist()
    df = get_realtime_quote(codes)
    return df.to_dict(orient="records") if not df.empty else []

def get_index_realtime() -> dict:
    """获取大盘核心指数实时行情（QMT Proxy 优先）"""
    TARGET_CODES = ['000001.SH', '399001.SZ', '399006.SZ', '000905.SH', '000510.SH']
    try:
        df = get_realtime_quote(TARGET_CODES)
        if not df.empty:
            indices = {}
            for _, row in df.iterrows():
                code = row['code']
                price = float(row['price'])
                lc = float(row.get('last_close', price))
                indices[code] = {
                    "price": price,
                    "change_pct": round((price - lc) / lc * 100, 2) if lc > 0 else 0,
                    "pre_close": lc,
                }
            return indices
    except Exception as e:
        log.warning(f"QMT 指数实时行情获取失败: {e}")
    return {}

def batch_download_all(freq: str = "daily", years: int = 1, mode: str = "incremental", custom_start: str = None, custom_end: str = None, progress_cb=None):
    """全量/增量分工指挥中心"""
    log.info(f"开启同步任务 | 模式: {mode} | 频率: {freq}")
    
    # [改造点] 1. 基础信息全量覆盖 (调用 Tushare Sync)
    from app.data_manager.tushare_sync import tushare_sync_manager
    if progress_cb: progress_cb(5, 100, "🌍 正在全量更新公司字典与基础元数据...")
    
    tushare_sync_manager.sync_stock_basic()
    
    if progress_cb: progress_cb(10, 100, "📊 正在拉取每日最新财务/估值基本面库...")
    tushare_sync_manager.sync_fundamentals_snapshot(progress_cb=progress_cb)
    
    # [改造点] 2. 如果是日线同步，移交至纯纯的高速 Parquet 批读取管线
    if freq == "daily":
        from app.data_manager.parquet_pipeline import parquet_pipeline
        if progress_cb: progress_cb(60, 100, "🚀 切换至极限 Parquet 管道，正在映射日线数据块...")
        
        # 注入用户在界面指定的自定义提取日期
        end_date = custom_end if custom_end else datetime.now().strftime('%Y%m%d')
        start_date = custom_start if custom_start else (datetime.now() - pd.Timedelta(days=250 * years if mode == 'full' else 7)).strftime('%Y%m%d')
        
        success = parquet_pipeline.sync_daily_klinesto_parquet(start_date=start_date, end_date=end_date, progress_cb=progress_cb)
        if success:
            if progress_cb: progress_cb(100, 100, f"🎉 {mode.upper()} 同步圆满结束！视图映射已重构！")
            return
    else:
        # 5分钟数据同步 —— 先并行拉取，再批量写入
        if progress_cb: progress_cb(20, 100, "正在扫描 5分钟 候选名单...")
        stocks_df = db.get_all_stocks()
        codes = stocks_df["code"].tolist() if not stocks_df.empty else []
        total_count = len(codes)

        _results_lock = threading.Lock()
        results = {}  # {code: DataFrame}

        def _worker(c):
            try:
                if mode == "incremental":
                    last_dt = db.get_last_date(c, freq)
                    if last_dt:
                        # count=80 覆盖至少一个交易日（240分钟/5=48根，冗余防假期）
                        df = download_bars(c, freq, count=max(80, (date.today() - last_dt).days * 50))
                        if df is not None and not df.empty:
                            # 数据-C1: daily 用 date 列, min5 用 datetime 列(统一后)
                            _tcol = 'date' if freq == 'daily' else 'datetime'
                            if _tcol in df.columns:
                                df = df[pd.to_datetime(df[_tcol]) > pd.to_datetime(last_dt)]
                            if not df.empty:
                                with _results_lock:
                                    results[c] = df
                        return True

                count_to_get = 1000
                df_full = download_bars(c, freq, count=count_to_get)
                if df_full is not None and not df_full.empty:
                    with _results_lock:
                        results[c] = df_full
                    return True
                return False
            except Exception as ex:
                log.error(f"线程任务失败 {c}: {ex}")
                return False

        done_count = 0
        with ThreadPoolExecutor(max_workers=20) as executor:
            future_to_code = {executor.submit(_worker, c): c for c in codes}
            for future in as_completed(future_to_code):
                done_count += 1
                if done_count % 50 == 0 or done_count == total_count:
                    pct = round(done_count / total_count * 100, 1)
                    if progress_cb:
                        progress_cb(done_count, total_count, f"拉取进度: {done_count}/{total_count} ({pct}%)")

        # 批量写入 Parquet —— 每只股票一次 I/O
        if results:
            log.info(f"开始批量写入 {len(results)} 只股票的 {freq} 数据...")
            if progress_cb:
                progress_cb(90, 100, f"正在批量写入 {len(results)} 只股票的 Parquet...")
            db.batch_save_bars(results, freq=freq)
            db.create_kline_view(freq=freq)

        log.info(f"同步完成。成功: {len(results)}/{total_count}")
        if progress_cb:
            progress_cb(100, 100, f"同步完成！获取 {len(results)} 只，共 {total_count} 只")
