import pandas as pd
import glob
import os
from pathlib import Path
from tqdm import tqdm

def rps_snapshot():
    data_dir = Path(r"d:\anti\p8\data\parquet\daily")
    files = list(data_dir.glob("*.parquet"))
    
    print(f"🔍 正在扫描全市场 {len(files)} 只股票的历史足迹...")
    
    results = []
    # 只需要最近的 200 天数据来查找 120 日回溯点
    for f in tqdm(files, desc="动量计算中"):
        try:
            # 仅读取 date 和 close 提高效率
            df = pd.read_parquet(f, columns=['date', 'close'])
            if len(df) < 120: continue
            
            # 确保按时间排序
            df = df.sort_values('date')
            
            latest_close = df['close'].iloc[-1]
            latest_date = df['date'].iloc[-1]
            
            # 回溯 120 个交易点
            base_close = df['close'].iloc[-121] # 120天前
            ret = (latest_close / base_close - 1)
            
            results.append({
                'code': f.stem,
                'last_date': latest_date,
                'return_120': ret
            })
        except Exception:
            continue
            
    if not results:
        print("❌ 未能获取到任何有效动量数据。")
        return

    # 汇总计算 RPS
    all_df = pd.DataFrame(results)
    all_df['rps_120'] = all_df['return_120'].rank(pct=True) * 100
    
    # 关联一个简单的名称映射（可选，如果 meta 没锁的话）
    top_50 = all_df.sort_values('rps_120', ascending=False).head(50)
    
    print("\n" + "="*50)
    print(f"🔥 全市场 RPS 120 动量英雄榜 (截止: {top_50['last_date'].max()})")
    print("="*50)
    print(f"{'排名':<4} {'代码':<10} {'120日收益':<10} {'RPS 120':<10}")
    print("-" * 50)
    
    for i, (_, row) in enumerate(top_50.iterrows()):
        print(f"{i+1:<4} {row['code']:<10} {row['return_120']*100:>8.2f}% {row['rps_120']:>10.2f}")
    
    print("="*50)
    print(f"💡 动量领头羊共计 {len(all_df)} 只参与海选。")

if __name__ == "__main__":
    rps_snapshot()
