"""
TDX 1分钟线 → Parquet 同步脚本
将通达信 .lc1 文件转换为 Parquet 格式
"""
import struct
import os
import sys
import time
import pandas as pd
from datetime import date, timedelta, datetime
from pathlib import Path

BASE_DATE = date(1899, 12, 30)
TDX_SH = Path("E:/NEW_TDX/vipdoc/sh/minline")
TDX_SZ = Path("E:/NEW_TDX/vipdoc/sz/minline")
OUT_DIR = Path("E:/1target/p9_project/quant-platform/data/parquet/min1")

STRUCT = struct.Struct('HHfffffII')  # 32 bytes per record


def parse_lc1(filepath: str):
    """Parse a single .lc1 file into a DataFrame"""
    size = os.path.getsize(filepath)
    if size < 32:
        return None

    name = os.path.basename(filepath)
    prefix = name[:2]
    num = name[2:8]
    if prefix == 'sh':
        code = f"{num}.SH"
    elif prefix == 'sz':
        code = f"{num}.SZ"
    else:
        return None

    rows = []
    with open(filepath, 'rb') as fp:
        while True:
            chunk = fp.read(50000 * 32)
            if not chunk:
                break
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

    if not rows:
        return None
    return pd.DataFrame(rows), code


def main():
    out_dir = OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # Collect files
    files = []
    for d in [TDX_SH, TDX_SZ]:
        if d.exists():
            files.extend(str(p) for p in d.glob('*.lc1'))

    print(f"共 {len(files)} 个 .lc1 文件")
    t0 = time.time()
    success = 0
    errors = 0
    total_records = 0

    for i, f in enumerate(sorted(files)):
        try:
            result = parse_lc1(f)
            if result is None:
                errors += 1
                continue

            df_new, code = result
            if df_new is None or df_new.empty:
                errors += 1
                continue

            code_clean = code.replace('.', '')
            out_path = out_dir / f"{code_clean}.parquet"

            # Merge with existing if any
            if out_path.exists() and out_path.stat().st_size >= 1000:
                df_old = pd.read_parquet(str(out_path))
                # Normalize datetime to str
                df_new['datetime'] = df_new['datetime'].astype(str)
                df_old['datetime'] = df_old['datetime'].astype(str)
                for col in df_old.columns:
                    if col not in df_new.columns:
                        df_new[col] = None
                for col in df_new.columns:
                    if col not in df_old.columns:
                        df_old[col] = None
                df_all = pd.concat([df_old, df_new], ignore_index=True)
                df_all = df_all.drop_duplicates(subset=['datetime'], keep='last')
                df_all = df_all.sort_values('datetime')
            else:
                df_all = df_new.sort_values('datetime')

            df_all.to_parquet(str(out_path), index=False)
            success += 1
            total_records += len(df_new)

        except Exception as e:
            errors += 1
            if errors <= 10:
                print(f"  ERROR {os.path.basename(f)}: {e}")

        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{i+1}/{len(files)}] {rate:.0f} files/s, {total_records:,} records", flush=True)

    elapsed = time.time() - t0
    print(f"\n完成! {elapsed:.0f}s")
    print(f"成功: {success}, 失败: {errors}")
    print(f"总记录: {total_records:,}")


if __name__ == '__main__':
    main()
