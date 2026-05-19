#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""断点续传：补充剩余 min5 缺口数据"""
import sys, os
import baostock as bs
import pandas as pd
import numpy as np
from datetime import date
from pathlib import Path
import time
import signal

ROOT = Path(__file__).parent.parent
MIN5_DIR = ROOT / "data" / "parquet" / "min5"
GAP_START = "2024-08-01"
GAP_END   = "2025-03-31"

def to_bs(code):
    return f"sh.{code}" if code.startswith(('6','5','9')) else f"sz.{code}"

def out_path(code):
    suffix = "SH" if code.startswith(('6','5','9')) else "SZ"
    return MIN5_DIR / f"{code}{suffix}.parquet"

def download_save(code):
    bs_code = to_bs(code)
    try:
        rs = bs.query_history_k_data_plus(
            bs_code, 'date,time,open,high,low,close,volume,amount',
            start_date=GAP_START, end_date=GAP_END,
            frequency='5', adjustflag='3'
        )
        if rs.error_code != '0':
            return 'api_err'

        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return 'empty'

        df = pd.DataFrame(rows, columns=['date','time','open','high','low','close','volume','amount'])
        df['datetime'] = pd.to_datetime(df['date'].str[:10] + ' ' + df['time'].str[:2] + ':' + df['time'].str[2:4], format='%Y-%m-%d %H:%M')
        for c in ['open','high','low','close','volume','amount']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna(subset=['open','high','low','close'])
        df = df.drop(columns=['date','time']).sort_values('datetime')

        fp = out_path(code)
        if fp.exists():
            old = pd.read_parquet(str(fp))
            old['datetime'] = pd.to_datetime(old['datetime'])
            merged = pd.concat([old, df]).drop_duplicates('datetime').sort_values('datetime')
        else:
            fp2 = MIN5_DIR / f"{code}.parquet"
            if fp2.exists():
                old = pd.read_parquet(str(fp2))
                old['datetime'] = pd.to_datetime(old['datetime'])
                merged = pd.concat([old, df]).drop_duplicates('datetime').sort_values('datetime')
            else:
                merged = df

        for c in ['open','high','low','close']:
            if c in merged.columns:
                merged[c] = pd.to_numeric(merged[c], errors='coerce')
        merged = merged.dropna(subset=['open','high','low','close'])
        merged.to_parquet(str(fp), index=False)
        return len(df)
    except Exception as e:
        return str(e)[:60]

# ── main ──
if __name__ == "__main__":
    # 读取剩余列表
    rem_file = ROOT / "data" / "min5_gap_remaining.txt"
    remaining = [l.strip() for l in open(rem_file) if l.strip()]
    total = len(remaining)
    print(f"Remaining: {total} stocks")

    bs.login()
    ok = empty = fail = rows_total = 0
    skipped = 0
    consecutive_fails = 0
    t0 = time.time()

    for i, code in enumerate(remaining):
        # 每 200 只刷新连接
        if i > 0 and i % 200 == 0:
            bs.logout()
            time.sleep(1)
            bs.login()
            consecutive_fails = 0

        result = download_save(code)

        if result == 'empty':
            empty += 1
            consecutive_fails = 0
        elif result == 'api_err':
            fail += 1
            consecutive_fails += 1
        elif isinstance(result, str):
            fail += 1
            consecutive_fails += 1
            if fail <= 10:
                print(f"\n  ERR {code}: {result}")
        else:
            ok += 1
            rows_total += result
            consecutive_fails = 0

        # 连续失败超过 20 次，刷新连接
        if consecutive_fails >= 20:
            print(f"\n  {consecutive_fails} consecutive fails, reconnecting...")
            bs.logout()
            time.sleep(3)
            bs.login()
            consecutive_fails = 0

        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            done = i + 1
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            sys.stdout.write(f"\r  [{done}/{total}] OK:{ok} EMPTY:{empty} FAIL:{fail} "
                           f"rows:{rows_total:,} rate:{rate:.1f}/s ETA:{eta/60:.0f}min   ")
            sys.stdout.flush()

    bs.logout()
    elapsed = time.time() - t0
    print(f"\n\nDone: {elapsed:.0f}s | OK:{ok} EMPTY:{empty} FAIL:{fail} | {rows_total:,} rows")
