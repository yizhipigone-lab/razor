"""
QMT 全市场 1分钟线完整下载脚本
从 2025-04-25 开始，下载全部 A 股 1 分钟线
支持断点续传 + 完整性检查 + 自动重试
"""
import sys, os, io, time, json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

import pandas as pd
import numpy as np
from xtquant import xtdata

DATA_DIR = ROOT_DIR / "data" / "parquet" / "min1"
DATA_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = "20250425"
END_DATE = datetime.now().strftime("%Y%m%d")
BATCH_SIZE = 80
MAX_RETRIES = 3
QMT_PERIOD = "1m"


def get_all_stocks():
    """获取全市场 A 股列表"""
    sh = xtdata.get_stock_list_in_sector('上证A股')
    sz = xtdata.get_stock_list_in_sector('深证A股')
    all_stocks = sorted(list(set(sh + sz)))
    return all_stocks


def scan_existing():
    """扫描已有数据，返回每只股票的最新日期"""
    existing = {}
    for f in DATA_DIR.glob("*.parquet"):
        try:
            df = pd.read_parquet(f)
            if not df.empty:
                dates = pd.to_datetime(df["datetime"])
                existing[f.stem] = dates.max()
        except:
            pass
    return existing


def download_batch(stocks, start_time, end_time):
    """批量下载一批股票的数据，返回成功/失败列表"""
    import threading
    event = threading.Event()

    def callback(data):
        if data.get('finished') == data.get('total'):
            event.set()

    xtdata.download_history_data2(
        stock_list=stocks,
        period=QMT_PERIOD,
        start_time=start_time,
        end_time=end_time,
        callback=callback
    )
    event.wait(timeout=120.0)
    time.sleep(0.3)

    success, failed = [], []
    try:
        res = xtdata.get_local_data(
            stock_list=stocks,
            period=QMT_PERIOD,
            start_time=start_time,
            end_time=end_time,
            count=-1
        )
        for s in stocks:
            df = res.get(s)
            if df is not None and not df.empty:
                success.append(s)
            else:
                failed.append(s)
    except Exception as e:
        failed = stocks[:]
    return success, failed


def save_stock_data(code, df):
    """保存单只股票数据到 parquet"""
    df = df.reset_index().rename(columns={'index': 'datetime', 'vol': 'volume'})
    df['datetime'] = pd.to_datetime(df['datetime'], format='%Y%m%d%H%M%S', errors='coerce')
    df = df.dropna(subset=['datetime'])
    df = df[(df['datetime'].dt.hour >= 9) & (df['datetime'].dt.hour <= 15)]
    if df.empty:
        return False

    code_only = code.split('.')[0]  # strip SH/SZ suffix
    save_path = DATA_DIR / f"{code_only}.parquet"
    if save_path.exists():
        try:
            old = pd.read_parquet(save_path)
            old['datetime'] = pd.to_datetime(old['datetime'])
            df = pd.concat([old, df]).drop_duplicates(subset=['datetime'])
        except:
            pass

    df.sort_values('datetime').to_parquet(save_path, compression='snappy')
    return True


def main():
    print(f"{'='*60}")
    print(f"QMT 全市场 1分钟线下载")
    print(f"区间: {START_DATE} ~ {END_DATE}")
    print(f"批次大小: {BATCH_SIZE} | 最大重试: {MAX_RETRIES}")
    print(f"{'='*60}\n")

    all_stocks = get_all_stocks()
    total = len(all_stocks)
    print(f"全市场 A 股: {total} 只")

    existing = scan_existing()
    print(f"已有数据: {len(existing)} 只")

    # 筛选需要下载的股票（没有数据 或 数据不是最新的）
    missing = [s for s in all_stocks if s.split('.')[0] not in existing]
    print(f"需要下载: {len(missing)} 只\n")

    if not missing:
        print("所有股票已有数据，无需下载！")
        return

    # 分批下载
    retry_queue = []
    all_success = []
    all_failed = []

    batches = [missing[i:i+BATCH_SIZE] for i in range(0, len(missing), BATCH_SIZE)]
    n_batches = len(batches)

    for bi, batch in enumerate(batches):
        print(f"[{bi+1}/{n_batches}] 下载 {len(batch)} 只...", end=" ", flush=True)

        for retry in range(MAX_RETRIES):
            success, failed = download_batch(batch, START_DATE + "000000", END_DATE + "235959")

            if failed and retry < MAX_RETRIES - 1:
                print(f"重试{retry+1} ({len(failed)}失败)...", end=" ", flush=True)
                batch = failed
                time.sleep(2)
            else:
                break

        # 保存成功的数据
        written = 0
        for s in success:
            try:
                res = xtdata.get_local_data(
                    stock_list=[s], period=QMT_PERIOD,
                    start_time=START_DATE + "000000",
                    end_time=END_DATE + "235959", count=-1
                )
                df = res.get(s)
                if df is not None and not df.empty:
                    if save_stock_data(s, df):
                        written += 1
            except:
                pass

        all_success.extend(success)
        all_failed.extend(failed)
        print(f"✓ {written}写入 {f'✗ {len(failed)}失败' if failed else ''}")

        if (bi + 1) % 10 == 0:
            print(f"  进度: {len(all_success)}/{len(missing)} | 失败: {len(all_failed)}")

    # 最终报告
    print(f"\n{'='*60}")
    print(f"下载完成!")
    print(f"  成功: {len(all_success)} 只")
    print(f"  失败: {len(all_failed)} 只")
    print(f"  总计: {len(existing) + len(all_success)}/{total} 只")

    if all_failed:
        print(f"\n失败列表: {all_failed[:20]}...")

    # 完整性检查
    print(f"\n{'='*60}")
    print(f"完整性检查...")
    final_existing = scan_existing()
    print(f"  现有 min1 数据: {len(final_existing)}/{total} 只")

    # 按日期分布
    date_counts = defaultdict(int)
    for code, dt in final_existing.items():
        date_counts[dt.date()] += 1
    print(f"  最新日期分布 (Top 5):")
    for d, c in sorted(date_counts.items(), reverse=True)[:5]:
        print(f"    {d}: {c} 只")


if __name__ == "__main__":
    main()
