"""
Eurica Quant (睿奕量化) - 主程序入口
启动流程:
1. 加载全局配置
2. 初始化 DuckDB 数据库连接
3. 启动盘中监控事件引擎
4. 启动 CLI 交互界面(后续可替换为 GUI)

v5.3(2026-07-14): 删除了 cmd_buy / get_gateway,真实下单唯一入口是 live_trader 的 qmt_wrapper。
sim_trader 永远不真下单——见 docs/审计报告/项目质量审计_2026-07-13_全项目.md H4 + BROKER_ENABLED 删除决定。
"""
import sys
from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db

log = get_logger("Main")


def cmd_download():
    """命令:初始化/增量更新股票数据"""
    from app.data_manager.engine import (
        get_all_stock_list, batch_download_all
    )
    log.info("=== 开始数据初始化/更新 ===")
    stocks = get_all_stock_list()


if __name__ == "__main__":
    log.info("====== Eurica Quant 睿奕量化 启动 ======")
    log.info(f"金额上限: {settings.max_buy_amount} | 止损: {settings.hard_stop_loss_pct}% | 移动止盈激活: {settings.trailing_activate_pct}%")

    if len(sys.argv) < 2:
        print("""
用法:
  python run.py download              # 初始化/更新股票数据

真实下单唯一入口: live_trader:8001 /live/order(qmt_wrapper 直连 xtquant)。
sim_trader 不再真下单,见 docs/审计报告/项目质量审计_2026-07-13_全项目.md。
        """)
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "download":
        cmd_download()
    else:
        print(f"未知命令: {cmd}")