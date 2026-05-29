# -*- coding: utf-8 -*-
"""
QMT Independent Sync Worker (隔离子进程版)
专门应对多线程环境下的 QMT C++ 底层死锁问题。
代理服务器通过 subprocess 启动本脚本，本脚本独享一个独立的 xtdata 连接环境。
运行完毕后进程销毁释放内存，彻底断绝“行情抓拍”和“大包下载”之间的竞争冲突。
"""

import os
import sys
import io
import time
import argparse
import pandas as pd
from datetime import datetime, timedelta
import threading
import traceback
import requests

# 强制 UTF-8 输出
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 屏蔽第三方库的烦人警告
import warnings
warnings.filterwarnings('ignore')

# ----------------- 基础环境配置 -----------------
QMT_PATH = r"D:\anti\tools\QMT\userdata_mini"
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
# 尝试复原项目的 sys.path，以调用现存的方法（如果需要）
sys.path.insert(0, ROOT_DIR)

def log_to_ui(msg, level="info", msg_type="log"):
    """
    进程间通信：回送给 Docker Backend 的实时通道。
    注意：Docker 在宿主机映射的端口通常是 8888。
    """
    timestamp_msg = f"[Worker] {msg}"
    print(timestamp_msg, flush=True)
    try:
        # 使用 127.0.0.1:8888 (宿主机访问 Docker 映射端口)
        requests.post("http://127.0.0.1:8888/api/data/sync_log",
                      json={"msg": timestamp_msg, "level": level, "type": msg_type},
                      timeout=0.5)
    except Exception as e:
        # 如果 8888 不通，尝试 8000 (容器内部直连模式)
        try:
             requests.post("http://127.0.0.1:8000/api/data/sync_log",
                           json={"msg": timestamp_msg, "level": level, "type": msg_type},
                           timeout=0.2)
        except:
             pass

# ----------------- 主战线逻辑 -----------------
def execute_sync(freq: str, days: int, start_date: str, end_date: str):
    
    # [核爆级强化] 在本隔离池中安全引导加载 xtdata！
    log_to_ui(f"🚀 初始化 QMT 隔离同步引擎 | 参数: Freq={freq}, Days={days}... ")
    try:
        from xtquant import xtdata
        # 极简模式下，xtdata 依赖底层的共享内存，无需进行远程 TCP connect。
        # 错误调用 connect 会报 IP 格式错误。仅需设置路径（可选）
        xtdata.data_dir = QMT_PATH
        log_to_ui("✅ QMT 底层链路装载完毕 (隔离内存池状态)")
    except Exception as e:
        log_to_ui(f"❌ 无法装载 xtquant 引擎: {e}", "error")
        return False

    period_label = "daily" if freq == "daily" else ("min5" if freq == "5m" else "min1")
    qmt_period = "1d" if freq == "daily" else freq
    
    # 构建纯物理路径 (抛弃系统层层的代理映射关系，直接锁定根磁盘)
    TARGET_DIR = os.path.join(ROOT_DIR, "data", "parquet", period_label)
    os.makedirs(TARGET_DIR, exist_ok=True)
    log_to_ui(f"📁 数据物理落地点: {TARGET_DIR}")

    # 解析跨度
    if start_date:
        start_time_long = f"{start_date.replace('-','')}000000"
        display_start = start_date
    else:
        start_date_dt = datetime.now() - timedelta(days=days)
        display_start = start_date_dt.strftime("%Y%m%d")
        start_time_long = f"{display_start}000000"

    end_time_long = f"{end_date.replace('-','')}235959" if end_date else datetime.now().strftime("%Y%m%d235959")
    log_to_ui(f"⏳ 同步区间已锁定: {start_time_long} 至 {end_time_long}")

    # 扫描存续标池
    try:
        all_stocks = xtdata.get_stock_list_in_sector('上证A股') + xtdata.get_stock_list_in_sector('深证A股')
        all_stocks = sorted(list(set(all_stocks)))
        total = len(all_stocks)
        log_to_ui(f"🎯 寻获市场可用标的: 共 {total} 只")
    except Exception as e:
        log_to_ui(f"❌ 获取市场代码组失败: {e}", "error")
        return False

    empty_stocks = []
    batch_size = 60
    # 为分批加紧控制，彻底剥去外部干扰
    for i in range(0, total, batch_size):
        batch = all_stocks[i: i + batch_size]
        curr_idx = i // batch_size + 1
        total_p = (total + batch_size - 1) // batch_size

        if curr_idx % 5 == 0 or curr_idx == 1:
            log_to_ui(f"🔃 正在推进批次 {curr_idx}/{total_p} ...")

        try:
            log_to_ui(f"    -> [NET] 放弃批量，启动逐只绝对安全强制下发 (Batch: {len(batch)})...")
            
            # 使用最稳定（可能较慢但绝不死锁）的单体纯同步版本接口降级
            # 我们在一个无限制生命期的隔离脚本中，完全可以承担串行的时间成本
            for code in batch:
                xtdata.download_history_data(
                    stock_code=code, 
                    period=qmt_period, 
                    start_time=start_time_long, 
                    end_time=end_time_long
                )
            
            log_to_ui(f"    -> [NET] 批次越过 C++ 防火墙，全数击穿，进入数据拼装...")
            
            # 因为是纯同步方法，执行到这里意味着物理磁盘确实已经写完对应的内存文件段
            
            # --- 直取实盘级清洗池模式 ---
            # 放弃 get_local_data 异构结构，改用官方最标准的 get_market_data_ex (自动完成多因子拆分与聚合)
            res_data = xtdata.get_market_data_ex(
                field_list=[], stock_list=batch, period=qmt_period,
                start_time=start_time_long, end_time=end_time_long, count=-1
            )
            
            if not res_data:
                continue

            written = 0
            time_col = "date" if freq == "daily" else "datetime"
            
            for code_raw, df in res_data.items():
                if df is None or df.empty:
                    continue

                # 格式校准
                df = df.reset_index().rename(columns={'index': time_col, 'vol': 'volume'})
                if freq == "daily":
                    # 安全转化为日期基类
                    df[time_col] = pd.to_datetime(df[time_col], format='%Y%m%d', errors='coerce').dt.date
                else:    
                    df[time_col] = pd.to_datetime(df[time_col], format='%Y%m%d%H%M%S', errors='coerce')
                    df = df.dropna(subset=[time_col])
                # 清剪尾声数据 (9:00 - 15:00 正常交易时段)
                    df = df[(df[time_col].dt.hour >= 9) & (df[time_col].dt.hour <= 15)]

                code_clean = code_raw.split('.')[0]
                
                if df.empty:
                    empty_stocks.append(code_clean)
                    continue

                save_path = os.path.join(TARGET_DIR, f"{code_clean}.parquet")

                # --------- 一致性缝接逻辑（与 duckdb_manager 对齐）---------
                if os.path.exists(save_path):
                    try:
                        old_df = pd.read_parquet(save_path)
                        if freq == "daily":
                            old_df[time_col] = pd.to_datetime(old_df[time_col]).dt.date
                        # 缝接，按照最新时间推翻存量冗余数据
                        df = pd.concat([old_df, df]).drop_duplicates(subset=[time_col], keep='last')
                    except Exception as merge_err:
                        # 倘若老旧 Parquet 受损，将其一举覆写，保留生机
                        log_to_ui(f"⚠️ {code_clean} 的历史簇已受损，进行全量覆写: {merge_err}", "warning")

                # 永远进行严格的升序排列重整，为回测组件提供优质土壤
                # 统一 time_col 类型，防止 concat 后混合 Timestamp/str 导致排序失败
                if freq == "daily":
                    df[time_col] = pd.to_datetime(df[time_col], errors='coerce').dt.date
                else:
                    df[time_col] = pd.to_datetime(df[time_col], errors='coerce')
                df.sort_values(time_col).to_parquet(save_path, compression='snappy')
                written += 1

            if written == 0 and len(res_data) > 0:
                 # 可能节假日
                 log_to_ui(f"    -> [DISK] 注意: 本批次未发生有效 K 线写入 (可能是非交易日或空停牌期)。", "warning")
            else:
                 log_to_ui(f"    -> [DISK] 成功清洗并复写 Parquet: {written} 只。")
            
            # 手动执行 Python 的 GC ，为庞大数据池减压
            import gc
            gc.collect()

        except Exception as e:
            log_to_ui(f"❌ 批次 {curr_idx} 脱靶异常: {str(e)}", "error")
            traceback.print_exc()

    if empty_stocks:
        log_to_ui(f"🔍 提示: 本次同步过程中，共有 {len(empty_stocks)} 只股票在截取时段内无有效数据(停牌/未上市)。", "info")
        # 若数量不是特别多，可以列出明细以便审计
        if len(empty_stocks) <= 100:
            log_to_ui(f"🧩 未同步缺失数据明细: {','.join(empty_stocks)}", "info")

    log_to_ui(f"🎉 专线同步引擎 ({freq}) 全面告捷！物理存储已同步就绪。", level="success", msg_type="done")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QMT Isolated Parquet Sync Worker")
    parser.add_argument("--freq", type=str, default="5m", help="daily or 5m or 1m")
    parser.add_argument("--days", type=int, default=30, help="lookback days")
    parser.add_argument("--start", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="YYYY-MM-DD")
    
    args = parser.parse_args()
    execute_sync(freq=args.freq, days=args.days, start_date=args.start, end_date=args.end)
