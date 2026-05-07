#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
baostock 5分钟数据下载器
下载上交所+深交所所有A股的5分钟K线数据
支持断点续传，增量更新
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import baostock as bs
import pandas as pd
from datetime import datetime, date, timedelta
from pathlib import Path
import time
import threading

# 输出目录
MIN5_BS_DIR = Path(__file__).parent.parent / "data" / "parquet" / "min5_bs"
MIN5_BS_DIR.mkdir(parents=True, exist_ok=True)

# 从 QMT stocks 数据库获取股票列表
def get_stock_list():
    """获取上交所+深交所所有A股代码"""
    try:
        from xtquant import xtdata
        stocks = xtdata.get_stock_list_in_sector('上证A股') + xtdata.get_stock_list_in_sector('深证A股')
        return sorted(list(set(s.replace('.SH', '').replace('.SZ', '') for s in stocks)))
    except:
        pass
    # 如果没有QMT，用固定列表
    return []


def download_stock_5min(code: str, start_date: str, end_date: str, retries=3):
    """
    下载单只股票的5分钟K线数据
    code: 纯数字代码，如 '000001'
    start_date/end_date: 'YYYY-MM-DD'
    """
    # 判断交易所
    if code.startswith(('6', '5', '9')):
        bs_code = f'sh.{code}'
    else:
        bs_code = f'sz.{code}'

    for attempt in range(retries):
        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                'date,time,open,high,low,close,volume,amount',
                start_date=start_date, end_date=end_date,
                frequency='5', adjustflag='3'
            )
            if rs.error_code != '0':
                time.sleep(1)
                continue

            rows = []
            while rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                return pd.DataFrame()

            df = pd.DataFrame(rows, columns=['date','time','open','high','low','close','volume','amount'])
            df['datetime'] = pd.to_datetime(df['date'] + ' ' + df['time'])
            for c in ['open','high','low','close','volume','amount']:
                df[c] = pd.to_numeric(df[c], errors='coerce')
            df = df.dropna(subset=['open','high','low','close'])
            df = df.drop(columns=['date', 'time'])
            return df.sort_values('datetime')

        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"  [ERROR] {code}: {e}")
    return pd.DataFrame()


def batch_sync_min5(start_date="2023-01-01", end_date="2026-05-02",
                    batch_size=20, batch_delay=3):
    """
    批量同步所有A股的5分钟数据

    策略：每次下载1个月的数据，逐月推进
    这样如果中断可以从中断点继续
    """
    stocks = get_stock_list()
    if not stocks:
        print("无法获取股票列表")
        return

    print(f"股票总数: {len(stocks)}")
    print(f"区间: {start_date} ~ {end_date}")
    print(f"输出: {MIN5_BS_DIR}")

    bs.login()

    # 按月分批
    months = pd.date_range(start=start_date, end=end_date, freq='MS')
    total_months = len(months) - 1

    for mi in range(total_months):
        m_start = months[mi].strftime('%Y-%m-%d')
        m_end = (months[mi+1] - timedelta(days=1)).strftime('%Y-%m-%d')

        print(f"\n[Month {mi+1}/{total_months}] {m_start} ~ {m_end}")

        for i in range(0, len(stocks), batch_size):
            batch = stocks[i:i+batch_size]
            written = 0
            skipped = 0

            for code in batch:
                out_path = MIN5_BS_DIR / f"{code}.parquet"

                # 检查是否已有本月数据
                if out_path.exists():
                    existing = pd.read_parquet(str(out_path))
                    existing['datetime'] = pd.to_datetime(existing['datetime'])
                    month_mask = (existing['datetime'] >= m_start) & (existing['datetime'] <= m_end + ' 23:59')
                    if month_mask.sum() > 0:
                        skipped += 1
                        continue

                # 下载
                df = download_stock_5min(code, m_start, m_end)
                if df.empty:
                    skipped += 1
                    continue

                # 合并保存
                if out_path.exists():
                    old = pd.read_parquet(str(out_path))
                    old['datetime'] = pd.to_datetime(old['datetime'])
                    merged = pd.concat([old, df]).drop_duplicates('datetime').sort_values('datetime')
                else:
                    merged = df

                merged.to_parquet(str(out_path), index=False)
                written += 1

            progress = min(i+batch_size, len(stocks))
            print(f"  [{progress}/{len(stocks)}] W:{written} S:{skipped}", end='\r')

            if i + batch_size < len(stocks):
                time.sleep(batch_delay)  # 速率控制

        print()  # 换行

    bs.logout()
    print("\n同步完成!")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2026-05-02")
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--delay", type=int, default=3)
    args = parser.parse_args()

    batch_sync_min5(args.start, args.end, args.batch, args.delay)
