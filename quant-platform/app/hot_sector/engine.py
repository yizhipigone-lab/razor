"""
热点板块评分引擎

核心评分公式：stock_score = sector_hotness * sector_weight + max(concept_hotness) * concept_weight
支持 Redis 缓存（T+0 盘中 10 分钟 TTL）和 DuckDB 持久化（T+1 历史）。
Redis 不可用时自动降级到进程内内存缓存。
"""

import json
import threading
from datetime import date, datetime
from typing import Optional

import pandas as pd

from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db
from app.hot_sector.heat_calculator import HeatCalculator

log = get_logger("HotSectorEngine")

# 默认权重
DEFAULT_SECTOR_WEIGHT = 0.6
DEFAULT_CONCEPT_WEIGHT = 0.4

# 概念同步重试间隔（秒），避免频繁重试消耗 Tushare 配额
_CONCEPT_SYNC_RETRY_INTERVAL = 3600  # 1 小时


class HotSectorEngine:
    """热点板块评分引擎（单例，线程安全）"""

    _instance = None
    _lock = threading.Lock()
    _calc_lock = threading.Lock()
    _last_concept_sync_attempt: float = 0.0  # 上次概念同步尝试的时间戳

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def _ensure_initialized(self):
        if not self._initialized:
            self._initialized = True

    # ─── 权重 ────────────────────────────────────────────────

    @property
    def sector_weight(self) -> float:
        return float(settings.get("hot_sector", "sector_weight", default=DEFAULT_SECTOR_WEIGHT))

    @property
    def concept_weight(self) -> float:
        return float(settings.get("hot_sector", "concept_weight", default=DEFAULT_CONCEPT_WEIGHT))

    @property
    def redis_ttl(self) -> int:
        return int(settings.get("hot_sector", "redis_ttl_seconds", default=600))

    # ─── 单股评分 ────────────────────────────────────────────

    def get_stock_sector_score(self, stock_code: str, trade_date: date = None) -> dict:
        """计算单只股票的板块/概念综合评分"""
        self._ensure_initialized()
        if trade_date is None:
            trade_date = date.today()

        # 去除后缀
        clean_code = stock_code.split('.')[0]

        sector = self._get_stock_sector(clean_code)
        concepts = db.get_stock_concepts(clean_code)

        sector_heat = 0.0
        concept_heat = 0.0

        if sector:
            row = db.conn.execute(
                "SELECT hotness FROM sector_heat WHERE sector_name = ? AND trade_date = ?",
                [sector, trade_date]
            ).fetchone()
            if row:
                sector_heat = float(row[0])

        if concepts:
            max_c = 0.0
            for concept in concepts:
                row = db.conn.execute(
                    "SELECT hotness FROM concept_heat WHERE concept_name = ? AND trade_date = ?",
                    [concept, trade_date]
                ).fetchone()
                if row:
                    max_c = max(max_c, float(row[0]))
            concept_heat = max_c

        score = sector_heat * self.sector_weight + concept_heat * self.concept_weight

        return {
            "code": clean_code,
            "sector": sector or "",
            "sector_hotness": round(sector_heat, 2),
            "concepts": concepts or [],
            "concept_hotness": round(concept_heat, 2),
            "composite_score": round(score, 4)
        }

    def _get_stock_sector(self, code: str) -> Optional[str]:
        """查询单只股票的所属行业板块"""
        row = db.conn.execute(
            "SELECT sector FROM stocks WHERE code = ? AND status = 'active'",
            [code]
        ).fetchone()
        return row[0] if row else None

    # ─── 批量评分 ────────────────────────────────────────────

    def batch_score_stocks(self, stock_codes: list, trade_date: date = None) -> dict:
        """
        批量评分，优先走 Redis 缓存。
        返回: {stock_code: composite_score}
        """
        self._ensure_initialized()
        if trade_date is None:
            trade_date = date.today()

        # 去后缀
        clean_codes = [c.split('.')[0] for c in stock_codes]

        # 尝试读 Redis 缓存
        cached = self._batch_read_redis(clean_codes)
        missing = [c for c in clean_codes if c not in cached]

        if missing:
            computed = self._compute_scores_batch(missing, trade_date)
            self._batch_write_redis(computed, trade_date)
            cached.update(computed)

        return cached

    def batch_score_stocks_detail(self, stock_codes: list, trade_date: date = None) -> dict:
        """
        批量评分详情，返回每个股票的板块评分、热点评分、综合评分。

        返回: {stock_code: {"sector_score": float, "concept_score": float, "total_score": float}}
        """
        self._ensure_initialized()
        if trade_date is None:
            trade_date = date.today()

        clean_codes = [c.split('.')[0] for c in stock_codes]

        # 批量查 sector_heat
        sector_heat_map = {}
        try:
            rows = db.conn.execute(
                "SELECT sector_name, hotness FROM sector_heat WHERE trade_date = ?",
                [trade_date]
            ).fetchall()
            sector_heat_map = {r[0]: float(r[1]) for r in rows}
        except Exception:
            pass

        # 批量查 concept_heat
        concept_heat_map = {}
        try:
            rows = db.conn.execute(
                "SELECT concept_name, hotness FROM concept_heat WHERE trade_date = ?",
                [trade_date]
            ).fetchall()
            concept_heat_map = {r[0]: float(r[1]) for r in rows}
        except Exception:
            pass

        result = {}
        for code in clean_codes:
            sector = self._get_stock_sector(code)
            concepts = db.get_stock_concepts(code)

            sector_score = sector_heat_map.get(sector, 0.0) if sector else 0.0
            concept_score = max(
                [concept_heat_map.get(c, 0.0) for c in concepts],
                default=0.0
            ) if concepts else 0.0

            total_score = sector_score * self.sector_weight + concept_score * self.concept_weight

            result[code] = {
                "sector_score": round(sector_score, 2),
                "concept_score": round(concept_score, 2),
                "total_score": round(total_score, 4)
            }

        return result

    def _compute_scores_batch(self, stock_codes: list, trade_date: date) -> dict:
        """从 DuckDB 批量计算评分"""
        result = {}

        # 批量查 sector_heat
        sector_heat_map = {}
        try:
            rows = db.conn.execute(
                "SELECT sector_name, hotness FROM sector_heat WHERE trade_date = ?",
                [trade_date]
            ).fetchall()
            sector_heat_map = {r[0]: float(r[1]) for r in rows}
        except Exception:
            pass

        # 批量查 concept_heat
        concept_heat_map = {}
        try:
            rows = db.conn.execute(
                "SELECT concept_name, hotness FROM concept_heat WHERE trade_date = ?",
                [trade_date]
            ).fetchall()
            concept_heat_map = {r[0]: float(r[1]) for r in rows}
        except Exception:
            pass

        for code in stock_codes:
            sector = self._get_stock_sector(code)
            concepts = db.get_stock_concepts(code)

            sector_heat = sector_heat_map.get(sector, 0.0) if sector else 0.0
            concept_heat = max(
                [concept_heat_map.get(c, 0.0) for c in concepts],
                default=0.0
            ) if concepts else 0.0

            score = sector_heat * self.sector_weight + concept_heat * self.concept_weight
            result[code] = round(score, 4)

        return result

    # ─── 缓存 ─────────────────────────────────────────────    def _batch_read_redis(self, codes: list) -> dict:
        """从缓存批量读取评分（Redis 优先，不可用时走内存缓存）"""
        try:
            from core.redis_manager import redis_manager
            if not redis_manager.is_available() and not redis_manager.get_client():
                return {}

            client = redis_manager.get_client()
            if not client:
                # 内存缓存模式：逐个读
                out = {}
                for code in codes:
                    val = redis_manager.cache_get(f"hot_sector:score:{code}")
                    if val is not None:
                        try:
                            out[code] = float(val)
                        except (ValueError, TypeError):
                            pass
                return out

            pipe = client.pipeline()
            for code in codes:
                pipe.get(f"hot_sector:score:{code}")
            results = pipe.execute()

            out = {}
            for code, val in zip(codes, results):
                if val is not None:
                    try:
                        out[code] = float(val)
                    except (ValueError, TypeError):
                        pass
            return out
        except Exception as e:
            log.debug(f"缓存批量读取失败: {e}")
            return {}

    def _batch_write_redis(self, scores: dict, trade_date: date):
        """批量写入缓存评分（Redis 优先，不可用时走内存缓存）"""
        try:
            from core.redis_manager import redis_manager
            client = redis_manager.get_client()
            if client:
                try:
                    pipe = client.pipeline()
                    for code, score in scores.items():
                        pipe.setex(f"hot_sector:score:{code}", self.redis_ttl, score)
                    pipe.execute()
                    # 更新时间戳
                    client.setex("hot_sector:last_update", self.redis_ttl,
                                 datetime.now().isoformat())
                    return
                except Exception as e:
                    log.debug(f"Redis 批量写入失败，降级内存缓存: {e}")

            # 降级到内存缓存
            for code, score in scores.items():
                redis_manager.cache_setex(f"hot_sector:score:{code}", self.redis_ttl, score)
            redis_manager.cache_setex("hot_sector:last_update", self.redis_ttl,
                                      datetime.now().isoformat())
        except Exception as e:
            log.debug(f"缓存批量写入失败: {e}")

    # ─── 刷新 ────────────────────────────────────────────────

    def refresh_hotness(self, use_redis: bool = True) -> dict:
        """全量刷新热度并写入缓存"""
        with self._calc_lock:
            calculator = HeatCalculator()
            today = date.today()
            summary = calculator.calc_all_heat(trade_date=today)

            # 概念热度为 0 时自动重试同步（限频：最多每小时重试一次）
            if summary.get("concepts_processed", 0) == 0:
                import time
                now_ts = time.time()
                if now_ts - self._last_concept_sync_attempt > _CONCEPT_SYNC_RETRY_INTERVAL:
                    self._last_concept_sync_attempt = now_ts
                    log.info("概念热度为 0，自动重试概念数据同步...")
                    try:
                        from app.hot_sector.concept_sync import concept_syncer
                        sync_result = concept_syncer.sync_all()
                        if sync_result.get("total_concepts", 0) > 0:
                            log.info(f"概念同步成功: {sync_result['total_concepts']} 概念, 重新计算热度")
                            summary = calculator.calc_all_heat(trade_date=today)
                        else:
                            log.warning(f"概念同步仍无数据: {sync_result.get('message', '未知原因')}")
                    except Exception as e:
                        log.warning(f"概念自动重试同步失败: {e}")

            if use_redis:
                # 预热热门板块/概念到缓存（Redis 或内存）
                try:
                    self._cache_top_sectors_to_redis()
                except Exception as e:
                    log.debug(f"缓存热门板块失败: {e}")

            # 记录本次重算时间
            now_str = datetime.now().strftime("%m/%d %H:%M")
            settings.set("hot_sector", "last_updated", now_str, save=False)
            settings.save()

            return summary

    @property
    def last_updated(self) -> str:
        """获取最近一次重算的时间字符串"""
        return str(settings.get("hot_sector", "last_updated", default="--"))

    def _cache_top_sectors_to_redis(self):
        """将 TOP 板块和概念缓存（Redis 优先，不可用时走内存缓存）"""
        from core.redis_manager import redis_manager
        today = date.today()

        # 热门概念 TOP 50
        try:
            rows = db.conn.execute("""
                SELECT concept_name, hotness, constituent_count
                FROM concept_heat WHERE trade_date = ?
                ORDER BY hotness DESC LIMIT 50
            """, [today]).fetchall()
            if rows:
                data = json.dumps([{"name": r[0], "hotness": round(float(r[1]), 2),
                                     "count": int(r[2])} for r in rows])
                redis_manager.cache_setex("hot_sector:top_concepts", self.redis_ttl, data)
        except Exception as e:
            log.debug(f"缓存热门概念失败: {e}")

        # 热门板块 TOP 30
        try:
            rows = db.conn.execute("""
                SELECT sector_name, hotness, constituent_count
                FROM sector_heat WHERE trade_date = ?
                ORDER BY hotness DESC LIMIT 30
            """, [today]).fetchall()
            if rows:
                data = json.dumps([{"name": r[0], "hotness": round(float(r[1]), 2),
                                     "count": int(r[2])} for r in rows])
                redis_manager.cache_setex("hot_sector:top_sectors", self.redis_ttl, data)
        except Exception as e:
            log.debug(f"缓存热门板块失败: {e}")

    # ─── 查询 ────────────────────────────────────────────────

    def get_top_concepts(self, limit: int = 20, min_stocks: int = 3) -> pd.DataFrame:
        """获取热门概念 TOP N，当日无数据时自动回退到最近交易日"""
        base_query = """
            SELECT concept_name AS name, hotness, constituent_count AS count,
                   advance_count, decline_count, trade_date
            FROM concept_heat
            WHERE constituent_count >= ? AND trade_date = ?
            ORDER BY hotness DESC LIMIT ?
        """
        df = db.conn.execute(base_query, [min_stocks, date.today(), limit]).df()
        if df.empty:
            latest = db.conn.execute("SELECT MAX(trade_date) FROM concept_heat").fetchone()
            if latest and latest[0]:
                df = db.conn.execute(base_query, [min_stocks, latest[0], limit]).df()
        return df

    def get_top_sectors(self, limit: int = 20, min_stocks: int = 3) -> pd.DataFrame:
        """获取热门行业板块 TOP N，当日无数据时自动回退到最近交易日"""
        base_query = """
            SELECT sector_name AS name, hotness, constituent_count AS count,
                   advance_count, decline_count, trade_date
            FROM sector_heat
            WHERE constituent_count >= ? AND trade_date = ?
            ORDER BY hotness DESC LIMIT ?
        """
        df = db.conn.execute(base_query, [min_stocks, date.today(), limit]).df()
        if df.empty:
            latest = db.conn.execute("SELECT MAX(trade_date) FROM sector_heat").fetchone()
            if latest and latest[0]:
                df = db.conn.execute(base_query, [min_stocks, latest[0], limit]).df()
        return df

    def cache_valid(self) -> bool:
        """检查缓存是否仍有效（TTL 内）"""
        try:
            from core.redis_manager import redis_manager
            ts = redis_manager.cache_get("hot_sector:last_update")
            if ts is None:
                return False
            last = datetime.fromisoformat(ts if isinstance(ts, str) else ts.decode() if isinstance(ts, bytes) else str(ts))
            return (datetime.now() - last).total_seconds() < self.redis_ttl
        except Exception:
            return False


# 全局单例
hot_sector_engine = HotSectorEngine()
