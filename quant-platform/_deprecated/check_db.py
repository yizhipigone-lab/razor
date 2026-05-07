import duckdb
conn = duckdb.connect('data/meta/meta.db')
r = conn.execute("SELECT count(*) FROM stocks WHERE sector='' or sector is NULL").fetchone()
print(f'Empty Sector Count: {r[0]}')
r = conn.execute("SELECT count(*) FROM stocks").fetchone()
print(f'Total Count: {r[0]}')
# Select 5 examples
r = conn.execute("SELECT code, name, exchange FROM stocks WHERE sector='' or sector is NULL LIMIT 5").fetchall()
print(f'Examples: {r}')
