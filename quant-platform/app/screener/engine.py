import importlib
import pandas as pd
from dataclasses import dataclass
from datetime import date, timedelta, datetime
from pathlib import Path
from typing import Optional, List
import threading
from multiprocessing import cpu_count

from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db
from app.screener.strategies.base import BaseStrategy

log = get_logger("Screener")

def _scan_worker(strategy_module_path, strategy_params, codes, freq, start, end, target_start, target_end, live_quotes=None, market_df=None, all_stock_df=None):
    """工作者：独立加载一部分股票的 parquet -> 策略计算 -> 过滤信号"""
    import duckdb
    import importlib
    import sys
    from pathlib import Path
    import pandas as pd

    root_dir = str(Path(__file__).resolve().parent.parent.parent)
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)

    from database.duckdb_manager import PARQUET_DAILY_DIR, PARQUET_MIN5_DIR

    try:
        conn = duckdb.connect(":memory:")
        base_dir = PARQUET_DAILY_DIR if freq == "daily" else PARQUET_MIN5_DIR
        date_col = "date" if freq == "daily" else "datetime"

        file_paths = [str(base_dir / f"{c}.parquet").replace("\\", "/") for c in codes if (base_dir / f"{c}.parquet").exists()]
        if not file_paths:
            return pd.DataFrame()

        sql = f"SELECT filename, * FROM read_parquet({str(file_paths)}, filename=true, union_by_name=True)"
        params = []
        where_clauses = []
        if start:
            where_clauses.append(f"{date_col} >= ?")
            params.append(start.isoformat())
        if end:
            where_clauses.append(f"{date_col} <= ?")
            params.append(end.isoformat())
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        bars = conn.execute(sql, params).df()
        if bars.empty:
            return pd.DataFrame()

        bars["code"] = bars["filename"].str.extract(r"([^\\/]+)\.parquet$")
        bars = bars.drop(columns=["filename"])

        # 注入实时行情
        if live_quotes:
            import datetime as dtmod
            today_str = dtmod.date.today().isoformat()
            new_rows = []
            grouped = bars.groupby("code")
            for code, group in grouped:
                if code in live_quotes:
                    q = live_quotes[code]
                    if q and q.get('lastPrice', 0) > 0:
                        row = group.iloc[-1].copy()
                        row[date_col] = pd.to_datetime(today_str)
                        row['close'] = q['lastPrice']
                        row['open'] = q.get('open', row['close'])
                        row['high'] = q.get('high', row['close'])
                        row['low'] = q.get('low', row['close'])
                        row['vol'] = q.get('volume', 0)
                        row['amount'] = q.get('amount', 0)
                        new_rows.append(row)
            if new_rows:
                ndf = pd.DataFrame(new_rows)
                bars = bars[bars[date_col].dt.date != dtmod.date.today()]
                bars = pd.concat([bars, ndf], ignore_index=True).sort_values(by=["code", date_col])

        # 加载并执行策略
        mod = importlib.import_module(strategy_module_path)
        from app.screener.engine import FunctionalStrategyWrapper, BaseStrategy

        strategy_obj = None
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if isinstance(attr, type) and issubclass(attr, BaseStrategy) and attr is not BaseStrategy:
                strategy_obj = attr(params=strategy_params)
                break

        if not strategy_obj:
            if hasattr(mod, 'signal'):
                strategy_obj = FunctionalStrategyWrapper(mod.signal, params=strategy_params)
            elif hasattr(mod, 'generate_signals'):
                strategy_obj = FunctionalStrategyWrapper(mod.generate_signals, params=strategy_params)

        if not strategy_obj:
            return pd.DataFrame()

        # 候选⑤:统一前置过滤(ST/北交所/停牌/涨停) — class 走 self.preprocess(尊重 self.LIMIT_TABLE 覆盖);
        # function 模式无 self,走自由 preprocess_bars(用 base 默认 limit_table)
        if hasattr(strategy_obj, "preprocess"):
            bars = strategy_obj.preprocess(bars)
        else:
            from app.screener.strategies.base import preprocess_bars
            bars = preprocess_bars(bars, getattr(strategy_obj, "params", {}) or {})

        if hasattr(strategy_obj, 'generate_signals'):
            import inspect
            sig = inspect.signature(strategy_obj.generate_signals)
            kwargs = {}
            if 'market_df' in sig.parameters: kwargs['market_df'] = market_df
            if 'all_stock_df' in sig.parameters: kwargs['all_stock_df'] = all_stock_df
            signals = strategy_obj.generate_signals(bars, **kwargs)
        else:
            signals = strategy_obj.generate_signals(bars)

        if signals is not None and not signals.empty:
            signals[date_col] = pd.to_datetime(signals[date_col])
            mask = (signals[date_col].dt.date >= target_start) & (signals[date_col].dt.date <= target_end)
            filtered = signals[mask]
            return filtered
    except Exception as e:
        print(f"Worker Error: {e}")
        import traceback
        traceback.print_exc()
    return pd.DataFrame()

def get_strategy_info(name: str):
    """主进程调用：获取策略的模块路径"""
    df = db.get_strategies(active_only=True)
    row = df[df['name'] == name]
    if not row.empty:
        p = Path(row.iloc[0]['code_path'])
        parts = p.parts
        if "app" in parts:
            idx = parts.index("app")
            return ".".join(parts[idx:]).replace(".py", "")
    
    builtin_map = {
        "MA金叉": "app.screener.strategies.ma_cross",
        "MACD金叉": "app.screener.strategies.macd",
        "RPS-VCP动量突破": "app.screener.strategies.rps_vcp",
        "连涨四天+水下MACD金叉": "app.screener.strategies.连续上涨_macd水下金叉",
        "快MACD+KDJ双金叉": "app.screener.strategies.快MACD_KDJ双金叉",
    }
    return builtin_map.get(name)

class FunctionalStrategyWrapper(BaseStrategy):
    """函数式策略适配器：将简单的 Python 函数包装成标准的 BaseStrategy 对象"""
    def __init__(self, func, name="FunctionalStrategy", params: dict = None):
        super().__init__(params=params)
        self.func = func
        self.name = name

    def generate_signals(self, bars: pd.DataFrame, market_df: pd.DataFrame = None, all_stock_df: pd.DataFrame = None) -> pd.DataFrame:
        if not self.func: return pd.DataFrame()
        # 探测函数签名，决定传递哪些参数
        import inspect
        sig = inspect.signature(self.func)
        kwargs = {}
        if 'market_df' in sig.parameters: kwargs['market_df'] = market_df
        if 'all_stock_df' in sig.parameters: kwargs['all_stock_df'] = all_stock_df
        
        # 调用原始函数
        try:
            return self.func(bars, **kwargs)
        except Exception as e:
            log.error(f"适配器执行内核报错 [{self.name}]: {e}")
            import traceback
            traceback.print_exc()
            return pd.DataFrame()

def load_strategy(name: str, params: dict = None) -> BaseStrategy:
    """动态加载策略：支持类模式及函数式(RPS等)逻辑全兼容加载"""
    import inspect
    
    # 1. 查找数据库获取路径信息
    df_db = db.get_strategies(active_only=True)
    row = df_db[df_db['name'] == name]
    
    module_path = None
    if not row.empty:
        code_path = row.iloc[0]['code_path']
        p = Path(code_path)
        parts = p.parts
        if "app" in parts:
            idx = parts.index("app")
            module_path = ".".join(parts[idx:]).replace(".py", "")
        elif "core" in parts:
            idx = parts.index("core")
            module_path = ".".join(parts[idx:]).replace(".py", "")
        else:
            # 兜底：如果是相对路径且不在核心目录，尝试拼接
            module_path = f"app.screener.strategies.{p.stem}"
            
    if not module_path:
        # 2. 兼容硬编码的旧路径
        builtin_map = {
            "MA金叉": "app.screener.strategies.ma_cross.MACrossStrategy",
            "MACD金叉": "app.screener.strategies.macd.MACDStrategy",
        }
        class_path = builtin_map.get(name)
        if not class_path:
            raise ValueError(f"未找到策略配置: {name} (请确认已在工厂中保存并处于有效状态)")
        module_path, class_name = class_path.rsplit(".", 1)

    try:
        # H-04 修复：去掉无条件 reload，避免并发扫描时模块状态不一致。
        # 策略代码变更后需重启服务或显式清缓存（_strategy_module_cache）。
        importlib.invalidate_caches()
        mod = importlib.import_module(module_path)

        # --- 探测器集群 A: 寻找类模式 (BaseStrategy 子类) ---
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if inspect.isclass(attr) and issubclass(attr, BaseStrategy) and attr is not BaseStrategy:
                log.info(f"🧬 [Loader] 识别到类继承策略: {attr_name}")
                return attr(params=params)

        # --- 探测器集群 B: 寻找函数模式 (signal 或 generate_signals) ---
        if hasattr(mod, 'signal') and callable(getattr(mod, 'signal')):
            log.info(f"📡 [Loader] 识别到函数式策略: {name}.signal()")
            return FunctionalStrategyWrapper(getattr(mod, 'signal'), name=name, params=params)

        if hasattr(mod, 'generate_signals') and callable(getattr(mod, 'generate_signals')):
            log.info(f"📡 [Loader] 识别到桥接函数: {name}.generate_signals()")
            return FunctionalStrategyWrapper(getattr(mod, 'generate_signals'), name=name, params=params)

        raise ValueError(f"策略文件 [{module_path}] 中未发现有效入口（类或 signal 函数）")

    except Exception as e:
        log.error(f"策略加载硬崩溃 [{name}]: {e}")
        raise e

class ScreenerEngine:
    def run_scan(
        self,
        strategy_name: str,
        strategy_params: dict = None,
        strategy_id: int = None,
        freq: str = "daily",
        exchanges: list = None,
        sectors: list = None,
        hot_sectors_only: bool = False,
        fundamentals_filter: dict = None, # 多因子扩展：前置财务/资金过滤参数
        index_filter: list = None,         # 指数成分过滤 e.g. ['HS300','ZZ500']
        min_mv: float = None,               # 流通市值下限（亿元）
        max_mv: float = None,               # 流通市值上限（亿元）
        start: Optional[date] = None,
        end: Optional[date] = None,
        progress_callback=None,
        stop_event: Optional[threading.Event] = None,
    ) -> list:
        """执行选股扫描（编排器：只负责流程编排，具体逻辑在各子方法中）。"""
        def _progress(step, total, msg):
            if progress_callback: progress_callback(step, total, msg)
            log.info(f"[{step}/{total}] {msg}")

        log.info(f"🔍 [Engine] run_scan 开始执行: {strategy_name}, 频率: {freq}")
        _progress(1, 10, "加载股票元数据...")

        # 1. 股票池过滤（交易所/板块/指数/市值/热点）
        stocks = self._filter_stock_pool(
            exchanges, sectors, index_filter, min_mv, max_mv, hot_sectors_only, _progress
        )
        if stocks is None or stocks.empty:
            log.warning("候选股票列表为空")
            return []
        codes = stocks["code"].tolist()

        # 2. 基本面多因子过滤
        codes = self._apply_fundamentals_filter(codes, fundamentals_filter, strategy_params, _progress)
        if not codes:
            log.warning("经过基本面多因子过滤后候选全量被淘汰")
            return []

        # 3. 计算时间窗口 & 准备策略上下文（签名嗅探 + 大盘/全市场数据预加载）
        _progress(3, 10, f"启动并行引擎 ({cpu_count()} 核心) 加载并扫描 {len(codes)} 只股票...")
        today = date.today()
        calc_start = (start or today) - timedelta(days=365)
        calc_end = end or today
        target_start = start or today
        target_end = end or today

        ctx = self._prepare_strategy_context(strategy_name, strategy_params, freq, calc_start, calc_end, _progress)
        if ctx is None:
            log.error(f"无法定位策略: {strategy_name}")
            return []

        # 4. 实时行情（QMT）
        live_quotes = self._fetch_live_quotes(codes, _progress)

        # 5. 并行扫描
        signal_frames = self._parallel_scan(
            ctx, strategy_params, codes, freq,
            calc_start, calc_end, target_start, target_end, live_quotes, _progress
        )
        if not signal_frames:
            return []

        # 6. 合并 + 元数据 + 板块评分 + 字段映射
        signals_df = pd.concat(signal_frames, ignore_index=True)
        results = self._enrich_results(signals_df, stocks, freq, _progress)

        _progress(10, 10, f"✅ 扫描完成: 锁定 {len(results)} 条信号")

        # 7. 持久化历史记录（0条也存，代表一次真实的拦截记录）
        self._save_scan_history(strategy_id, strategy_name, results, strategy_params)

        return results

    # ─── 子方法 ───────────────────────────────────────────────────

    def _filter_stock_pool(self, exchanges, sectors, index_filter, min_mv, max_mv,
                           hot_sectors_only, _progress) -> Optional[pd.DataFrame]:
        """第1步：加载股票池并按交易所/板块/指数/市值/热点过滤。"""
        use_filtered = bool(index_filter or min_mv is not None or max_mv is not None)
        if use_filtered:
            stocks = db.get_stocks_filtered(index_filter=index_filter, min_mv=min_mv, max_mv=max_mv)
        else:
            stocks = db.get_all_stocks()

        if exchanges:
            stocks = stocks[stocks["exchange"].isin(exchanges)]
        if sectors:
            def _match_sector(val):
                val_str = str(val)
                for s_filter in sectors:
                    if s_filter.startswith("CAT:"):
                        cat_char = s_filter[4:].upper()
                        if val_str.startswith(cat_char): return True
                    elif s_filter in val_str:
                        return True
                return False
            stocks = stocks[stocks["sector"].apply(_match_sector)]

        if hot_sectors_only:
            _progress(2, 10, "联动 AI 舆情热点词库过滤...")
            try:
                sentiment = db.get_latest_sentiment()
                if sentiment and sentiment.get("extracted_concepts"):
                    top_sectors = sentiment["extracted_concepts"]
                    log.info(f"应用热点板块过滤: {top_sectors}")

                    def _match_hot(val_sec, val_con):
                        s1, s2 = str(val_sec), str(val_con)
                        for sec in top_sectors:
                            if sec in s1 or sec in s2: return True
                        return False
                    mask = stocks.apply(lambda r: _match_hot(r["sector"], r["concepts"]), axis=1)
                    stocks = stocks[mask]
                else:
                    log.warning("未找到有效 AI 舆情热点记录，跳过该层过滤")
            except Exception as e:
                log.error(f"热点过滤异常: {e}")

        return stocks

    def _apply_fundamentals_filter(self, codes: list, fundamentals_filter: dict,
                                   strategy_params: dict, _progress) -> list:
        """第2步：基本面多因子过滤（ROE/毛利率/负债率/净利同比）。"""
        fundamentals_filter = fundamentals_filter or {}
        # 兼容：strategy_params 中的基本面参数透传到 fundamentals_filter
        if strategy_params:
            for k in ('min_roe', 'min_gross_margin', 'max_debt_to_assets', 'min_net_profit_yoy'):
                if k in strategy_params:
                    fundamentals_filter[k] = strategy_params[k]

        if not fundamentals_filter:
            return codes

        _progress(2.5, 10, "应用多因子财务/资金面漏斗过滤...")
        try:
            # H-02 修复：只查候选股票 + 只取需要的列，避免全表扫描
            placeholders = ",".join(["?"] * len(codes))
            cols = ["code"]
            if 'min_roe' in fundamentals_filter: cols.append("roe")
            if 'min_gross_margin' in fundamentals_filter: cols.append("gross_margin")
            if 'max_debt_to_assets' in fundamentals_filter: cols.append("debt_to_assets")
            if 'min_net_profit_yoy' in fundamentals_filter: cols.append("net_profit_yoy")
            col_sql = ", ".join(cols)
            fund_df = db.conn.execute(
                f"SELECT {col_sql} FROM stock_fundamentals WHERE code IN ({placeholders})",
                codes,
            ).df()
            if not fund_df.empty:
                initial_count = len(codes)
                mask = fund_df['code'].isin(codes)
                if 'min_roe' in fundamentals_filter:
                    mask &= (fund_df['roe'] >= fundamentals_filter['min_roe'])
                if 'min_gross_margin' in fundamentals_filter:
                    mask &= (fund_df['gross_margin'] >= fundamentals_filter['min_gross_margin'])
                if 'max_debt_to_assets' in fundamentals_filter:
                    mask &= (fund_df['debt_to_assets'] <= fundamentals_filter['max_debt_to_assets'])
                if 'min_net_profit_yoy' in fundamentals_filter:
                    mask &= (fund_df['net_profit_yoy'] >= fundamentals_filter['min_net_profit_yoy'])
                codes = fund_df[mask]['code'].tolist()
                log.info(f"基本面漏斗淘汰: {initial_count} -> {len(codes)}")
            else:
                log.warning("候选股票在 stock_fundamentals 表中无记录，跳过过滤")
        except Exception as e:
            log.error(f"多因子过滤异常: {e}")
        return codes

    def _prepare_strategy_context(self, strategy_name: str, strategy_params: dict,
                                  freq: str, calc_start, calc_end, _progress):
        """第3步：探测策略签名，按需预加载大盘基准和全市场数据。返回 _StrategyContext 或 None。"""
        strategy_module_path = get_strategy_info(strategy_name)
        if not strategy_module_path:
            return None

        # 嗅探策略函数签名
        needs_market = False
        needs_all_stock = False
        try:
            import inspect
            temp_strategy = load_strategy(strategy_name, strategy_params)
            actual_func = temp_strategy
            if isinstance(temp_strategy, FunctionalStrategyWrapper):
                actual_func = temp_strategy.func
            sig = inspect.signature(actual_func.generate_signals if hasattr(actual_func, 'generate_signals') else actual_func)
            needs_market = 'market_df' in sig.parameters
            needs_all_stock = 'all_stock_df' in sig.parameters
        except Exception as e:
            log.warning(f"策略签名嗅探异常 (降级为默认加载): {e}")

        market_df = None
        all_stock_df = None

        if needs_market:
            try:
                m_code = "000905.SH"
                market_df = db.load_bars(m_code, freq="daily", start=calc_start, end=calc_end)
                if market_df is not None and not market_df.empty:
                    log.info(f"成功预加载大盘基准数据: {m_code} ({len(market_df)} 行)")
            except Exception as e:
                log.warning(f"预加载大盘数据失败: {e}")

        if needs_all_stock and freq == "daily":
            _progress(4, 10, "正在进行全市场 RPS 动量预预算...")
            try:
                all_stock_df = db.load_all_bars(freq="daily", start=calc_start, end=calc_end)
                if all_stock_df is not None and not all_stock_df.empty:
                    all_stock_df = all_stock_df[['code', 'date', 'close']]
                    log.info(f"成功构建全市场对比池 (用于 RPS): {len(all_stock_df)} 条记录")
            except Exception as e:
                log.warning(f"构建 RPS 对比池失败: {e}")
        elif not needs_all_stock:
            log.info(f"⏩ 当前策略 [{strategy_name}] 无需全市场数据，启用极速扫描流...")

        return _StrategyContext(
            module_path=strategy_module_path,
            needs_market=needs_market,
            needs_all_stock=needs_all_stock,
            market_df=market_df,
            all_stock_df=all_stock_df,
        )

    def _fetch_live_quotes(self, codes: list, _progress):
        """第4步：获取盘中实时行情（走 quote_source 统一深 module）。

        返回 {code: {lastPrice,open,high,low,volume,...}} dict 供策略缝合
        (缝合器本身属候选⑥,这里只在边界转 dict,缝合逻辑不动)。
        """
        if settings.get("gateway", "active_gateway") != "qmt":
            return None
        _progress(4.5, 10, "QMT 极速行情引擎激活中：拉取当日最新真实验证切片...")
        try:
            from app.data_manager.quote_source import get_realtime_quotes
            qdf = get_realtime_quotes(codes)
            if qdf.empty:
                return None
            qdf = qdf[qdf["price"] > 0]
            if qdf.empty:
                return None
            # 转 camelCase dict(缝合器期望的格式);候选⑥ 会把缝合器也迁到 DataFrame
            live_quotes = {}
            for _, row in qdf.iterrows():
                live_quotes[str(row["code"])] = {
                    "lastPrice": float(row.get("price", 0)),
                    "open": float(row.get("open", 0) or 0),
                    "high": float(row.get("high", 0) or 0),
                    "low": float(row.get("low", 0) or 0),
                    "volume": float(row.get("volume", 0) or 0),
                    "amount": float(row.get("amount", 0) or 0),
                }
            if live_quotes:
                log.info(f"成功获取 {len(live_quotes)} 只股票实时快照进行策略缝合")
            return live_quotes or None
        except Exception as e:
            log.error(f"读取实时行情失败: {e}")
            return None

    def _parallel_scan(self, ctx, strategy_params, codes, freq,
                       calc_start, calc_end, target_start, target_end,
                       live_quotes, _progress) -> list:
        """第5步：将 codes 分片，ThreadPool 并行执行 _scan_worker。返回 signal_frames 列表。"""
        max_procs = min(cpu_count(), 8)
        num_workers = min(max_procs, len(codes) // 100 + 1)
        if num_workers < 1: num_workers = 1
        chunk_size = len(codes) // num_workers + 1
        code_chunks = [codes[i: i + chunk_size] for i in range(0, len(codes), chunk_size)]

        from concurrent.futures import ThreadPoolExecutor, as_completed
        total_chunks = len(code_chunks)
        signal_frames = [None] * total_chunks

        _progress(5, 5 + total_chunks, f"并行计算 {total_chunks} 个区块 ({num_workers} 线程)...")
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            fut_map = {}
            for i, chunk in enumerate(code_chunks):
                fut = executor.submit(
                    _scan_worker,
                    ctx.module_path, strategy_params,
                    chunk, freq, calc_start, calc_end,
                    target_start, target_end, live_quotes,
                    market_df=ctx.market_df, all_stock_df=ctx.all_stock_df
                )
                fut_map[fut] = i

            done = 0
            for future in as_completed(fut_map):
                idx = fut_map[future]
                done += 1
                try:
                    res = future.result()
                    if res is not None and not res.empty:
                        signal_frames[idx] = res
                except Exception as e:
                    log.error(f"区块 {idx+1} 计算失败: {e}")
                log.info(f"[{done}/{total_chunks}] 区块 {idx+1} 完成")

        return [f for f in signal_frames if f is not None and not f.empty]

    def _enrich_results(self, signals_df: pd.DataFrame, stocks: pd.DataFrame,
                        freq: str, _progress) -> list:
        """第6步：合并信号 → 关联元数据 → 注入板块评分 → 字段映射 → 返回 records。"""
        _progress(9, 10, "关联元数据并排序...")
        date_col = "date" if freq == "daily" else "datetime"
        meta = stocks[["code", "name", "sector"]]
        res_df = pd.merge(signals_df, meta, on="code", how="left")
        res_df = res_df.sort_values(date_col, ascending=False)

        # 批量注入板块/概念评分
        self._inject_sector_scores(res_df, date_col)

        # NaN → None（避免 JSON 序列化出 NaN 导致前端 JSON.parse 崩溃）
        res_df = res_df.where(pd.notna(res_df), None)
        results = res_df.to_dict(orient="records")
        log.info(f"选股原始结果字段: {list(res_df.columns)}")

        for r in results:
            r["buy_date"] = r.get(date_col) or r.get("date") or r.get("datetime")
            r["entry_price"] = r.get("close") or r.get("price") or r.get("last") or 0.0
            if hasattr(r["buy_date"], "isoformat"):
                r["buy_date"] = r["buy_date"].strftime("%Y-%m-%d")
            elif isinstance(r["buy_date"], str) and " " in r["buy_date"]:
                r["buy_date"] = r["buy_date"].split(" ")[0]
            if not r.get("buy_date"): r["buy_date"] = "--"
            if not r.get("entry_price"): r["entry_price"] = 0.0
        return results

    def _inject_sector_scores(self, res_df: pd.DataFrame, date_col: str):
        """向结果 DataFrame 注入 sector_score/concept_score/total_score。失败时置 None。"""
        try:
            from app.hot_sector.engine import hot_sector_engine
            if date_col not in res_df.columns or res_df.empty:
                self._set_default_scores(res_df)
                return
            sample_date = res_df[date_col].dropna().iloc[0]
            if sample_date is None:
                self._set_default_scores(res_df)
                return
            if hasattr(sample_date, "strftime"):
                td = sample_date.date() if hasattr(sample_date, "date") else sample_date
            else:
                td = pd.to_datetime(sample_date).date() if sample_date else None
            if not td:
                self._set_default_scores(res_df)
                return
            codes = res_df['code'].tolist()
            detail_scores = hot_sector_engine.batch_score_stocks_detail(codes, trade_date=td)
            res_df['sector_score'] = res_df['code'].map(lambda c: detail_scores.get(c.split('.')[0], {}).get('sector_score', 0.0))
            res_df['concept_score'] = res_df['code'].map(lambda c: detail_scores.get(c.split('.')[0], {}).get('concept_score', 0.0))
            res_df['total_score'] = res_df['code'].map(lambda c: detail_scores.get(c.split('.')[0], {}).get('total_score', 0.0))
        except Exception as e:
            log.warning(f"sector scoring skipped: {e}")
            self._set_default_scores(res_df)

    @staticmethod
    def _set_default_scores(res_df: pd.DataFrame):
        """L-04 修复：评分缺失时置 None 而非 0.0，前端显示 '--' 区分'无评分'和'0分'。"""
        res_df['sector_score'] = None
        res_df['concept_score'] = None
        res_df['total_score'] = None

    def _save_scan_history(self, strategy_id, strategy_name, results, strategy_params):
        """第7步：保存选股历史记录。0条也存，代表一次真实的拦截记录。"""
        if not strategy_id:
            df_strats = db.get_strategies()
            matched_row = df_strats[df_strats['name'] == strategy_name]
            strategy_id = int(matched_row.iloc[0]['id']) if not matched_row.empty else 0
        db.save_scan_result(strategy_id, strategy_name, [r['code'] for r in results], strategy_params or {})


@dataclass
class _StrategyContext:
    """策略执行上下文：封装签名嗅探结果和预加载的数据。"""
    module_path: str
    needs_market: bool
    needs_all_stock: bool
    market_df: Optional[pd.DataFrame]
    all_stock_df: Optional[pd.DataFrame]
