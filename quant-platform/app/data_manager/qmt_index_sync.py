"""
QMT 指数成分股同步模块
通过 QMT Proxy 获取主流指数的实时成分股列表，写入 DuckDB。
替代 Tushare index_weight 接口（受积分限制）的功能。
"""
import os
import requests
from core.logger import get_logger
from database.duckdb_manager import db
from app.backtest.index_config import INDEX_QMT_SECTOR, INDEX_DISPLAY
from app.trader.gateways.qmt import qmt_gateway

log = get_logger("QMT-IndexSync")


class QmtIndexSyncer:
    """基于 QMT Proxy 的指数成分股同步器"""

    def _check_proxy_available(self) -> bool:
        """轻量预检 QMT Proxy 是否可达"""
        try:
            resp = requests.get(f"{qmt_gateway.proxy_url}/health", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    def _get_members(self, sector_name: str) -> list:
        """通过 QMTGateway 获取单个板块的成分股列表"""
        return qmt_gateway.get_index_members(sector_name)

    def sync_all(self, progress_cb=None) -> dict:
        """
        遍历所有已配置指数，通过 QMT Proxy 获取成分股并写入 DuckDB。
        progress_cb 可选回调 (curr, total, message)，兼容 Tushare 版接口。
        返回: {"success": bool, "success_count": int, "total": int, "failed": list}
        """
        total = len(INDEX_QMT_SECTOR)

        if total == 0:
            if progress_cb:
                progress_cb(0, 0, "⚠️ 未配置任何指数")
            return {"success": False, "success_count": 0, "total": 0, "failed": []}

        # 预检 Proxy 是否可达，避免逐指数超时等待
        if not self._check_proxy_available():
            msg = "QMT Proxy 不可达，请检查 Windows 端代理服务"
            log.error(f"qmt_index_sync | {msg}")
            if progress_cb:
                progress_cb(0, total, f"❌ {msg}")
            return {"success": False, "success_count": 0, "total": total, "failed": list(INDEX_QMT_SECTOR.keys())}

        success_count = 0
        failed = []

        for i, (key, sector_name) in enumerate(INDEX_QMT_SECTOR.items()):
            display = INDEX_DISPLAY.get(key, key)
            if progress_cb:
                progress_cb(i + 1, total, f"QMT 同步 {display} ({key})...")

            codes = self._get_members(sector_name)
            if codes:
                db.upsert_index_members(key, codes)
                log.info(f"qmt_index_sync | {display}: {len(codes)} 只成分股写入完成")
                success_count += 1
            else:
                log.error(f"qmt_index_sync | {display}: 未获取到成分股，跳过")
                failed.append(key)

        if progress_cb:
            status = f"QMT 指数同步完成：{success_count}/{total} 成功"
            if failed:
                status += f"，{len(failed)} 个指数失败"
            progress_cb(total, total, status)

        return {
            "success": success_count > 0,
            "success_count": success_count,
            "total": total,
            "failed": failed,
        }


qmt_index_syncer = QmtIndexSyncer()


# ── 指数日线行情同步 ────────────────────────────────────────

def sync_index_daily_qmt(progress_cb=None) -> bool:
    """
    通过 QMT Proxy dispatch 隔离子进程，同步主流指数日线 OHLCV 到本地 Parquet。
    与 sync_qmt_intra 相同 dispatch 模式。
    返回 True 表示 dispatch 成功（非实际同步结果）。
    """
    import os as _os
    proxy_host = _os.environ.get("LIVE_TRADER_HOST", _os.environ.get("QMT_PROXY_HOST", "127.0.0.1"))
    target_url = f"http://{proxy_host}:8001/live/sync/index_daily"
    payload = {}
    if progress_cb:
        progress_cb(0, 14, "🔄 正在向 QMT Proxy 发送指数日线同步指令...")
    try:
        resp = requests.post(target_url, json=payload, timeout=3)
        if resp.status_code == 200:
            log.info("指数日线同步已 dispatch 到 QMT 隔离子进程")
            if progress_cb:
                progress_cb(1, 14, "✅ 指令已发送，QMT 子进程在后台运行中...")
            return True
        else:
            log.error(f"QMT Proxy 返回异常: {resp.status_code} {resp.text}")
            return False
    except requests.exceptions.ConnectionError:
        log.error(f"无法连接 QMT Proxy ({target_url})，请检查 Windows 端代理服务")
    except Exception as e:
        log.error(f"QMT Proxy dispatch 异常: {e}")
    return False
