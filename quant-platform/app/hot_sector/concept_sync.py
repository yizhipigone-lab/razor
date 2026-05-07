"""
Tushare 概念数据同步器

从 Tushare API 同步概念分类和成分股映射到 DuckDB concept_stocks 表。
支持标准概念 (concept) 和同花顺概念 (ths_concept) 两种数据源。
"""

import time
import pandas as pd
from core.logger import get_logger
from database.duckdb_manager import db

log = get_logger("ConceptSync")


class TushareConceptSyncer:
    """Tushare 概念数据同步器"""

    def __init__(self):
        self.pro = None
        self._init_tushare()

    def _init_tushare(self):
        """复用项目中已有的 tushare_fetcher"""
        try:
            from app.data_manager.tushare_api import tushare_fetcher
            self.pro = tushare_fetcher.pro
        except Exception as e:
            log.warning(f"无法从 tushare_fetcher 获取 pro 实例: {e}")
        if self.pro is None:
            try:
                import os
                import tushare as ts
                token = os.environ.get("TUSHARE_KEY", "")
                if token:
                    ts.set_token(token)
                    self.pro = ts.pro_api()
                else:
                    log.error("TUSHARE_KEY 未设置")
            except Exception as e:
                log.error(f"初始化 Tushare pro 失败: {e}")

    def sync_all(self, progress_cb=None) -> dict:
        """
        全量同步概念数据。
        优先尝试标准概念，失败则降级到同花顺概念。
        两者均失败时返回 warning（不影响板块热度功能）。
        """
        if self.pro is None:
            return {"status": "error", "message": "Tushare API 未初始化"}

        total_concepts = 0
        total_mappings = 0
        failed = []

        # 尝试标准概念 (concept)
        concept_ok = False
        try:
            result = self._sync_concept('concept', progress_cb)
            total_concepts += result['concepts']
            total_mappings += result['mappings']
            failed.extend(result['failed'])
            if result['concepts'] > 0:
                concept_ok = True
                log.info(f"标准概念同步完成: {result['concepts']} 概念, {result['mappings']} 映射")
        except Exception as e:
            log.warning(f"标准概念同步失败: {e}")

        if not concept_ok:
            try:
                result = self._sync_concept('ths_concept', progress_cb)
                total_concepts += result['concepts']
                total_mappings += result['mappings']
                failed.extend(result['failed'])
                if result['concepts'] > 0:
                    concept_ok = True
                    log.info(f"同花顺概念同步完成: {result['concepts']} 概念, {result['mappings']} 映射")
            except Exception as e2:
                log.warning(f"同花顺概念也同步失败: {e2}")

        if not concept_ok:
            log.warning("概念数据同步不可用（Tushare API 权限或网络问题），板块热度功能不受影响")
            return {"status": "warning", "message": "概念同步不可用，仅板块热度可用",
                    "total_concepts": 0, "total_mappings": 0}

        return {
            "status": "ok",
            "total_concepts": total_concepts,
            "total_mappings": total_mappings,
            "failed_concepts": failed[:10]
        }

    def _sync_concept(self, source: str, progress_cb=None) -> dict:
        """
        同步指定来源的概念数据。
        source: 'concept' -> pro.concept(), 'ths_concept' -> pro.ths_concept()
        """
        source_map = {
            'concept': {'list_api': 'concept', 'detail_api': 'concept_detail', 'id_field': 'code'},
            'ths_concept': {'list_api': 'ths_concept', 'detail_api': 'ths_concept_detail', 'id_field': 'code'},
        }
        cfg = source_map.get(source)
        if not cfg:
            return {"concepts": 0, "mappings": 0, "failed": []}

        # Step 1: 获取概念列表
        list_api = getattr(self.pro, cfg['list_api'])
        df_concepts = list_api()
        if df_concepts is None or df_concepts.empty:
            log.warning(f"{source}: 概念列表为空")
            return {"concepts": 0, "mappings": 0, "failed": []}

        concept_list = df_concepts.to_dict('records')
        log.info(f"{source}: 获取到 {len(concept_list)} 个概念分类")

        # Step 2: 逐概念获取成分股
        all_rows = []
        failed = []
        id_field = cfg['id_field']
        detail_api = getattr(self.pro, cfg['detail_api'])

        for i, concept in enumerate(concept_list):
            concept_id = concept.get(id_field, '')
            concept_name = concept.get('concept_name') or concept.get('name', concept_id)
            if not concept_id:
                continue

            try:
                df_detail = detail_api(id=concept_id)
                if df_detail is not None and not df_detail.empty:
                    for _, row in df_detail.iterrows():
                        ts_code = row.get('ts_code', '')
                        if not ts_code:
                            continue
                        # 统一代码格式: 去掉后缀用于存储
                        stock_code = ts_code.split('.')[0] if '.' in ts_code else ts_code
                        all_rows.append({
                            'concept_name': concept_name,
                            'stock_code': stock_code,
                            'source': source
                        })
            except Exception as e:
                failed.append(concept_name)
                log.debug(f"{source}: 获取概念 '{concept_name}' 成分股失败: {e}")

            # 进度回调
            if progress_cb and i % 10 == 0:
                progress_cb(i + 1, len(concept_list), f"同步 {source} 概念 {i+1}/{len(concept_list)}")

            time.sleep(0.5)  # Tushare 限流保护

        # Step 3: 批量写入 DuckDB
        if all_rows:
            df = pd.DataFrame(all_rows)
            df['source'] = source
            db.upsert_concept_stocks(df)
            log.info(f"{source}: 写入 {len(all_rows)} 条概念映射到 DuckDB")
        else:
            log.warning(f"{source}: 无有效成分股数据")

        return {
            "concepts": len(set(r['concept_name'] for r in all_rows)),
            "mappings": len(all_rows),
            "failed": failed
        }


# 全局单例
concept_syncer = TushareConceptSyncer()
