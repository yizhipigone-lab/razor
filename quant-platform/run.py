"""
主程序入口 - A股量化交易平台
启动流程：
1. 加载全局配置
2. 初始化 DuckDB 数据库连接
3. 连接同花顺交易网关（根据配置）
4. 启动盘中监控事件引擎
5. 启动 CLI 交互界面（后续可替换为 GUI）
"""
import sys
from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db

log = get_logger("Main")


def get_gateway():
    """从统一管理器获取交易网关"""
    from core.gateway import get_gateway as _get_gw
    return _get_gw()


def cmd_download():
    """命令：初始化/增量更新股票数据"""
    from app.data_manager.engine import (
        get_all_stock_list, batch_download_all
    )
    log.info("=== 开始数据初始化/更新 ===")
    stocks = get_all_stock_list()
    if not stocks.empty:
        db.upsert_stocks(stocks)
    batch_download_all(freq="daily", years=1)
    batch_download_all(freq="min5")
    log.info("=== 数据更新完成 ===")


def cmd_scan(strategy_name: str, params: dict = None):
    """命令：执行选股扫描"""
    from app.screener.engine import ScreenerEngine
    engine = ScreenerEngine()

    def progress(step, total, msg):
        print(f"  [{step}/{total}] {msg}")

    results = engine.run_scan(
        strategy_name=strategy_name,
        strategy_params=params,
        progress_callback=progress,
    )
    print(f"\n共选出 {len(results)} 只股票:")
    for r in results:
        print(f"  {r.get('code')} {r.get('name','')}  收盘:{r.get('close',0):.2f}  日期:{r.get('date')}")
    return results


def cmd_backtest(strategy_name: str, start=None, end=None):
    """命令：执行历史回测"""
    from app.backtest.engine import backtest_engine
    from datetime import date

    def progress(step, total, msg):
        print(f"  [{step}/{total}] {msg}")

    result = backtest_engine.run(
        strategy_name=strategy_name,
        start=start,
        end=end,
        progress_callback=progress,
    )
    print(f"\n=== 回测结果 ===")
    print(f"总交易数: {result.total_trades}")
    print(f"胜率: {result.win_rate:.1f}%")
    print(f"平均收益: {result.total_pnl_pct:.2f}%")
    df = result.to_summary_df()
    if not df.empty:
        print(df[["code", "pnl_pct", "hold_days", "exit_reason"]].to_string(index=False))
    return result


def cmd_buy(code: str, price: float):
    """命令：手工买入"""
    from core.settings import calc_buy_volume
    gateway = get_gateway()
    volume = calc_buy_volume(price)
    if volume <= 0:
        print(f"股价 {price} 超出单笔限额 {settings.max_buy_amount}，无法买入")
        return
    confirm = input(f"确认买入 {code} x {volume} 股（约 {price*volume:.0f}元）？[y/N] ")
    if confirm.lower() == "y":
        success = gateway.buy(code, price, volume, reason="手工买入")
        if success:
            db.open_position(
                code=code, name=code, price=price, volume=volume,
                source="manual"
            )
            print(f"买入委托已发出: {code} x {volume}")


if __name__ == "__main__":
    log.info("====== A股量化平台启动 ======")
    log.info(f"金额上限: {settings.max_buy_amount} | 止损: {settings.hard_stop_loss_pct}% | 移动止盈激活: {settings.trailing_activate_pct}%")

    if len(sys.argv) < 2:
        print("""
用法:
  python run.py download              # 初始化/更新股票数据
  python run.py scan MA金叉           # 执行选股
  python run.py scan MACD金叉         # 执行选股
  python run.py backtest MA金叉       # 历史回测
  python run.py buy 000001 12.5       # 手工买入
        """)
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "download":
        cmd_download()
    elif cmd == "scan":
        strategy = sys.argv[2] if len(sys.argv) > 2 else "MA金叉"
        cmd_scan(strategy)
    elif cmd == "backtest":
        strategy = sys.argv[2] if len(sys.argv) > 2 else "MA金叉"
        cmd_backtest(strategy)
    elif cmd == "buy":
        if len(sys.argv) >= 4:
            cmd_buy(sys.argv[2], float(sys.argv[3]))
    else:
        print(f"未知命令: {cmd}")
