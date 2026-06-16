"""
启动时后台自动补齐最近 5 个交易日的缺失数据。

P0 → 日线 (parquet_pipeline)
P1 → 5 分钟线 (batch_download_all incremental)
P2 → 1 分钟线 (QMT xtquant)
"""

import threading
from datetime import datetime, timedelta
from core.sync_logger import info, warn, error, ok


def _run_async():
    """后台线程入口：按设置级别执行 P0/P1/P2"""
    try:
        from core.settings import settings
        level = settings.get("data", "auto_sync", default="daily")
        info(f"自动同步级别: {level}")

        info("=== 启动自动数据补齐（最近 5 个交易日）===")

        # ── P0: 日线（所有级别都执行）────────────────────────
        info("[P0/3] 开始检查并补齐日线数据...")
        try:
            from app.data_manager.parquet_pipeline import parquet_pipeline

            today = datetime.now().strftime("%Y%m%d")
            five_days_ago = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")

            def _p0_progress(curr, total, msg):
                info(f"[P0] {msg}")

            success = parquet_pipeline.sync_daily_klinesto_parquet(
                start_date=five_days_ago, end_date=today, progress_cb=_p0_progress
            )
            if success:
                ok("[P0/3] 个股日线数据补齐完成")
            else:
                warn("[P0/3] 个股日线同步未完成（可能 Tushare 不可用）")
        except Exception as e:
            error(f"[P0/3] 个股日线同步异常: {e}")

        # ── P0 附加：指数日线同步 ─────────────────────────
        info("[P0 附加] 开始同步指数日线数据...")
        try:
            from app.data_manager.tushare_sync import tushare_sync_manager
            idx_ok = tushare_sync_manager.sync_index_daily(
                start_date=five_days_ago, end_date=today, mode="incremental"
            )
            if idx_ok:
                ok("[P0 附加] 指数日线数据补齐完成")
            else:
                warn("[P0 附加] 指数日线同步未完成（可能 Tushare 不可用）")
        except Exception as e:
            error(f"[P0 附加] 指数日线同步异常: {e}")

        # 如果设置级别为 daily，P1/P2 跳过
        if level == "daily":
            info("同步级别为「仅日线」，跳过分钟线同步")
            info("=== 自动数据补齐全部完成 ===")
            return

        # ── P1: 5 分钟线 ──────────────────────────────────
        info("[P1/3] 开始检查并补齐 5 分钟线数据...")
        try:
            from app.data_manager.engine import batch_download_all

            def _p1_progress(curr, total, msg):
                info(f"[P1] {msg}")

            batch_download_all(freq="min5", years=1, mode="incremental", progress_cb=_p1_progress)
            ok("[P1/3] 5 分钟线数据补齐完成")
        except Exception as e:
            error(f"[P1/3] 5 分钟线同步异常: {e}")

        if level == "intraday":
            info("同步级别为「日线+5分钟」，跳过 1 分钟线同步")
            info("=== 自动数据补齐全部完成 ===")
            return

        # ── P2: 1 分钟线（QMT 仅在本地可用时执行）─────────
        info("[P2/3] 尝试检查 1 分钟线数据...")
        try:
            from xtquant import xtdata
            from app.data_manager.sync_min5 import sync_qmt_intraday
            info("[P2/3] QMT 在线，开始同步 1 分钟线（最近5天）...")
            sync_qmt_intraday(freq="1m", days=5)
            ok("[P2/3] 1 分钟线数据补齐完成")
        except ImportError:
            warn("[P2/3] 未安装 xtquant 库，跳过 1 分钟线同步")
        except Exception as e:
            warn(f"[P2/3] 跳过 1 分钟线: {e}")

        info("=== 自动数据补齐全部完成 ===")

    except Exception as e:
        error(f"自动数据补齐主流程异常: {e}")


def start_auto_sync():
    """启动后台自动同步线程（非阻塞），检查设置决定是否执行"""
    from core.settings import settings
    level = settings.get("data", "auto_sync", default="daily")
    if level == "off":
        info("自动数据同步已禁用（auto_sync=off）")
        return

    t = threading.Thread(target=_run_async, daemon=True, name="AutoSync")
    t.start()
    info("后台自动数据同步线程已创建（不阻塞页面加载）")
