import os

def fix_server():
    path = r'D:\targetone\p9\server.py'
    if not os.path.exists(path):
        print("File not found")
        return

    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    start = -1
    end = -1
    for i, line in enumerate(lines):
        if 'async def get_redis_status' in line:
            start = i
        if 'async def get_stocks' in line and start != -1:
            end = i
            break

    if start == -1 or end == -1:
        print(f"Could not find markers: start={start}, end={end}")
        return

    new_middle = [
        '    """查询 Redis 连接状态与当前缓存的持仓最高价数量"""\n',
        '    try:\n',
        '        from core.redis_manager import redis_manager\n',
        '        client = redis_manager.get_client()\n',
        '        if not client:\n',
        '            return {"status": "disconnected", "message": "Redis 未连接"}\n',
        '        client.ping()\n',
        '        keys = client.keys("pos:highest:*")\n',
        '        cached = []\n',
        '        for key in keys:\n',
        '            data = client.hgetall(key)\n',
        '            cached.append({\n',
        '                "pos_id": key.decode().split(":")[-1] if isinstance(key, bytes) else key.split(":")[-1],\n',
        '                "code": data.get(b"code", b"?").decode() if b"code" in data else data.get("code", "?"),\n',
        '                "highest_price": data.get(b"price", b"?").decode() if b"price" in data else data.get("price", "?"),\n',
        '                "update_time": data.get(b"update_time", b"?").decode() if b"update_time" in data else data.get("update_time", "?"),\n',
        '            })\n',
        '        return {"status": "connected", "cached_positions": len(cached), "positions": cached}\n',
        '    except Exception as e:\n',
        '        return {"status": "error", "message": str(e)}\n',
        '\n',
        '# ─── 股票搜索 API ─────────────────────────────────────────────\n',
        '@app.get("/api/stock/search")\n',
        'async def api_stock_search(q: str = ""):\n',
        '    """提供模糊搜索与简易拼音匹配"""\n',
        '    q = q.strip()\n',
        '    if not q: return []\n',
        '    ALIASES = {"BYD": "002594.SZ", "GZMT": "600519.SH", "GZJT": "601238.SH", "HS": "000001.SH", "SZ": "399001.SZ", "CY": "399006.SZ"}\n',
        '    INDEX_LIST = [\n',
        '        {"code": "000001.SH", "name": "上证指数", "exchange": "SH", "sector": "指数板块"},\n',
        '        {"code": "399001.SZ", "name": "深证成指", "exchange": "SZ", "sector": "指数板块"},\n',
        '        {"code": "399006.SZ", "name": "创业板指", "exchange": "SZ", "sector": "指数板块"},\n',
        '        {"code": "000300.SH", "name": "沪深300",  "exchange": "SH", "sector": "指数板块"},\n',
        '        {"code": "000905.SH", "name": "中证500",  "exchange": "SH", "sector": "指数板块"},\n',
        '    ]\n',
        '    q_upper = q.upper(); results = []\n',
        '    P_HEADERS = {"G":"广国工公谷","Z":"中中国招浙证","Y":"豫元宜银永","H":"华海恒航红","D":"大大电东鼎","S":"深申苏盛上","B":"比北宝包","X":"信新小新兴","A":"安奥埃","T":"通天泰铁","L":"联龙利","K":"科凯康","J":"金京精建","Q":"汽奇全七","C":"长重创成","P":"平普"}\n',
        '    def match_pinyin(name, query):\n',
        '        if not query or not query.isalpha(): return False\n',
        '        q0 = query[0].upper(); n0 = name[0]\n',
        '        return q0 in P_HEADERS and n0 in P_HEADERS[q0]\n',
        '\n',
        '    if q_upper in ALIASES:\n',
        '        c = ALIASES[q_upper]; im = [i for i in INDEX_LIST if i["code"]==c]\n',
        '        if im: results.append(im[0])\n',
        '    for idx in INDEX_LIST:\n',
        '        if idx not in results and (q_upper in idx["code"].upper() or q in idx["name"]): results.append(idx)\n',
        '    try:\n',
        '        wild = f"%{q}%"\n',
        '        df = db.conn.execute("SELECT code, name, exchange, sector FROM stocks WHERE status=\'active\' AND (code LIKE ? OR name LIKE ?) LIMIT 60", [wild, wild]).df()\n',
        '        for _, row in df.iterrows():\n',
        '            nm = str(row["name"]); co = str(row["code"]).strip()\n',
        '            if "." not in co: co = f"{co}.SH" if co.startswith("6") else f"{co}.SZ"\n',
        '            if not any(r["code"] == co for r in results):\n',
        '                if not q.isalpha() or (match_pinyin(nm, q) or q_upper in co or q in nm):\n',
        '                    results.append({"code": co, "name": nm, "exchange": str(row.get("exchange", "")), "sector": str(row.get("sector", "-"))})\n',
        '    except: pass\n',
        '    return results[:25]\n',
        '\n',
        '# ─── 自选股 (Watchlist)  ─────────────────────────────────────────────\n',
        '@app.get("/api/watchlist")\n',
        'async def api_get_watchlist(limit: int = 50, offset: int = 0):\n',
        '    df = db.get_watchlist(limit=limit, offset=offset)\n',
        '    if df.empty: return []\n',
        '    if "added_at" in df.columns: df["added_at"] = df["added_at"].astype(str)\n',
        '    if "name" not in df.columns or df["name"].isnull().all():\n',
        '        df["name"] = df["code"].apply(db.get_stock_name_by_code)\n',
        '    return df.to_dict(orient="records")\n',
        '\n',
        '@app.post("/api/watchlist")\n',
        'async def api_add_watchlist(body: dict):\n',
        '    code = body.get("code", "").strip(); name = body.get("name", "").strip()\n',
        '    if not code: return {"status": "error", "message": "代码为空"}\n',
        '    if not name: name = db.get_stock_name_by_code(code)\n',
        '    db.add_to_watchlist(code, name=name, source=body.get("source", "manual"))\n',
        '    return {"status": "ok", "message": f"加入自选股: {name or code}"}\n',
        '\n',
        '@app.delete("/api/watchlist/{code}")\n',
        'async def api_remove_watchlist(code: str):\n',
        '    db.remove_from_watchlist(code)\n',
        '    return {"status": "ok", "message": f"移出自选股: {code}"}\n',
        '\n'
    ]

    final_lines = lines[:start+1] + new_middle + lines[end:]
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(final_lines)
    print("Successfully fixed server.py")

if __name__ == "__main__":
    fix_server()
