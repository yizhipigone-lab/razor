import os

with open("server.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

def filter_blocks(lines):
    new_lines = []
    i = 0
    while i < len(lines):
        # Data Sync Block 1
        if i == 295:
            i = 482
            continue
        # Data Sync Block 2
        if i == 693:
            i = 783
            continue
        # Data Sync Block 3
        if i == 1117:
            i = 1159
            continue
        
        # Backtest Block 1
        if i == 482:
            i = 693
            continue
        # Backtest Block 2
        if i == 1017:
            i = 1117
            continue
        # Backtest Block 3
        if i == 1159:
            i = 1205
            continue
            
        new_lines.append(lines[i])
        i += 1
    return new_lines

cleaned = filter_blocks(lines)

# Now, we also need to add router includes in server.py!
# We find: app.include_router(market.router)
# and append ours!
for idx, line in enumerate(cleaned):
    if "app.include_router(market.router)" in line:
        insert_idx = idx + 1
        cleaned.insert(insert_idx, "app.include_router(data_sync.router)\n")
        cleaned.insert(insert_idx+1, "app.include_router(backtest.router)\n")
        break

# We must also import the modules at the top.
for idx, line in enumerate(cleaned):
    if "from app.api import market, watchlist" in line:
        cleaned[idx] = "from app.api import market, watchlist, data_sync, backtest\n"
        break

# Also, we must change _run_in_thread inside server.py to run_in_thread and import it.
# Actually, if we just removed all _run_in_thread calls maybe we can delete it in server.py.
# But other parts might still use it. Let's just import it? No, server.py still has _run_in_thread definition at line 209 (which is at a different line after extraction). Let's leave it as is in server.py.

with open("server.py", "w", encoding="utf-8") as f:
    f.writelines(cleaned)
    
print("server.py cleaned!")
