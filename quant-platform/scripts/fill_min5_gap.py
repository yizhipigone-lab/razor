#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""baostock min5 gap fill — serial with connection retry"""
import sys, os
import baostock as bs
import pandas as pd
import numpy as np
from datetime import date
from pathlib import Path
import time

ROOT = Path(__file__).parent.parent
MIN5_DIR = ROOT / "data" / "parquet" / "min5"
GAP_START = "2024-08-01"
GAP_END   = "2025-03-31"

def get_all_stocks():
    daily_dir = ROOT / "data" / "parquet" / "daily"
    return sorted([f.stem for f in daily_dir.glob("*.parquet")
                   if f.stem.isdigit() and len(f.stem) == 6])

def to_bs(code):
    return f"sh.{code}" if code.startswith(('6','5','9')) else f"sz.{code}"

def out_path(code):
    suffix = "SH" if code.startswith(('6','5','9')) else "SZ"
    return MIN5_DIR / f"{code}{suffix}.parquet"

def download_save(code):
    bs_code = to_bs(code)
    try:
        rs = bs.query_history_k_data_plus(
            bs_code,
            'date,time,open,high,low,close,volume,amount',
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
        return str(e)[:80]

# ── main ──
if __name__ == "__main__":
    all_stocks = get_all_stocks()
    total = len(all_stocks)
    print(f"目标: {total} 只 | 缺口: {GAP_START} ~ {GAP_END} | 单线程串行")

    # 持续登录，每100只刷新一次连接
    bs.login()
    ok = empty = fail = rows_total = 0
    t0 = time.time()

    for i, code in enumerate(all_stocks):
        if i > 0 and i % 100 == 0:
            # 定期刷新连接
            bs.logout()
            time.sleep(0.5)
            bs.login()

        result = download_save(code)

        if result == 'empty':
            empty += 1
        elif result == 'api_err':
            fail += 1
        elif isinstance(result, str):
            fail += 1
            if fail <= 5:
                print(f"\n  ERR {code}: {result}")
        else:
            ok += 1
            rows_total += result

        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            done = ok + empty + fail
            rate = done / elapsed
            eta = (total - done) / rate if rate > 0 else 0
            sys.stdout.write(f"\r  [{done}/{total}] OK:{ok} EMPTY:{empty} FAIL:{fail} "
                           f"rows:{rows_total:,} rate:{rate:.1f}/s ETA:{eta/60:.0f}min   ")
            sys.stdout.flush()

    bs.logout()
    elapsed = time.time() - t0
    done = ok + empty + fail
    print(f"\n\n完成: {elapsed:.0f}s | OK:{ok} EMPTY:{empty} FAIL:{fail} | 新增 {rows_total:,} 行")
