"""
用历史日K线反推每日概念热度，写入 concept_heat 表
优化版：从文件名提取股票代码，一次性计算所有日期涨跌幅
"""
import sys, io
from pathlib import Path
from datetime import date
import duckdb

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
ROOT = Path(__file__).resolve().parent
PARQUET_DIR = ROOT / "data" / "parquet" / "daily"
DB_PATH = ROOT / "data" / "meta" / "meta.db"

START = date(2025, 4, 25)
END = date(2026, 5, 2)

print("连接数据库...")
conn = duckdb.connect(str(DB_PATH))

# 加载概念-股票映射
concept_stocks = conn.execute("SELECT concept_name, stock_code FROM concept_stocks").df()
print(f"概念映射: {len(concept_stocks)} 行, {concept_stocks['concept_name'].nunique()} 概念")

parquet_glob = str(PARQUET_DIR / "*.parquet").replace("\\", "/")

print(f"日期范围: {START} ~ {END}")
print("一次性读取所有日K线并计算每日涨跌幅...")

sql = f"""
WITH all_bars AS (
    SELECT
        regexp_extract(filename, '([^/\\\\]+)\\.parquet$', 1) AS code,
        date, close
    FROM read_parquet('{parquet_glob}', hive_partitioning=false, union_by_name=true, filename=true)
    WHERE date >= '{START.isoformat()}'
),
with_prev AS (
    SELECT code, date, close,
           LAG(close) OVER (PARTITION BY code ORDER BY date) AS prev_close
    FROM all_bars
),
daily_change AS (
    SELECT code, date,
           (close - prev_close) / NULLIF(prev_close, 0) * 100 AS change_pct
    FROM with_prev
    WHERE prev_close IS NOT NULL
      AND date <= '{END.isoformat()}'
      AND ABS(close - prev_close) / prev_close < 0.11
),
concept_agg AS (
    SELECT
        cs.concept_name,
        dc.date,
        AVG(dc.change_pct) AS hotness,
        COUNT(*) AS constituent_count,
        SUM(CASE WHEN dc.change_pct > 0 THEN 1 ELSE 0 END) AS advance_count,
        SUM(CASE WHEN dc.change_pct < 0 THEN 1 ELSE 0 END) AS decline_count
    FROM concept_stocks cs
    JOIN daily_change dc ON cs.stock_code = dc.code
    WHERE dc.change_pct IS NOT NULL
    GROUP BY cs.concept_name, dc.date
    HAVING COUNT(*) >= 3
)
SELECT concept_name, date AS trade_date, hotness, constituent_count,
       advance_count, decline_count, hotness AS avg_change_pct
FROM concept_agg
ORDER BY date, hotness DESC
"""

print("执行查询...")
df = conn.execute(sql).df()
print(f"查询完成: {len(df)} 条记录, {df['trade_date'].nunique()} 天, {df['concept_name'].nunique()} 概念")

if df.empty:
    print("无数据，退出")
    conn.close()
    exit()

# 写入 concept_heat 表
print("清除旧数据并写入 concept_heat...")
conn.execute("DELETE FROM concept_heat WHERE trade_date >= ? AND trade_date <= ?",
             [START.isoformat(), END.isoformat()])

conn.register('_tmp_heat', df)
conn.execute("""
    INSERT INTO concept_heat (concept_name, trade_date, hotness, constituent_count,
                               advance_count, decline_count, avg_change_pct)
    SELECT concept_name, trade_date, ROUND(hotness, 2), constituent_count,
           advance_count, decline_count, ROUND(avg_change_pct, 2)
    FROM _tmp_heat
""")

# 验证
check = conn.execute("""
    SELECT trade_date, COUNT(*) as concepts,
           ROUND(AVG(hotness), 2) as avg_hotness,
           ROUND(MAX(hotness), 2) as max_hotness,
           ROUND(MIN(hotness), 2) as min_hotness
    FROM concept_heat
    WHERE trade_date >= ? AND trade_date <= ?
    GROUP BY trade_date ORDER BY trade_date
""", [START.isoformat(), END.isoformat()]).df()

print(f"\n写入完成! 共 {len(df)} 条记录")
print(f"\n前10天热度概况:")
print(check.head(10).to_string())

# 热度最高 Top-10 概念
top_concepts = conn.execute("""
    SELECT concept_name, ROUND(AVG(hotness), 2) as avg_hotness, COUNT(*) as days
    FROM concept_heat
    WHERE trade_date >= ? AND trade_date <= ?
    GROUP BY concept_name
    ORDER BY avg_hotness DESC
    LIMIT 10
""", [START.isoformat(), END.isoformat()]).df()
print(f"\n热度最高 Top-10 概念:")
print(top_concepts.to_string())

conn.close()
print("\n完成")
