import time
import pandas as pd
from datetime import datetime, date
from collections import defaultdict
from app.data_manager.tushare_api import tushare_fetcher
from database.duckdb_manager import db
from core.logger import get_logger

log = get_logger("ParquetPipeline")

class ParquetPipelineManager:
    def __init__(self):
        self.pro = tushare_fetcher.pro

    def format_to_duckdb_code(self, ts_code: str) -> str:
        return ts_code.split('.')[0] if '.' in ts_code else ts_code

    def sync_daily_klinesto_parquet(self, start_date: str = None, end_date: str = None, progress_cb=None):
        """批量拉取全市场日线 K 线并写入 Parquet

        优化要点:
          - 先攒后写：收集全量数据后逐股票一次性写入
          - 避免逐日逐 stock 的 I/O 风暴（250天×5000只 → 5000次）
          - 每只股票仅一次读-合并-写操作
        """
        if not self.pro:
            log.error("Tushare API is not configured.")
            return False

        end_date = end_date or datetime.now().strftime('%Y%m%d')
        start_date = start_date or (datetime.now() - pd.Timedelta(days=7)).strftime('%Y%m%d')

        log.info(f"ParquetPipeline | Fetching daily k-lines from {start_date} to {end_date}...")

        try:
            dates = self.pro.trade_cal(exchange='', start_date=start_date, end_date=end_date, is_open='1')
            if dates.empty:
                log.info("ParquetPipeline | No trading days found in range.")
                return True

            total_dates = len(dates['cal_date'])

            # 1) 逐日拉取数据，按股票代码收集
            stocks_data = defaultdict(list)

            for idx, d in enumerate(dates['cal_date']):
                log.info(f"ParquetPipeline | Pulling market for {d}...")

                if progress_cb and total_dates > 0:
                    # 拉取阶段占 80% 进度
                    prog_val = int(80 * (idx / total_dates))
                    progress_cb(prog_val, 100, f"正在拉取 {d} ({idx+1}/{total_dates})...")

                df = self.pro.daily(trade_date=d)

                if df is None or df.empty:
                    from server.websocket.manager import sync_broadcast
                    msg = f"Tushare Warning: No data for {d}. May hit limit/points."
                    sync_broadcast({"type": "log", "level": "warning", "msg": msg})
                    continue

                # 数据-C3: 同步拉复权因子, 存入 parquet(adj_factor列), 读取层做前复权。
                # 存原始价+因子(不在写入时改价), 读时按文件内最新因子归一→永远正确的前复权,
                # 增量追加不破坏(读取时整体重算)。
                try:
                    adj = self.pro.adj_factor(trade_date=d)
                    if adj is not None and not adj.empty:
                        df = df.merge(adj[['ts_code', 'adj_factor']], on='ts_code', how='left')
                    time.sleep(0.15)  # adj_factor 限速
                except Exception as _e:
                    log.debug(f"adj_factor 拉取失败 {d}: {_e}")
                if 'adj_factor' not in df.columns:
                    df['adj_factor'] = 1.0
                df['adj_factor'] = df['adj_factor'].fillna(1.0)

                df['code'] = df['ts_code'].apply(self.format_to_duckdb_code)
                df['date'] = pd.to_datetime(d)

                for code, code_df in df.groupby('code'):
                    stocks_data[code].append(code_df)

                time.sleep(0.3)  # Tushare 限速

            if not stocks_data:
                log.warning("ParquetPipeline | No data collected.")
                return True

            # 2) 批量写入 Parquet —— 每只股票一次 I/O
            log.info(f"ParquetPipeline | Writing {len(stocks_data)} stocks to Parquet...")
            if progress_cb:
                progress_cb(85, 100, f"正在批量写入 {len(stocks_data)} 只股票的 Parquet 文件...")

            # 合并每只股票的 DataFrame 列表 → 单 DataFrame
            stocks_merged = {}
            for code, dfs in stocks_data.items():
                if len(dfs) == 1:
                    stocks_merged[code] = dfs[0]
                else:
                    stocks_merged[code] = pd.concat(dfs, ignore_index=True)

            db.batch_save_bars(stocks_merged, freq='daily')

            if progress_cb:
                progress_cb(95, 100, "正在重建数据视图...")

            db.create_kline_view(freq='daily')
            from server.websocket.manager import sync_broadcast
            sync_broadcast({"type": "log", "level": "info", "msg": "Tushare Daily Sync Finished."})

            if progress_cb:
                progress_cb(100, 100, "同步完成！")

            log.info("ParquetPipeline | Daily K-lines fully synced.")
            return True

        except Exception as e:
            log.error(f"ParquetPipeline | Sync failed: {e}")
            import traceback
            traceback.print_exc()
            return False


parquet_pipeline = ParquetPipelineManager()
