import duckdb
conn = duckdb.connect('data/meta/meta.db')
r = conn.execute("SELECT sector FROM stocks WHERE code='300413'").fetchone()
print(f'Sector: {r[0]}') # 预期结果是行业名称或者空字符串
