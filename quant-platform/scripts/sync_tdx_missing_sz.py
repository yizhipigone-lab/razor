"""
补全 SZ 缺失的 5分钟线 Parquet 文件（续跑脚本）
只处理尚未同步的 SZ .lc5 文件
"""
import struct
import os
import sys
import time
import pandas as pd
from datetime import date, timedelta, datetime
from pathlib import Path

BASE_DATE = date(1899, 12, 30)
TDX_SZ = Path("E:/NEW_TDX/vipdoc/sz/fzline")
OUT_DIR = Path("E:/1target/p9_project/quant-platform/data/parquet/min5")

STRUCT = struct.Struct('HHfffffII')  # 32 bytes per record
BATCH_SIZE = 50000


def parse_lc5(filepath: str):
    size = os.path.getsize(filepath)
    if size < 32:
        return None

    name = os.path.basename(filepath)
    code_num = name[2:8]
    code = f"{code_num}.SZ"

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

    if not rows:
        return None
    return pd.DataFrame(rows), code


def main():
    out_dir = OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # Collect all SZ .lc5 files
    all_files = sorted(TDX_SZ.glob('*.lc5'))
    total = len(all_files)

    # Find missing ones
    missing = []
    for f in all_files:
        code_num = f.stem[2:8]
        code_sz = f"{code_num}SZ"
        # Check both naming conventions
        if not (out_dir / f"{code_sz}.parquet").exists() and not (out_dir / f"{code_num}.parquet").exists():
            missing.append(f)
        # Also check if the existing file has 0 rows (corrupted)
        for name in [f"{code_sz}.parquet", f"{code_num}.parquet"]:
            fp = out_dir / name
            if fp.exists() and fp.stat().st_size < 1000:
                fp.unlink()
                missing.append(f)
                break

    print(f"SZ 总计: {total} 个文件, 需补: {len(missing)} 个")
    if not missing:
        print("暂无缺失文件。")
        return

    t0 = time.time()
    success = 0
    errors = 0
    new_records = 0

    for i, f in enumerate(missing):
        try:
            result = parse_lc5(str(f))
            if result is None:
                errors += 1
                continue

            df_new, code = result
            if df_new is None or df_new.empty:
                errors += 1
                continue

            code_sz = code.replace('.', '')
            out_path = out_dir / f"{code_sz}.parquet"

            # Merge with existing if any (from unsuffixed or partial)
            existing = None
            for alt_name in [f"{code_sz}.parquet", f"{code_num}.parquet"]:
                alt = out_dir / alt_name
                if alt.exists() and alt.stat().st_size >= 1000:
                    existing = alt
                    break

            if existing:
                df_old = pd.read_parquet(str(existing))
                for col in df_old.columns:
                    if col not in df_new.columns:
                        df_new[col] = None
                df_all = pd.concat([df_old, df_new], ignore_index=True)
                df_all = df_all.drop_duplicates(subset=['datetime'], keep='first')
                df_all = df_all.sort_values('datetime')
            else:
                df_all = df_new.sort_values('datetime')

            df_all.to_parquet(str(out_path), index=False)
            success += 1
            new_records += len(df_new)

        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  ERROR {f.name}: {e}")

        # Progress every 100 files
        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{i+1}/{len(missing)}] {rate:.0f} files/s, {new_records:,} new records", flush=True)

    elapsed = time.time() - t0
    print(f"\n完成! {elapsed:.0f}s")
    print(f"成功: {success}, 失败: {errors}")
    print(f"新增记录: {new_records:,}")


if __name__ == '__main__':
    main()
