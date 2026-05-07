#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
baostock 5分钟数据 — 单线程稳健下载器
低并发 + 自动重试 + 断点续传
运行后自动在后台持续下载，直到全部完成
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import baostock as bs
import pandas as pd
from pathlib import Path
from datetime import datetime
import time
import random

OUT_DIR = Path(__file__).parent.parent / "data" / "parquet" / "min5_bs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
START = "2022-01-01"
END   = "2026-05-02"

def get_all_stocks():
    try:
        from xtquant import xtdata
        s = xtdata.get_stock_list_in_sector('上证A股') + xtdata.get_stock_list_in_sector('深证A股')
        return sorted(set(c.split('.')[0] for c in s))
    except:
        return []

def bs_code(c):
    return f'sh.{c}' if c.startswith(('6','5','9')) else f'sz.{c}'

def download_with_retry(code, max_retries=5):
    """带重试的单股票下载"""
    out = OUT_DIR / f"{code}.parquet"

    # Skip if already complete
    if out.exists():
        try:
            old = pd.read_parquet(str(out))
            if len(old) > 1000:
                old['datetime'] = pd.to_datetime(old['datetime'])
                if old['datetime'].max().strftime('%Y-%m-%d') >= '2026-04-01':
                    return 'skip'
        except:
            out.unlink(missing_ok=True)

    for attempt in range(max_retries):
        lg = bs.login()
        if lg.error_code != '0':
            time.sleep(2 ** attempt)
            continue

        try:
            # 分两段下载：2022-2023 和 2024-2026，减少单次数据量
            dfs = []
            for seg_start, seg_end in [(START, '2023-12-31'), ('2024-01-01', END)]:
                rs = bs.query_history_k_data_plus(bs_code(code),
                    'date,time,open,high,low,close,volume,amount',
                    start_date=seg_start, end_date=seg_end,
                    frequency='5', adjustflag='3')

                if rs.error_code != '0':
                    raise Exception(f"query error: {rs.error_msg}")

                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())

                if rows:
                    df = pd.DataFrame(rows, columns=['date','time','open','high','low','close','volume','amount'])
                    df['datetime'] = pd.to_datetime(df['date'].astype(str) + ' ' +
                                      df['time'].astype(str).str[:2] + ':' +
                                      df['time'].astype(str).str[2:4] + ':' +
                                      df['time'].astype(str).str[4:6])
                    for c in ['open','high','low','close','volume','amount']:
                        df[c] = pd.to_numeric(df[c], errors='coerce')
                    df = df.dropna(subset=['open','high','low','close'])
                    df = df.drop(columns=['date', 'time'])
                    dfs.append(df)

            bs.logout()

            if not dfs:
                return 'empty'

            merged = pd.concat(dfs).drop_duplicates('datetime').sort_values('datetime')
            merged.to_parquet(str(out), index=False)
            return 'ok'

        except Exception as e:
            try: bs.logout()
            except: pass
            err = str(e)[:80]
            if attempt < max_retries - 1:
                wait = (2 ** attempt) + random.uniform(1, 3)
                time.sleep(wait)
            else:
                return f'fail:{err}'

    return 'fail'


def main():
    print(f"baostock 5min downloader (single-thread, retry)")
    print(f"Range: {START} ~ {END}")

    stocks = get_all_stocks()
    if not stocks:
        print("ERROR: no stocks")
        return

    # Filter out already completed
    remaining = []
    skipped = 0
    for c in stocks:
        out = OUT_DIR / f"{c}.parquet"
        if out.exists():
            try:
                old = pd.read_parquet(str(out))
                if len(old) > 1000:
                    old['datetime'] = pd.to_datetime(old['datetime'])
                    if old['datetime'].max().strftime('%Y-%m-%d') >= '2026-04-01':
                        skipped += 1
                        continue
            except:
                out.unlink(missing_ok=True)
        remaining.append(c)

    print(f"Total: {len(stocks)} | Already done: {skipped} | To download: {len(remaining)}")
    est_hours = len(remaining) * 12 / 3600  # ~12s per stock
    print(f"Est. time: {est_hours:.1f} hours")

    ok = skip = fail = 0
    t0 = time.time()

    for i, code in enumerate(remaining):
        result = download_with_retry(code)

        if result == 'ok':
            ok += 1
        elif result == 'skip':
            skip += 1
        else:
            fail += 1

        # Progress every 50 stocks
        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            done = ok + skip + fail
            rate = done / elapsed * 60
            eta = (len(remaining) - done) / rate if rate > 0 else 0
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] {done}/{len(remaining)} "
                  f"| ok:{ok} skip:{skip} fail:{fail} | {rate:.1f}/min | ETA {eta:.0f}min")

        # Rate limiting: 0.5-1.5s between requests
        time.sleep(random.uniform(0.5, 1.0))

    elapsed = time.time() - t0
    print(f"\nDone! {elapsed/60:.1f}min | ok:{ok} skip:{skip} fail:{fail}")
    print(f"Output: {OUT_DIR}")


if __name__ == "__main__":
    main()
