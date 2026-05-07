from database.duckdb_manager import db

def purge_bse():
    print("正在启动沪深精选清理逻辑...")
    try:
        # 1. 从清单删除
        db.conn.execute("DELETE FROM stocks WHERE code LIKE '8%' OR code LIKE '4%' OR code LIKE '9%'")
        # 2. 从分时/日线删除
        db.conn.execute("DELETE FROM daily WHERE code[:1] IN ('8', '4', '9')")
        db.conn.execute("DELETE FROM min5 WHERE code[:1] IN ('8', '4', '9')")
        db.conn.commit()
        count = db.conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        print(f"清理成功！目前库内仅包含沪、深 A 股: {count} 只")
    except Exception as e:
        print(f"清理异常: {e}")

if __name__ == '__main__':
    purge_bse()
