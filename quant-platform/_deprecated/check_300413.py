import duckdb
conn = duckdb.connect('data/meta/meta.db')
r = conn.execute("SELECT * FROM stocks WHERE code='300413'").fetchall()
print(f'Query 300413: {r}')
