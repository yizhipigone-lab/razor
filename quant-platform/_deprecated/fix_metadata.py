from database.duckdb_manager import db
import time

def fix_all_codes():
    print("正在扫描并修正数据库内股票代码...")
    try:
        # 1. 股票代码去皮
        db.conn.execute("UPDATE stocks SET code = REPLACE(REPLACE(REPLACE(code, 'sh', ''), 'sz', ''), 'bj', '')")
        # 2. 检查结果
        res = db.conn.execute("SELECT code FROM stocks LIMIT 5").fetchall()
        print(f"修正后的代码样例: {res}")
        db.conn.commit()
        print("数据库代码列修正成功！")
    except Exception as e:
        print(f"修正过程中出现异常: {e}")

if __name__ == "__main__":
    fix_all_codes()
