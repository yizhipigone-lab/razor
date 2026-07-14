import time
import pandas as pd
from datetime import datetime
from app.data_manager.tushare_api import tushare_fetcher
from database.duckdb_manager import db
from core.logger import get_logger

log = get_logger("TushareSync")

class TushareSyncManager:
    def __init__(self):
        self.pro = tushare_fetcher.pro

    def format_to_duckdb_code(self, ts_code: str) -> str:
        """Convert Tushare formatting 000001.SZ to system formatting 000001"""
        return ts_code.split('.')[0] if '.' in ts_code else ts_code

    def sync_stock_basic(self):
        """Sync stock list, names, sectors, list dates"""
        if not self.pro:
            log.error("Tushare API is not configured.")
            return False

        try:
            log.info("Fetching stock basics from Tushare...")
            df = self.pro.stock_basic(fields='ts_code,symbol,name,area,industry,market,list_date')

            if df.empty:
                log.warning("Received empty stock basic dataframe.")
                return False

            # Format for DuckDB schema
            df['code'] = df['symbol']
            df['exchange'] = df['ts_code'].apply(lambda x: x.split('.')[1] if '.' in x else 'UNKNOWN')
            df['sector'] = df['industry']
            df['concepts'] = ''  # Can be enriched later
            
            # Format dates
            df['list_date'] = pd.to_datetime(df['list_date']).dt.date
            df['status'] = 'active'

            required_cols = ['code', 'name', 'exchange', 'sector', 'concepts', 'list_date', 'status']
            upsert_df = df[required_cols].copy()

            db.upsert_stocks(upsert_df)
            log.info(f"Successfully synced {len(upsert_df)} stock basics.")
            return True

        except Exception as e:
            log.error(f"Error syncing stock basic: {e}")
            return False

    def sync_fundamentals_snapshot(self, date_str: str = None, progress_cb=None):
        """
        Fetch daily basics (PE, PB, MV) and update the stock_fundamentals table
        date_str format: 'YYYYMMDD'. Default is most recent trading day.
        """
        if not self.pro:
            return False

        try:
            log.info(f"Fetching daily basics snapshot from Tushare (Date: {date_str or 'Latest'})...")
            # For simplicity, we get daily_basic which contains PE, PB, Total_MV
            if date_str:
                df = self.pro.daily_basic(trade_date=date_str)
            else:
                cal = self.pro.trade_cal(exchange='', start_date=datetime.now().strftime('%Y%m%d'), end_date=datetime.now().strftime('%Y%m%d'))
                if not cal.empty and cal.iloc[0]['is_open'] == 1:
                    df = self.pro.daily_basic(trade_date=datetime.now().strftime('%Y%m%d'))
                else:
                    df = self.pro.daily_basic(ts_code='') # default latest 

            if df.empty:
                log.warning("No daily basic data found.")
                return False

            df['code'] = df['ts_code'].apply(self.format_to_duckdb_code)
            
            # Fundamentals schema matching
            df['pe_ttm'] = df.get('pe_ttm', None)
            df['pb'] = df.get('pb', None)
            df['total_mv'] = df.get('total_mv', None)
            df['circ_mv'] = df.get('circ_mv', None)

            # --- [Fix] Fetch financial indicators (ROE, Margin, etc.) with Batching ---
            log.info("Pulling more financial indicators (ROE, Margin) in chunks of 100...")
            f_cols = 'ts_code,roe,grossprofit_margin,netprofit_yoy,debt_to_assets'
            all_ts_codes = df['ts_code'].tolist()
            indicators_list = []
            
            # Determine most recent applicable reporting periods
            curr_y = datetime.now().year
            # Attempt to pull from most likely recent periods: Q3 of last year and Annual of last year.
            # Strict limit: ONLY 2 periods to cut down API calls in half
            periods = [f"{curr_y-1}1231", f"{curr_y-1}0930"]
            
            batch_size = 100
            total_batches = (len(all_ts_codes) + batch_size - 1) // batch_size
            
            # Pre-flight check to verify token permissions
            try:
                self.pro.fina_indicator(ts_code='000001.SZ', period=periods[0], fields=f_cols)
            except Exception as e:
                log.error(f"Tushare API permission error or limit hit during pre-flight: {e}")
                if progress_cb: progress_cb(10, 100, "⚠️ Tushare 接口权限异常，跳过财务指标抓取...")
                return False

            for i in range(0, len(all_ts_codes), batch_size):
                batch = all_ts_codes[i : i + batch_size]
                batch_number = (i // batch_size) + 1
                
                if progress_cb and total_batches > 0:
                    # Allocate 10% to 60% of overall progress for this phase
                    prog_val = 10 + int(50 * (batch_number / total_batches))
                    progress_cb(prog_val, 100, f"⏳ 正在拉取财务指标 批次 ({batch_number}/{total_batches})...")
                
                batch_df = pd.DataFrame()
                for p in periods:
                    try:
                        temp_df = self.pro.fina_indicator(ts_code=",".join(batch), period=p, fields=f_cols)
                        if temp_df is not None and not temp_df.empty:
                            batch_df = pd.concat([batch_df, temp_df])
                    except Exception as e:
                        log.warning(f"Batch {batch_number} failed on period {p}: {e}")
                    
                    time.sleep(0.5) # Minimum 0.5s strictly between EVERY API request

                if not batch_df.empty:
                    # Keep the most recent data by preferring earlier periods if concated in order? 
                    # Drop duplicates keep first since the first periods (e.g. 1231) are appended first.
                    batch_df = batch_df.sort_values('ts_code').drop_duplicates('ts_code', keep='first')
                    indicators_list.append(batch_df)
                
                time.sleep(1.0) # Additional 1.0s delay per batch total 2.0s per batch (30/min max, very safe)
            
            if indicators_list:
                f_df = pd.concat(indicators_list)
                f_df['code'] = f_df['ts_code'].apply(self.format_to_duckdb_code)
                f_df = f_df.rename(columns={'netprofit_yoy': 'net_profit_yoy', 'grossprofit_margin': 'gross_margin'})
                df = pd.merge(df, f_df[['code', 'roe', 'gross_margin', 'net_profit_yoy', 'debt_to_assets']], on='code', how='left')
            else:
                log.error("All batches for financial indicators failed.")
                for col in ['roe', 'gross_margin', 'net_profit_yoy', 'debt_to_assets']:
                    if col not in df.columns: df[col] = None

            cols = ['code', 'pe_ttm', 'pb', 'total_mv', 'circ_mv', 'roe', 'gross_margin', 'net_profit_yoy', 'debt_to_assets']
            # Dedup and Clean
            upsert_df = df[cols].drop_duplicates('code').copy()
            
            db.upsert_fundamentals(upsert_df)
            log.info(f"Successfully synced {len(upsert_df)} fundamentals records with enhanced indicators.")
            return True

        except Exception as e:
            log.error(f"Error syncing fundamentals snapshot: {e}")
            import traceback
            traceback.print_exc()
            return False

    def sync_index_daily(self, progress_cb=None, start_date: str = None,
                         end_date: str = None, mode: str = "incremental") -> bool:
        """
        同步主要市场指数的日线数据到本地 Parquet。
        文件命名：index_000001.parquet（上证综指）等，
        避免与股票 000001.parquet（平安银行）冲突。

        mode: "incremental" (默认) 仅拉取最近日期范围并合并；
              "full" 拉取 2018 年至今全部覆盖写入。
        """
        import os
        import tushare as ts
        from pathlib import Path
        from datetime import datetime, timedelta

        ts_key = os.getenv("TUSHARE_KEY", "")
        if not ts_key:
            log.error("sync_index_daily | TUSHARE_KEY 未配置")
            return False

        ts.set_token(ts_key)
        pro = ts.pro_api()

        PARQUET_DIR = Path(__file__).parent.parent.parent / "data" / "parquet" / "daily"
        PARQUET_DIR.mkdir(parents=True, exist_ok=True)

        INDICES = [
            # 综合指数
            ("000001.SH", "index_000001"),   # 上证综指
            ("399001.SZ", "index_399001"),   # 深证成指
            ("399006.SZ", "index_399006"),   # 创业板指
            ("000688.SH", "index_000688"),   # 科创50
            # 宽基指数（Regime 基准首选）
            ("000510.SH", "index_000510"),   # 中证A500
            ("000300.SH", "index_000300"),   # 沪深300
            ("000905.SH", "index_000905"),   # 中证500
            ("000852.SH", "index_000852"),   # 中证1000
            ("000985.SH", "index_000985"),   # 中证全指
            # 风格指数
            ("000016.SH", "index_000016"),   # 上证50
            # 补充 qmt_sync_index_job.py 中配置的指数
            ("000009.SH", "index_000009"),   # 上证380
            ("399004.SZ", "index_399004"),   # 深证100
            ("399005.SZ", "index_399005"),   # 中小板指
        ]

        if end_date:
            _end = end_date.replace("-", "")
        else:
            _end = datetime.now().strftime("%Y%m%d")

        if start_date:
            _start = start_date.replace("-", "")
        elif mode == "incremental":
            _start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        else:
            _start = "20180101"

        success_count = 0
        total = len(INDICES)

        for i, (ts_code, file_stem) in enumerate(INDICES):
            if progress_cb:
                progress_cb(i + 1, total, f"正在同步指数 {ts_code}...")
            try:
                log.info(f"sync_index_daily | 拉取 {ts_code} ({_start}~{_end})...")
                df = pro.index_daily(
                    ts_code=ts_code,
                    start_date=_start,
                    end_date=_end,
                    fields="trade_date,open,high,low,close,vol,amount,pct_chg"
                )
                if df is None or df.empty:
                    log.warning(f"sync_index_daily | {ts_code} 无数据")
                    continue

                df = df.rename(columns={
                    "trade_date": "date",
                    "vol":        "volume",
                    "pct_chg":    "change_pct",
                })
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)
                df["code"] = ts_code.split(".")[0]
                df["is_index"] = True

                out_path = PARQUET_DIR / f"{file_stem}.parquet"

                # 增量模式：与已有数据合并去重
                if mode == "incremental" and out_path.exists():
                    try:
                        old_df = pd.read_parquet(out_path)
                        old_df["date"] = pd.to_datetime(old_df["date"])
                        df = pd.concat([old_df, df], ignore_index=True)
                        df = df.drop_duplicates(subset=["date"], keep="last")
                        df = df.sort_values("date").reset_index(drop=True)
                    except Exception as merge_err:
                        log.warning(f"sync_index_daily | {file_stem} 合并失败，覆写: {merge_err}")

                df.to_parquet(out_path, index=False)
                date_range = f"{df['date'].min().date()} ~ {df['date'].max().date()}" if not df.empty else "无数据"
                log.info(
                    f"sync_index_daily | {ts_code} 保存成功：{len(df)} 条（{date_range}）"
                    f" → {out_path.name}"
                )
                success_count += 1
                time.sleep(0.3)

            except Exception as e:
                log.error(f"sync_index_daily | {ts_code} 同步失败: {e}")

        if progress_cb:
            progress_cb(total, total, f"指数日线同步完成：{success_count}/{total} 成功")
        return success_count > 0

    def sync_index_members(self, progress_cb=None) -> bool:
        """
        同步三大指数的成分股到本地数据库。
        策略：
          1. 优先用 Tushare index_weight 接口（5100积分可用，返回当月权重+成分代码）
             => 取最近一个月末的数据，提取 con_code 即为当前成分股
          2. 失败则自动降级到本地静态 CSV 文件
        注意：index_member 接口只返回历史记录流水（需超高积分），不用它。
        """
        import os, time as _time, csv
        from pathlib import Path
        from datetime import date, timedelta
        from app.backtest.index_config import INDEX_MAP, INDEX_DISPLAY

        ROOT_DIR = Path(__file__).parent.parent.parent
        BACKUP_DIR = ROOT_DIR / "data" / "meta" / "index_backup"

        # 计算最近一个月末日期（用于 index_weight 查询）
        today = date.today()
        # 往回找上月末或本月已过的最近月末
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - timedelta(days=1)
        trade_date_str = last_month_end.strftime("%Y%m%d")

        total = len(INDEX_MAP)
        success_count = 0
        api_available = self.pro is not None

        # ── 预检：测试 index_weight 接口 ────────────────────────
        if api_available:
            try:
                test = self.pro.index_weight(
                    index_code="000300.SH",
                    trade_date=trade_date_str,
                    fields="con_code,weight"
                )
                if test is None or test.empty:
                    log.warning(f"sync_index_members | index_weight 预检: {trade_date_str} 无数据，尝试再往前一月")
                    # 再退一个月
                    first_of_last = last_month_end.replace(day=1)
                    last_month_end = first_of_last - timedelta(days=1)
                    trade_date_str = last_month_end.strftime("%Y%m%d")
                    test2 = self.pro.index_weight(
                        index_code="000300.SH",
                        trade_date=trade_date_str,
                        fields="con_code,weight"
                    )
                    if test2 is None or test2.empty:
                        log.warning("sync_index_members | API预检两次均空，降级为静态CSV")
                        api_available = False
                    else:
                        log.info(f"sync_index_members | API预检通过 (date={trade_date_str}), 返回 {len(test2)} 行")
                else:
                    log.info(f"sync_index_members | API预检通过 (date={trade_date_str}), 返回 {len(test)} 行")
            except Exception as e:
                log.warning(f"sync_index_members | API预检失败 ({e})，降级为静态CSV")
                api_available = False

        for i, (key, ts_code) in enumerate(INDEX_MAP.items()):
            name = INDEX_DISPLAY.get(key, key)
            if progress_cb:
                progress_cb(i + 1, total, f"同步 {name} ({key})...")

            codes = []
            source = ""

            # ── 方案A: Tushare index_weight 接口 ──────────────────
            if api_available:
                try:
                    df = self.pro.index_weight(
                        index_code=ts_code,
                        trade_date=trade_date_str,
                        fields="con_code,weight"
                    )
                    if df is not None and not df.empty:
                        codes = [self.format_to_duckdb_code(c) for c in df["con_code"].tolist()]
                        source = f"Tushare index_weight ({trade_date_str})"
                        log.info(f"sync_index_members | {name}: API返回 {len(codes)} 只")
                    _time.sleep(0.5)
                except Exception as e:
                    log.warning(f"sync_index_members | {name} API拉取失败 ({e})，尝试静态CSV")
                    codes = []

            # ── 方案B: 本地静态 CSV 降级 ──────────────────────────
            if not codes:
                csv_path = BACKUP_DIR / f"{key}.csv"
                if csv_path.exists():
                    try:
                        with open(csv_path, "r", encoding="utf-8") as f:
                            reader = csv.DictReader(f)
                            codes = [row["stock_code"] for row in reader if row.get("stock_code")]
                        source = "本地静态CSV"
                    except Exception as e:
                        log.error(f"sync_index_members | {name} 静态CSV读取失败: {e}")
                else:
                    log.error(f"sync_index_members | {name} 静态备份不存在: {csv_path}")

            if codes:
                db.upsert_index_members(key, codes)
                log.info(f"sync_index_members | {name}: {len(codes)} 只成分股写入完成 (来源: {source})")
                success_count += 1
            else:
                log.error(f"sync_index_members | {name}: 所有方案均失败，跳过")

        if progress_cb:
            progress_cb(total, total, f"指数成分同步完成：{success_count}/{total} 成功")
        return success_count > 0


tushare_sync_manager = TushareSyncManager()
