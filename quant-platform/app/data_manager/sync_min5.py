import os
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from core.logger import get_logger

log = get_logger("SyncIntraday")

# ─── 全局停止标志（线程安全的软中断机制）──────────────────────────────
_stop_requested = False

def stop_qmt_sync():
    """由外部接口调用，设置停止标志位，让同步循环在下一批次之前退出"""
    global _stop_requested
    _stop_requested = True
    log.info("🛑 [QMT] 已接收到用户手动中断指令，将在当前批次结束后停止...")

def reset_qmt_stop_flag():
    """每次开始新同步任务时，必须先重置停止标志"""
    global _stop_requested
    _stop_requested = False


def log_to_ui(msg, level="info"):
    """向前端 WebSocket 推送实时进度日志"""
    try:
        import requests
        requests.post("http://127.0.0.1:8000/api/data/sync_log",
                     json={"msg": msg, "level": level},
                     timeout=0.5)
    except:
        pass


def sync_qmt_intraday(freq="5m", days=30, batch_size=60, start_date=None, end_date=None):
    """
    QMT 分时同步：支持定向补救与 30 天回溯。
    新增：_stop_requested 检测，用户可通过停止接口软中断当前任务。
    """
    import os, time  # 额外导入确保线程内可见

    # ── 重置停止标志，确保每次启动是干净的 ──
    reset_qmt_stop_flag()
    try:
        from xtquant import xtdata
    except ImportError:
        log_to_ui("当前处于非 Windows 环境，缺少 xtquant 交易库，必须通过代理进行同步", "error")
        return False

    period_label = "daily" if freq == "daily" else ("min5" if freq == "5m" else "min1")
    qmt_period = "1d" if freq == "daily" else freq
    
    # 使用相对路径，确保在任何目录下都能运行
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    DATA_DIR = os.path.join(base_dir, "data", "parquet", period_label)
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

    # 确定同步时间区间
    if start_date:
        start_time_long = f"{start_date.replace('-','')}000000"
        display_start = start_date
    else:
        start_date_dt = datetime.now() - timedelta(days=days)
        display_start = start_date_dt.strftime("%Y%m%d")
        start_time_long = f"{display_start}000000"

    end_time_long = f"{end_date.replace('-','')}235959" if end_date else datetime.now().strftime("%Y%m%d235959")

    interval_info = f"区间: {display_start} - {end_date or '今'}" if start_date else f"追溯: {days}天"
    log_to_ui(f"🚀 [QMT] 启动 {period_label} 同步 | {interval_info}")
    log_to_ui(f" [QMT] 启动 {period_label} 同步 | {interval_info}")

    # 1. 扫描市场标的
    try:
        # 3. 确定标的池
        all_stocks = xtdata.get_stock_list_in_sector('上证A股') + xtdata.get_stock_list_in_sector('深证A股')
        all_stocks = sorted(list(set(all_stocks)))
        total = len(all_stocks)
        print(f">>> [DISK] Current DATA_DIR: {DATA_DIR}")
        print(f">>> [TASK] Found {total} stocks, ready to download...")
        log_to_ui(f"Scan finished. Found {total} stocks, start downloading...")
    except Exception as e:
        log_to_ui(f" 无法连接 QMT 客户端: {e}", "error")
        return False

    # 2. 分批调度
    for i in range(0, total, batch_size):
        # ── 每批次开始前检查停止信号 ──
        if _stop_requested:
            log.info(" [QMT] 检测到停止指令，同步已安全中断。")
            log_to_ui(" 用户手动中断，已安全停止 QMT 同步任务。", "warning")
            return False

        batch = all_stocks[i: i + batch_size]
        curr_idx = i // batch_size + 1
        total_p = (total + batch_size - 1) // batch_size

        if curr_idx % 5 == 0 or curr_idx == 1:
            log_to_ui(f" 任务进度: {curr_idx}/{total_p} 批 ({len(batch)} 只)")

        import threading
        # 创建回调事件卡口，保障异步变同步
        download_event = threading.Event()
        
        def on_batch_download_progress(data):
            print(f">>> [PROGRESS] Callback: {data}")
            # 每当下发全部完成后才放行（必须监测 finished == total）
            if data.get('finished') == data.get('total'):
                print(f">>> [SIGNAL] Batch Finished. Releasing wait lock.")
                download_event.set()
                
        # 调用支持原生列表且速度极快的批量并发下发版 API
        print(f">>> [NET] Triggering QMT download2 API (Size: {len(batch)})...")
        xtdata.download_history_data2(
            stock_list=batch, period=qmt_period, 
            start_time=start_time_long, end_time=end_time_long, 
            callback=on_batch_download_progress
        )
        print(">>> [LOCK] Waiting for batch download to finish...")
        # 最长等待该批次 60 秒的网络下发时间（平时一般零点几秒内完成，防止由于网络波动死锁）
        download_event.wait(timeout=60.0)
        # 预留给极个别超大包体写盘缓冲
        time.sleep(0.5)

        try:
            res_data = xtdata.get_local_data(
                stock_list=batch, period=qmt_period,
                start_time=start_time_long, end_time=end_time_long, count=-1
            )
            if not res_data:
                log.warning(f" 批次 {curr_idx}: get_local_data 返回空字典。")
                continue

            written, skipped_empty = 0, 0
            time_col = "date" if freq == "daily" else "datetime"
            
            for s, df in res_data.items():
                if df is None or df.empty:
                    skipped_empty += 1
                    continue

                df = df.reset_index().rename(columns={'index': time_col, 'vol': 'volume'})
                
                if freq == "daily":
                    # 日线时 QMT 返回的 index 是 YYYYMMDD 格式的字符串或者是毫秒时间戳？
                    # xtdata 1d 返回毫秒或字符，安全起见交给 to_datetime
                    df[time_col] = pd.to_datetime(df[time_col], format='%Y%m%d', errors='coerce').dt.date
                else:    
                    df[time_col] = pd.to_datetime(df[time_col], format='%Y%m%d%H%M%S', errors='coerce')
                    df = df.dropna(subset=[time_col])
                    # 清理盘外异动线
                    df = df[(df[time_col].dt.hour >= 9) & (df[time_col].dt.hour <= 15)]

                if df.empty:
                    skipped_empty += 1
                    continue

                code_only = s.split('.')[0]
                save_path = os.path.join(DATA_DIR, f"{code_only}.parquet")
                print(f">>> [WRITE] {code_only}: Fetched {len(df)} rows. Target: {save_path}")

                if os.path.exists(save_path):
                    try:
                        old_df = pd.read_parquet(save_path)
                        if freq == "daily":
                            # 将 old_df 的 date 统一转义为 date obj 以免混合
                            old_df[time_col] = pd.to_datetime(old_df[time_col]).dt.date
                        df = pd.concat([old_df, df]).drop_duplicates(subset=[time_col])
                    except Exception as merge_err:
                        log.warning(f"⚠️ {code_only} 合并旧数据失败，将覆写: {merge_err}")

                df.sort_values(time_col).to_parquet(save_path, compression='snappy')
                written += 1

            # 每批次汇报写入情况，空批次也要报出来而不是静默
            if skipped_empty > 0:
                log.warning(f"批次 {curr_idx}: 写入 {written} 只，跳过空数据 {skipped_empty} 只")

        except Exception as e:
            log.warning(f"⚠️ 批次 {curr_idx} 转储异常: {e}")
            continue

    if not _stop_requested:
        log_to_ui(f"🏁 QMT {period_label} 同步圆满结束！", "success")
    return not _stop_requested


def get_qmt_intra_status(freq="5m", days=30, start_date=None, end_date=None):
    """探测数据的健康度与连续性支持任意时间区间探测"""
    import os  # 额外导入
    period_label = "daily" if freq == "daily" else ("min5" if freq == "5m" else "min1")
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    DATA_DIR = os.path.join(base_dir, "data", "parquet", period_label)

    if not os.path.exists(DATA_DIR) or not os.listdir(DATA_DIR):
        return {"error": f"本地 {freq} 存储目录为空或路径不正确 ({period_label})"}

    try:
        fixed_dir = DATA_DIR.replace("\\", "/")
        sample_path = f"{fixed_dir}/*.parquet"

        import duckdb
        conn = duckdb.connect(':memory:')

        # 动态处理日线与分时的时间列名差异（Tushare/Daily底层存的是 date，QMT分时是 datetime）
        time_col = "date" if freq == "daily" else "datetime"

        row = conn.execute(f"SELECT MAX({time_col}) FROM read_parquet('{sample_path}')").fetchone()
        res_latest = row[0] if row else None
        if res_latest:
            try:
                latest_str = res_latest.strftime("%Y-%m-%d %H:%M:%S")
            except AttributeError:
                latest_str = str(res_latest)
        else:
            latest_str = "无数据"

        f_list = os.listdir(DATA_DIR)
        first_file = os.path.join(DATA_DIR, f_list[0]).replace("\\", "/")

        # 动态构造 SQL 条件
        if start_date and end_date:
            condition = f"CAST({time_col} AS DATE) >= '{start_date}' AND CAST({time_col} AS DATE) <= '{end_date}'"
        else:
            condition = f"CAST({time_col} AS DATE) >= current_date - interval {days} day"

        sql = f"""
            SELECT CAST({time_col} AS DATE) as d, COUNT(*) as c
            FROM read_parquet('{first_file}')
            WHERE {condition}
            GROUP BY d ORDER BY d DESC
        """
        
        res_daily = conn.execute(sql).df()

        daily_counts = {}
        if not res_daily.empty:
            daily_counts = res_daily.set_index('d')['c'].to_dict()
            daily_counts = {str(k)[:10]: int(v) for k, v in daily_counts.items()}

        conn.close()
        
        ideal_c = 1
        if freq == "5m": ideal_c = 48
        elif freq == "1m": ideal_c = 240
        
        return {
            "latest_time": latest_str,
            "daily_counts": daily_counts,
            "ideal_count": ideal_c,
            "total_files": len(f_list)
        }
    except Exception as e:
        import traceback
        return {"error": f"统计引擎故障: {str(e)}", "trace": traceback.format_exc()}


if __name__ == "__main__":
    sync_qmt_intraday(freq="5m", days=30)
