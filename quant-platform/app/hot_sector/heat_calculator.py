"""
板块/概念热度计算器

核心算法：从成分股日线涨跌幅聚合出板块和概念的热度。
关键优化：一次加载全市场日线到内存，避免 N+1 查询。
"""

import pandas as pd
import numpy as np
from datetime import date, timedelta
from core.logger import get_logger
from database.duckdb_manager import db

log = get_logger("HeatCalc")


class HeatCalculator:
    """热度计算器：聚合成分股涨跌幅得出板块/概念热度"""

    def _load_all_latest_bars(self, trade_date: date = None) -> dict:
        """
        一次加载全市场近 2 个交易日日线到内存 dict。
        返回: {stock_code: {close, prev_close, change_pct}}
        """
        if trade_date is None:
            trade_date = date.today()

        # 取最近 10 个交易日以保证能拿到 2 个有效数据点
        start = trade_date - timedelta(days=20)
        df = db.load_all_bars(freq="daily", start=start, end=trade_date)
        if df.empty:
            log.warning("无日线数据可用于热度计算")
            return {}

        date_col = "date" if "date" in df.columns else "datetime"
        if "code" not in df.columns:
            log.error("日线数据缺少 code 列")
            return {}

        df[date_col] = pd.to_datetime(df[date_col])

        # 对每只股票，取最后 2 条计算涨跌幅
        result = {}
        for code, group in df.groupby("code"):
            group = group.sort_values(date_col)
            if len(group) < 2:
                continue
            latest = group.iloc[-1]
            prev = group.iloc[-2]
            close = float(latest.get("close", 0))
            prev_close = float(prev.get("close", 0))
            if prev_close > 0:
                change_pct = (close - prev_close) / prev_close * 100
            else:
                change_pct = 0.0
            result[code] = {
                "close": close,
                "prev_close": prev_close,
                "change_pct": change_pct
            }

        log.info(f"加载了 {len(result)} 只股票的最近日线数据")
        return result

    def calc_concept_heat(self, concept_name: str, bars_dict: dict) -> dict:
        """计算单个概念的热度（成分股涨跌幅均值）"""
        df_stocks = db.get_concept_stocks(concept_name)
        if df_stocks.empty:
            return {"hotness": 0.0, "constituent_count": 0,
                    "advance_count": 0, "decline_count": 0, "avg_change_pct": 0.0}

        changes = []
        advances = 0
        declines = 0
        for _, row in df_stocks.iterrows():
            code = row['stock_code']
            bar = bars_dict.get(code)
            if bar is None:
                # 尝试加后缀匹配
                for suffix in ('.SH', '.SZ', '.BJ'):
                    if code.endswith(suffix):
                        break
                else:
                    for prefix, suffix in [('6', '.SH'), ('0', '.SZ'), ('3', '.SZ'), ('4', '.BJ'), ('8', '.BJ')]:
                        if code.startswith(prefix):
                            bar = bars_dict.get(f"{code}{suffix}")
                            break
            if bar:
                chg = bar['change_pct']
                changes.append(chg)
                if chg > 0:
                    advances += 1
                elif chg < 0:
                    declines += 1

        avg_change = float(np.mean(changes)) if changes else 0.0
        return {
            "hotness": round(avg_change, 2),
            "constituent_count": len(changes),
            "advance_count": advances,
            "decline_count": declines,
            "avg_change_pct": round(avg_change, 2)
        }

    def calc_sector_heat(self, sector_name: str, bars_dict: dict) -> dict:
        """计算单个行业板块的热度（成分股涨跌幅均值）"""
        df_stocks = db.conn.execute(
            "SELECT code FROM stocks WHERE sector = ? AND status = 'active'",
            [sector_name]
        ).df()
        if df_stocks.empty:
            return {"hotness": 0.0, "constituent_count": 0,
                    "advance_count": 0, "decline_count": 0, "avg_change_pct": 0.0}

        changes = []
        advances = 0
        declines = 0
        for _, row in df_stocks.iterrows():
            code = row['code']
            bar = bars_dict.get(code)
            if bar:
                chg = bar['change_pct']
                changes.append(chg)
                if chg > 0:
                    advances += 1
                elif chg < 0:
                    declines += 1

        avg_change = float(np.mean(changes)) if changes else 0.0
        return {
            "hotness": round(avg_change, 2),
            "constituent_count": len(changes),
            "advance_count": advances,
            "decline_count": declines,
            "avg_change_pct": round(avg_change, 2)
        }

    def calc_all_heat(self, trade_date: date = None, progress_cb=None) -> dict:
        """全量重算所有概念和行业板块的热度"""
        if trade_date is None:
            trade_date = date.today()

        bars_dict = self._load_all_latest_bars(trade_date)
        if not bars_dict:
            return {"concepts_processed": 0, "sectors_processed": 0,
                    "trade_date": str(trade_date), "error": "无日线数据"}

        # 1. 计算所有概念热度
        concepts = db.get_distinct_concepts()
        concept_results = []
        for i, concept in enumerate(concepts):
            heat = self.calc_concept_heat(concept, bars_dict)
            concept_results.append({
                "concept_name": concept,
                "trade_date": trade_date,
                **heat
            })
            if progress_cb and i % 10 == 0:
                progress_cb(i + 1, len(concepts), f"概念 {i+1}/{len(concepts)}")

        # 2. 计算所有行业板块热度
        sectors = db.get_distinct_sectors()
        sector_results = []
        for i, sector in enumerate(sectors):
            heat = self.calc_sector_heat(sector, bars_dict)
            sector_results.append({
                "sector_name": sector,
                "trade_date": trade_date,
                **heat
            })

        # 3. 批量写入 DuckDB
        if concept_results:
            df_concept = pd.DataFrame(concept_results)
            db.upsert_concept_heat(df_concept)
        if sector_results:
            df_sector = pd.DataFrame(sector_results)
            db.upsert_sector_heat(df_sector)

        log.info(f"热度计算完成: {len(concept_results)} 概念, {len(sector_results)} 板块")
        return {
            "concepts_processed": len(concept_results),
            "sectors_processed": len(sector_results),
            "trade_date": str(trade_date)
        }
