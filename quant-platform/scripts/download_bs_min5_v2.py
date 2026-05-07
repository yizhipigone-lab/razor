#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
baostock 5分钟数据下载器 (ThreadPoolExecutor版)
简洁可靠的多线程下载
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import baostock as bs
import pandas as pd
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import threading

OUT_DIR = Path(__file__).parent.parent / "data" / "parquet" / "min5_bs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
START = "2022-01-01"
END   = "2026-05-02"
NUM_WORKERS = 5

_lock = threading.Lock()
_stats = {'ok': 0, 'skip': 0, 'fail': 0}

def get_all_stocks():
    """获取全部A股代码"""
    try:
        from xtquant import xtdata
        s = xtdata.get_stock_list_in_sector('上证A股') + xtdata.get_stock_list_in_sector('深证A股')
        return sorted(set(c.split('.')[0] for c in s))
    except:
        return []

def bs_code(c):
    return f'sh.{c}' if c.startswith(('6','5','9')) else f'sz.{c}'

def download_one(code):
    """下载一只股票，返回 (code, success, rows)"""
    out = OUT_DIR / f"{code}.parquet"
    if out.exists():
        try:
            old = pd.read_parquet(str(out))
            if 'datetime' in old.columns and len(old) > 1000:
                old['datetime'] = pd.to_datetime(old['datetime'])
                if old['datetime'].max().strftime('%Y-%m-%d') >= '2026-04-01':
                    with _lock:
                        _stats['skip'] += 1
                    return (code, 'skip', len(old))
        except:
            pass

    # 每个线程独立登录
    lg = bs.login()
    if lg.error_code != '0':
        with _lock:
            _stats['fail'] += 1
        return (code, 'fail', 0)

    try:
        rs = bs.query_history_k_data_plus(bs_code(code),
            'date,time,open,high,low,close,volume,amount',
            start_date=START, end_date=END, frequency='5', adjustflag='3')
        if rs.error_code != '0':
            bs.logout()
            with _lock: _stats['fail'] += 1
            return (code, 'fail', 0)

        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        bs.logout()

        if not rows:
            with _lock: _stats['fail'] += 1
            return (code, 'fail', 0)

        df = pd.DataFrame(rows, columns=['date','time','open','high','low','close','volume','amount'])
        df['datetime'] = pd.to_datetime(df['date'].astype(str) + ' ' +
                          df['time'].astype(str).str[:2] + ':' +
                          df['time'].astype(str).str[2:4] + ':' +
                          df['time'].astype(str).str[4:6])
        for c in ['open','high','low','close','volume','amount']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna(subset=['open','high','low','close'])
        df = df.drop(columns=['date','time'])
        df = df.sort_values('datetime')
        df.to_parquet(str(out), index=False)

        with _lock: _stats['ok'] += 1
        return (code, 'ok', len(df))

    except Exception as e:
        with _lock: _stats['fail'] += 1
        try: bs.logout()
        except: pass
        return (code, 'error', 0)


def main():
    print(f"baostock 5min downloader | {START} ~ {END} | {NUM_WORKERS} threads")
    stocks = get_all_stocks()
    if not stocks:
        print("ERROR: no stocks")
        return
    print(f"Total: {len(stocks)} stocks")
    print(f"Output: {OUT_DIR}")

    t0 = time.time()
    done = 0
    total_rows = 0

    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as ex:
        futures = {ex.submit(download_one, c): c for c in stocks}
        for f in as_completed(futures):
            code, status, rows = f.result()
            done += 1
            total_rows += rows
            if done % 200 == 0:
                elapsed = time.time() - t0
                rate = done / elapsed * 60
                remaining = (len(stocks) - done) / rate if rate > 0 else 0
                print(f"  [{datetime.now().strftime('%H:%M:%S')}] {done}/{len(stocks)} "
                      f"({done/len(stocks)*100:.1f}%) | "
                      f"ok:{_stats['ok']} skip:{_stats['skip']} fail:{_stats['fail']} | "
                      f"{rate:.0f}/min | ETA {remaining:.0f}min | {total_rows:,} rows")

    elapsed = time.time() - t0
    print(f"\nDone! {elapsed/60:.1f}min | ok:{_stats['ok']} skip:{_stats['skip']} fail:{_stats['fail']} | {total_rows:,} rows")


if __name__ == "__main__":
    main()
