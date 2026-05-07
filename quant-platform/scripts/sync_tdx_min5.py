"""
TDX 5分钟线 → Parquet 同步脚本
将通达信 .lc5 文件转换为 Parquet 格式，补齐现有 min5 数据
"""
import struct
import os
import glob
import time
import pandas as pd
from datetime import date, timedelta, datetime
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing as mp

BASE_DATE = date(1899, 12, 30)
TDX_SH = Path("E:/NEW_TDX/vipdoc/sh/fzline")
TDX_SZ = Path("E:/NEW_TDX/vipdoc/sz/fzline")
OUT_DIR = Path("E:/1target/p9_project/quant-platform/data/parquet/min5")

STRUCT = struct.Struct('HHfffffII')  # 32 bytes per record
BATCH_SIZE = 50000  # Process records in batches

def tdx_to_code(filepath: str) -> str:
    """sh600000.lc5 → 600000.SH, sz000001.lc5 → 000001.SZ"""
    name = os.path.basename(filepath)
    prefix = name[:2]
    num = name[2:8]
    if prefix == 'sh':
        return f"{num}.SH"
    elif prefix == 'sz':
        return f"{num}.SZ"
    return None

def parse_lc5(filepath: str) -> pd.DataFrame:
    """Parse a single .lc5 file into a DataFrame"""
    size = os.path.getsize(filepath)
    if size < 32:
        return None

    n_records = size // 32
    code = tdx_to_code(filepath)
    if code is None:
        return None

    rows = []
    with open(filepath, 'rb') as fp:
        chunk = fp.read(BATCH_SIZE * 32)
        while chunk:
            for i in range(0, len(chunk), 32):
                date_off, minute, op, hi, lo, cl, amt, vol, _ = STRUCT.unpack(chunk[i:i+32])
                d = BASE_DATE + timedelta(days=date_off)
                h, m = minute // 60, minute % 60
                dt = datetime(d.year, d.month, d.day, h, m)
                ts_ms = int(dt.timestamp() * 1000)
                rows.append({
                    'datetime': dt.strftime('%Y-%m-%d %H:%M:%S'),
                    'time': ts_ms,
                    'open': op,
                    'high': hi,
                    'low': lo,
                    'close': cl,
                    'volume': vol,
                    'amount': amt,
                })
            chunk = fp.read(BATCH_SIZE * 32)

    df = pd.DataFrame(rows)
    return df

def process_file(args):
    """Process a single file: convert and write to Parquet"""
    filepath, out_dir = args
    code = tdx_to_code(filepath)
    if code is None:
        return None

    out_path = out_dir / f"{code.replace('.', '')}.parquet"

    try:
        df_new = parse_lc5(filepath)
        if df_new is None or df_new.empty:
            return None

        # Merge with existing if any
        if out_path.exists():
            df_old = pd.read_parquet(str(out_path))
            # Keep existing columns
            for col in df_old.columns:
                if col not in df_new.columns:
                    df_new[col] = None
            # Combine, deduplicate by datetime
            df_all = pd.concat([df_old, df_new], ignore_index=True)
            df_all = df_all.drop_duplicates(subset=['datetime'], keep='first')
            df_all = df_all.sort_values('datetime')
        else:
            df_all = df_new.sort_values('datetime')

        df_all.to_parquet(str(out_path), index=False)
        return {
            'code': code,
            'new': len(df_new),
            'total': len(df_all),
        }
    except Exception as e:
        return {'code': code, 'error': str(e)}

def main():
    out_dir = OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # Collect all .lc5 files
    files = []
    for d in [TDX_SH, TDX_SZ]:
        if d.exists():
            files.extend(str(p) for p in d.glob('*.lc5'))

    print(f"找到 {len(files)} 个 .lc5 文件")

    # Process (single process for safety, multiprocessing may lock DuckDB)
    total_new = 0
    total_all = 0
    success = 0
    errors = 0
    t0 = time.time()

    for i, f in enumerate(sorted(files)):
        if i % 500 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            print(f"[{i+1}/{len(files)}] {rate:.0f} files/s, {total_new:,} new records")

        result = process_file((f, out_dir))
        if result:
            if 'error' in result:
                errors += 1
                if errors <= 5:
                    print(f"  ERROR {result['code']}: {result['error']}")
            else:
                success += 1
                total_new += result['new']
                total_all += result['total']

    elapsed = time.time() - t0
    print(f"\n完成! {elapsed:.0f}s")
    print(f"成功: {success} 文件, 失败: {errors} 文件")
    print(f"新增记录: {total_new:,}, 总记录: {total_all:,}")

if __name__ == '__main__':
    main()
