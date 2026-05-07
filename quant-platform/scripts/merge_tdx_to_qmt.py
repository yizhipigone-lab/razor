"""
将 TDX 历史 5分钟数据合并到 QMT 命名规范（无后缀）的 Parquet 文件中
解决 TDX 数据(2013-2024) 与 QMT 数据(2025-2026) 的命名不统一问题
"""
import time
import pandas as pd
from pathlib import Path

OUT_DIR = Path("E:/1target/p9_project/quant-platform/data/parquet/min5")


def merge_one(tdx_path: Path, qmt_path: Path, code: str):
    """Merge TDX file into QMT-named file"""
    df_tdx = pd.read_parquet(str(tdx_path))
    if df_tdx is None or df_tdx.empty:
        return 0

    if qmt_path.exists() and qmt_path.stat().st_size >= 1000:
        df_qmt = pd.read_parquet(str(qmt_path))
        # Normalize datetime to string (TDX uses str, QMT uses Timestamp)
        df_tdx['datetime'] = df_tdx['datetime'].astype(str)
        df_qmt['datetime'] = df_qmt['datetime'].astype(str)
        # Align columns: TDX has fewer columns, add missing ones as None
        for col in df_qmt.columns:
            if col not in df_tdx.columns:
                df_tdx[col] = None
        for col in df_tdx.columns:
            if col not in df_qmt.columns:
                df_qmt[col] = None
        df_all = pd.concat([df_tdx, df_qmt], ignore_index=True)
        df_all = df_all.drop_duplicates(subset=['datetime'], keep='last')
        df_all = df_all.sort_values('datetime')
        new_rows = len(df_tdx)
    else:
        df_all = df_tdx.sort_values('datetime')
        new_rows = len(df_tdx)

    df_all.to_parquet(str(qmt_path), index=False)
    return new_rows


def main():
    out_dir = OUT_DIR

    # Collect all TDX-suffixed files
    tdx_files = sorted(list(out_dir.glob('*SH.parquet')) + list(out_dir.glob('*SZ.parquet')))
    total = len(tdx_files)
    print(f"TDX 文件总数: {total}")

    # Classify work
    merge_list = []
    copy_list = []
    skip_list = []
    for tdx_f in tdx_files:
        # Extract code from filename: "600000SH" -> "600000", "000001SZ" -> "000001"
        code = tdx_f.stem[:-2]
        qmt_f = out_dir / f"{code}.parquet"

        if qmt_f.exists() and qmt_f.stat().st_size >= 1000:
            merge_list.append((tdx_f, qmt_f, code))
        elif not qmt_f.exists():
            copy_list.append((tdx_f, qmt_f, code))
        else:
            skip_list.append((tdx_f, qmt_f, code))

    print(f"需合并: {len(merge_list)}")
    print(f"需拷贝: {len(copy_list)}")
    print(f"跳过: {len(skip_list)}")

    # Process merges
    t0 = time.time()
    total_new = 0
    success = 0
    errors = 0

    all_work = merge_list + copy_list
    for i, (tdx_f, qmt_f, code) in enumerate(all_work):
        try:
            n = merge_one(tdx_f, qmt_f, code)
            total_new += n
            success += 1
        except Exception as e:
            errors += 1
            if errors <= 10:
                print(f"  ERROR {code}: {e}")

        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            print(f"  [{i+1}/{len(all_work)}] {rate:.0f} files/s, {total_new:,} TDX rows merged", flush=True)

    elapsed = time.time() - t0
    print(f"\n完成! {elapsed:.0f}s")
    print(f"成功: {success}, 失败: {errors}")
    print(f"TDX 历史行数已合并: {total_new:,}")
    print(f"\n最终 min5 目录文件数: {len(list(out_dir.glob('*.parquet')))}")


if __name__ == '__main__':
    main()
