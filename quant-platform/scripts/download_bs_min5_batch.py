#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
baostock 5分钟数据高效下载器 (2022-01-01 ~ 2026-05-02)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
策略：
- 仅下载有信号的股票（~3700只），大幅减少下载量
- 多年合并查询（减少API调用次数）
- 断点续传（已下载的自动跳过）
- 多线程并行（4线程，注意baostock限制）
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import baostock as bs
import pandas as pd
from datetime import datetime
from pathlib import Path
import time
import threading
import queue
import signal

# 配置
OUT_DIR = Path(__file__).parent.parent / "data" / "parquet" / "min5_bs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = "2022-01-01"
END_DATE   = "2026-05-02"
FREQ = '5'  # 5分钟

NUM_THREADS = 3  # baostock限制，不要太多
BATCH_REPORT = 50  # 每50只报告一次进度

# 全局控制
_stop_flag = False
_lock = threading.Lock()
_stats = {'done': 0, 'skip': 0, 'fail': 0, 'rows': 0}


def get_signal_stocks():
    """从回测信号中提取需要下载的股票代码列表"""
    try:
        from xtquant import xtdata
        stocks = xtdata.get_stock_list_in_sector('上证A股') + xtdata.get_stock_list_in_sector('深证A股')
        return sorted(list(set(s.replace('.SH', '').replace('.SZ', '') for s in stocks)))
    except:
        pass
    # Fallback: 使用 baostock 获取
    # 上证: 600000-605999, 688000-689999
    # 深证: 000001-003999, 300000-301999
    codes = []
    for prefix in ['60', '00', '30', '68']:
        # 生成范围
        pass
    return []


def bs_code(code: str) -> str:
    """纯数字代码 → baostock格式"""
    if code.startswith(('6', '5', '9')):
        return f'sh.{code}'
    return f'sz.{code}'


def download_one_stock(code: str, start: str, end: str) -> pd.DataFrame:
    """下载单只股票的5分钟数据（单次查询覆盖全区间）"""
    bs_code_str = bs_code(code)
    try:
        rs = bs.query_history_k_data_plus(
            bs_code_str,
            'date,time,open,high,low,close,volume,amount',
            start_date=start, end_date=end,
            frequency=FREQ, adjustflag='3'
        )
        if rs.error_code != '0':
            return pd.DataFrame()

        rows = []
        while rs.next():
            rows.append(rs.get_row_data())

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=['date','time','open','high','low','close','volume','amount'])
        df['datetime'] = pd.to_datetime(df['date'] + ' ' + df['time'].str[:2] + ':' +
                                         df['time'].str[2:4] + ':' + df['time'].str[4:6])
        for c in ['open','high','low','close','volume','amount']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna(subset=['open','high','low','close'])
        df = df.drop(columns=['date', 'time'])
        return df.sort_values('datetime')
    except Exception as e:
        return pd.DataFrame()


def worker(thread_id, code_queue):
    """工作线程"""
    global _stop_flag
    # 每个线程独立登录
    lg = bs.login()
    if lg.error_code != '0':
        print(f'[Thread-{thread_id}] 登录失败: {lg.error_msg}')
        return

    while not _stop_flag:
        try:
            code = code_queue.get(timeout=5)
        except queue.Empty:
            break

        if code is None:
            break

        out_path = OUT_DIR / f"{code}.parquet"

        # 断点续传：检查是否已有完整数据
        if out_path.exists():
            try:
                existing = pd.read_parquet(str(out_path))
                if 'datetime' in existing.columns and len(existing) > 100:
                    existing['datetime'] = pd.to_datetime(existing['datetime'])
                    min_dt = existing['datetime'].min().strftime('%Y-%m-%d')
                    max_dt = existing['datetime'].max().strftime('%Y-%m-%d')
                    if min_dt <= START_DATE and max_dt >= '2026-04-01':
                        with _lock:
                            _stats['skip'] += 1
                        code_queue.task_done()
                        continue
            except:
                pass  # 文件损坏，重新下载

        # 下载
        df = download_one_stock(code, START_DATE, END_DATE)

        if df.empty:
            with _lock:
                _stats['fail'] += 1
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(str(out_path), index=False)
            with _lock:
                _stats['done'] += 1
                _stats['rows'] += len(df)

        code_queue.task_done()

        # 进度报告
        with _lock:
            total_done = _stats['done'] + _stats['skip'] + _stats['fail']
            if total_done % BATCH_REPORT == 0:
                print(f'  [{datetime.now().strftime("%H:%M:%S")}] '
                      f'完成:{_stats["done"]} 跳过:{_stats["skip"]} '
                      f'失败:{_stats["fail"]} 总行:{_stats["rows"]:,}')

        # 速率控制
        time.sleep(0.5)

    bs.logout()


def main():
    print("=" * 60)
    print(f"  baostock 5分钟数据批量下载")
    print(f"  区间: {START_DATE} ~ {END_DATE}")
    print(f"  线程: {NUM_THREADS}")
    print(f"  输出: {OUT_DIR}")
    print("=" * 60)

    # 获取股票列表
    print("\n[1] 获取股票列表...")
    stocks = get_signal_stocks()
    print(f"  共 {len(stocks)} 只")

    if not stocks:
        print("  无法获取股票列表，尝试使用默认范围")
        # 用QMT获取
        try:
            from xtquant import xtdata
            s1 = xtdata.get_stock_list_in_sector('上证A股')
            s2 = xtdata.get_stock_list_in_sector('深证A股')
            stocks = sorted(list(set(
                s.replace('.SH','').replace('.SZ','') for s in (s1 + s2)
            )))
            print(f"  QMT获取到 {len(stocks)} 只")
        except:
            print("  [错误] 无法获取股票列表")
            return

    # 过滤已完成的
    remaining = []
    skipped_now = 0
    for code in stocks:
        out_path = OUT_DIR / f"{code}.parquet"
        if out_path.exists():
            try:
                existing = pd.read_parquet(str(out_path))
                if 'datetime' in existing.columns and len(existing) > 100:
                    existing['datetime'] = pd.to_datetime(existing['datetime'])
                    if existing['datetime'].max().strftime('%Y-%m-%d') >= '2026-04-01':
                        skipped_now += 1
                        continue
            except:
                pass
        remaining.append(code)

    print(f"  已跳过: {skipped_now} 只 | 待下载: {len(remaining)} 只")
    if not remaining:
        print("  全部已完成！")
        return

    # 预估时间
    est_seconds = len(remaining) * 5.0 / NUM_THREADS  # ~5秒/只/线程
    est_hours = est_seconds / 3600
    print(f"  预估耗时: {est_hours:.1f} 小时 ({est_seconds/60:.0f} 分钟)")

    # 创建任务队列
    code_queue = queue.Queue()
    for c in remaining:
        code_queue.put(c)

    # 启动线程
    print(f"\n[2] 启动 {NUM_THREADS} 个下载线程...")
    threads = []
    for i in range(NUM_THREADS):
        t = threading.Thread(target=worker, args=(i, code_queue), daemon=True)
        t.start()
        threads.append(t)

    # 等待完成
    try:
        for t in threads:
            while t.is_alive():
                t.join(timeout=10)
    except KeyboardInterrupt:
        print("\n  用户中断...")
        global _stop_flag
        _stop_flag = True

    # 汇总
    print(f"\n[3] 完成!")
    print(f"  成功: {_stats['done']}  跳过: {_stats['skip']}  失败: {_stats['fail']}")
    print(f"  总行数: {_stats['rows']:,}")
    print(f"  输出: {OUT_DIR}")


if __name__ == "__main__":
    main()
