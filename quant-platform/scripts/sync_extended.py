#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
扩展数据同步脚本 — 将日线/5分钟/1分钟数据从当前覆盖范围扩展至 2023-01-01
使用 QMT xtdata 批量下载上交所+深交所所有A股
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.data_manager.sync_min5 import sync_qmt_intraday, get_qmt_intra_status
import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="扩展数据同步到 2023-01-01")
    parser.add_argument("--freq", choices=["daily", "5m", "1m"], default="daily", help="K线频率")
    parser.add_argument("--start", default="2023-01-01", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default="2024-12-31", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--batch", type=int, default=60, help="批量大小")
    parser.add_argument("--status", action="store_true", help="仅检查数据状态")
    args = parser.parse_args()

    if args.status:
        status = get_qmt_intra_status(freq=args.freq, start_date=args.start, end_date=args.end)
        print(status)
    else:
        print(f"开始同步 {args.freq} 数据: {args.start} ~ {args.end}")
        success = sync_qmt_intraday(
            freq=args.freq,
            start_date=args.start,
            end_date=args.end,
            batch_size=args.batch,
        )
        print(f"同步{'成功' if success else '失败/中断'}")
