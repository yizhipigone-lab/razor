# -*- coding: utf-8 -*-
"""
QMT 指数日线同步隔离工作进程
专门同步主流指数（沪深300、上证50、中证500 等）的日线 OHLCV 数据。
与 qmt_sync_job.py 模式相同，作为隔离子进程运行在 Windows 宿主机上。
"""
import os
import sys
import time
import argparse
import pandas as pd
from datetime import datetime, timedelta
import traceback
import requests

import warnings
warnings.filterwarnings('ignore')

# ----------------- 基础环境配置 -----------------
QMT_PATH = r"D:\anti\tools\QMT\userdata_mini"
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT_DIR)

# 目标目录：与 tushare_sync.py 保持一致，写入同一位置
TARGET_DIR = os.path.join(ROOT_DIR, "data", "parquet", "daily")
os.makedirs(TARGET_DIR, exist_ok=True)

# 指数列表：与 index_config.py INDEX_MAP 保持一致
INDEX_CODES = [
    # 上证系列
    "000001.SH",   # 上证指数
    "000016.SH",   # 上证50
    "000010.SH",   # 上证180
    "000009.SH",   # 上证380
    # 中证系列
    "000300.SH",   # 沪深300
    "000905.SH",   # 中证500
    "000852.SH",   # 中证1000
    "000510.SH",   # 中证A500
    # 深证系列
    "399001.SZ",   # 深证成指
    "399004.SZ",   # 深证100
    "399009.SZ",   # 深证200
    "399007.SZ",   # 深证300
    # 主题指数
    "000688.SH",   # 科创50
    "399006.SZ",   # 创业板指
]


def log_to_ui(msg, level="info", msg_type="log"):
    """进程间通信：回送给 API 服务端的实时通道"""
    timestamp_msg = f"[IndexWorker] {msg}"
    print(timestamp_msg, flush=True)
    try:
        requests.post("http://127.0.0.1:8888/api/data/sync_log",
                      json={"msg": timestamp_msg, "level": level, "type": msg_type},
                      timeout=0.5)
    except Exception:
        try:
            requests.post("http://127.0.0.1:8000/api/data/sync_log",
                          json={"msg": timestamp_msg, "level": level, "type": msg_type},
                          timeout=0.2)
        except:
            pass


def execute_sync(start_date: str = None, end_date: str = None):
    log_to_ui("🚀 初始化 QMT 指数日线同步引擎...")
    try:
        from xtquant import xtdata
        xtdata.data_dir = QMT_PATH
        log_to_ui("✅ QMT 底层链路装载完毕")
    except Exception as e:
        log_to_ui(f"❌ 无法装载 xtquant 引擎: {e}", "error")
        return False

    # 解析日期范围：默认拉近 10 年
    if end_date:
        end_dt = datetime.strptime(end_date.replace("-", ""), "%Y%m%d")
    else:
        end_dt = datetime.now()
    if start_date:
        start_dt = datetime.strptime(start_date.replace("-", ""), "%Y%m%d")
    else:
        start_dt = end_dt - timedelta(days=3650)  # 10 年

    start_time_str = start_dt.strftime("%Y%m%d000000")
    end_time_str = end_dt.strftime("%Y%m%d235959")
    log_to_ui(f"⏳ 同步区间: {start_time_str} ~ {end_time_str}")

    total = len(INDEX_CODES)
    success_count = 0

    for i, ts_code in enumerate(INDEX_CODES):
        code_naked = ts_code.split(".")[0]  # 000300
        save_name = f"index_{code_naked}.parquet"  # index_000300.parquet
        save_path = os.path.join(TARGET_DIR, save_name)
        index_label = f"[{i+1}/{total}] {ts_code}"

        log_to_ui(f"🔃 正在同步 {index_label} ...")

        try:
            # 下载到 QMT 本地缓存
            xtdata.download_history_data(
                stock_code=ts_code,
                period="1d",
                start_time=start_time_str,
                end_time=end_time_str,
            )
            # 读取数据
            res = xtdata.get_market_data_ex(
                field_list=[],
                stock_list=[ts_code],
                period="1d",
                start_time=start_time_str,
                end_time=end_time_str,
                count=-1,
            )
            if not res or ts_code not in res:
                log_to_ui(f"⚠️ {index_label} 无返回数据，跳过", "warning")
                continue

            df = res[ts_code]
            if df is None or df.empty:
                log_to_ui(f"⚠️ {index_label} 数据为空，跳过", "warning")
                continue

            # 格式校准（与 qmt_sync_job.py 一致）
            df = df.reset_index().rename(columns={"index": "date", "vol": "volume"})
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce").dt.date
            df = df.dropna(subset=["date"])

            # 与已有 parquet 合并去重
            if os.path.exists(save_path):
                try:
                    old_df = pd.read_parquet(save_path)
                    old_df["date"] = pd.to_datetime(old_df["date"]).dt.date
                    df = pd.concat([old_df, df]).drop_duplicates(subset=["date"], keep="last")
                except Exception as e:
                    log_to_ui(f"⚠️ {code_naked} 历史文件合并失败，覆写: {e}", "warning")

            df = df.sort_values("date").reset_index(drop=True)
            df.to_parquet(save_path, compression="snappy")

            date_range = f"{df['date'].min()} ~ {df['date'].max()}" if not df.empty else "无数据"
            log_to_ui(f"✅ {index_label} 保存成功: {len(df)} 条（{date_range}）")
            success_count += 1

        except Exception as e:
            log_to_ui(f"❌ {index_label} 同步异常: {e}", "error")
            traceback.print_exc()

        time.sleep(0.3)  # QMT 限流保护

    log_to_ui(f"🎉 指数日线同步完成: {success_count}/{total} 成功", level="success", msg_type="done")
    return success_count > 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QMT Index Daily Sync Worker")
    parser.add_argument("--start", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="YYYY-MM-DD")
    args = parser.parse_args()
    execute_sync(start_date=args.start, end_date=args.end)
