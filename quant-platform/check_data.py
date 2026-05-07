import duckdb
import os

def check(p, t):
    if not os.path.exists(p):
        print(f"{t}: FILE NOT FOUND: {p}")
        return
    try:
        if t == "MIN5":
            sql = f"SELECT CAST(datetime AS DATE) as d, count(*) as cnt FROM read_parquet('{p}') GROUP BY d ORDER BY d DESC LIMIT 5"
        else:
            sql = f"SELECT CAST(date AS DATE) as d, count(*) as cnt FROM read_parquet('{p}') GROUP BY d ORDER BY d DESC LIMIT 5"
        print(f"--- {t} ---")
        print(duckdb.query(sql).df())
    except Exception as e:
        print(f"{t}: ERROR: {e}")

check('d:/anti/p8/data/parquet/min5/000001.parquet', "MIN5")
check('d:/anti/p8/data/parquet/daily/000001.parquet', "DAILY")
