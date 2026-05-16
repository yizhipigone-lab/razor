"""修复 parquet 文件中 close 为 NaN 的行"""
import pandas as pd
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent.parent
DAILY = ROOT / "data" / "parquet" / "daily"
BACKUP = ROOT / "data" / "parquet" / "daily_backup"

files = [f for f in DAILY.glob("*.parquet") if len(f.stem)==6 and f.stem.isdigit()]
print(f"扫描 {len(files)} 个文件...")

fixed = 0; ok = 0
for f in files:
    df = pd.read_parquet(str(f))
    bad = df['close'].isna().sum()
    if bad == 0:
        ok += 1; continue

    # 备份
    BACKUP.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(f), str(BACKUP / f.name))

    # 删除 close 为 NaN 的行
    df = df.dropna(subset=['close'])
    if len(df) == 0:
        print(f"  {f.stem}: ALL NaN, skipped")
        continue

    df.to_parquet(str(f), index=False)
    fixed += 1

print(f"修复: {fixed} 个 | 正常: {ok} 个 | 备份在 {BACKUP}")
