#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
通达信 .day 文件 → Parquet 转换器
将 通达信 vipdoc/{sh,sz}/lday/*.day 转为与 QMT 一致的 parquet 格式
用于：交叉验证、补充QMT缺失数据
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import struct
import pandas as pd
from datetime import datetime, date
from pathlib import Path
from tqdm import tqdm  # type: ignore

TDX_VIPDOC = Path("E:/NEW_TDX/vipdoc")
OUT_DIR = Path(__file__).parent.parent / "data" / "parquet" / "daily_tdx"
OUT_DIR.mkdir(parents=True, exist_ok=True)

def parse_tdx_day_file(filepath: Path) -> list[dict]:
    """
    通达信日线 .day 文件格式 (每条记录 32 字节):
      int date;       // YYYYMMDD
      int open;       // open * 100
      int high;       // high * 100
      int low;        // low * 100
      int close;      // close * 100
      float amount;   // 成交额(元)
      int volume;     // 成交量(股)
      int reserved;
    """
    with open(filepath, 'rb') as f:
        data = f.read()

    records = []
    for i in range(0, len(data), 32):
        chunk = data[i:i+32]
        if len(chunk) < 32:
            break
        date_int, open_i, high_i, low_i, close_i, amount, vol, reserved = \
            struct.unpack('IiiiiIii', chunk)

        if date_int < 19900101 or date_int > 20991231:
            continue

        yr = date_int // 10000
        mo = (date_int % 10000) // 100
        dy = date_int % 100
        try:
            dt = date(yr, mo, dy)
        except ValueError:
            continue

        records.append({
            'date': dt,
            'open': open_i / 100.0,
            'high': high_i / 100.0,
            'low': low_i / 100.0,
            'close': close_i / 100.0,
            'volume': vol,
            'amount': amount,
        })

    return records


def convert_all():
    """将通达信日线全部转换为 parquet"""
    total_converted = 0
    total_skipped = 0

    for market in ['sh', 'sz']:
        lday = TDX_VIPDOC / market / 'lday'
        if not lday.exists():
            print(f"  {lday} 不存在，跳过")
            continue

        files = sorted(lday.glob('*.day'))
        print(f"\n{'='*50}")
        print(f"  {market} 市场: {len(files)} 个文件")
        print(f"{'='*50}")

        for fp in tqdm(files, desc=f"转换 {market}"):
            # 提取股票代码: sh600519.day → 600519
            code = fp.stem
            if market == 'sh' and code.startswith('sh'):
                code = code[2:]
            elif market == 'sz' and code.startswith('sz'):
                code = code[2:]

            try:
                records = parse_tdx_day_file(fp)
                if not records:
                    total_skipped += 1
                    continue

                df = pd.DataFrame(records)
                df = df.sort_values('date')

                # 与已有 QMT 数据合并（如果存在）
                qmt_path = Path(__file__).parent.parent / "data" / "parquet" / "daily" / f"{code}.parquet"
                if qmt_path.exists():
                    old = pd.read_parquet(str(qmt_path))
                    if 'date' in old.columns:
                        old['date'] = pd.to_datetime(old['date']).dt.date
                    # 只添加QMT中没有的日期
                    existing_dates = set(old['date'].tolist())
                    new_records = df[~df['date'].isin(existing_dates)]
                    if not new_records.empty:
                        merged = pd.concat([old, new_records], ignore_index=True)
                        merged = merged.sort_values('date')
                        merged.to_parquet(str(qmt_path), index=False)
                else:
                    # 直接存到 tdx 独立目录
                    out_path = OUT_DIR / f"{code}.parquet"
                    df.to_parquet(str(out_path), index=False)

                total_converted += 1

            except Exception as e:
                print(f"\n  [ERROR] {fp.name}: {e}")
                total_skipped += 1

    print(f"\n  转换完成: {total_converted} 成功, {total_skipped} 跳过")
    print(f"  TDX独立数据: {OUT_DIR}")


if __name__ == "__main__":
    convert_all()
