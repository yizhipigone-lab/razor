import duckdb, os
p = 'd:/anti/p8/data/parquet/daily/600000.parquet'
if os.path.exists(p):
    print("--- 600000 DAILY ---")
    print(duckdb.query(f"SELECT date, count(*) FROM read_parquet('{p}') GROUP BY date ORDER BY date DESC LIMIT 10").df())
else:
    print("600000 FILE NOT FOUND")
