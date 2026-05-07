import os

with open("server.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

def get_block(start_idx, end_idx):
    return "".join(lines[start_idx:end_idx])

# Indices are 0-based.
# Screener: lines 381 to 499
screener_code = f"""from fastapi import APIRouter
from core.logger import get_logger
from database.duckdb_manager import db
import threading
from server.websocket.manager import sync_broadcast
from utils.threading import run_in_thread
import datetime

log = get_logger("API-Screener")
router = APIRouter()

{get_block(381, 499).replace('@app.', '@router.').replace('_run_in_thread', 'run_in_thread')}
"""

with open("app/api/screener.py", "w", encoding="utf-8") as f:
    f.write(screener_code)

# Sentiment: lines 499 to 529
sentiment_code = f"""from fastapi import APIRouter
from core.logger import get_logger
from database.duckdb_manager import db
import threading

log = get_logger("API-Sentiment")
router = APIRouter()

{get_block(499, 529).replace('@app.', '@router.').replace('_run_in_thread', 'run_in_thread')}
"""

with open("app/api/sentiment.py", "w", encoding="utf-8") as f:
    f.write(sentiment_code)

# Factory: lines 529 to 619
factory_code = f"""from fastapi import APIRouter, Header, HTTPException
from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db
from pydantic import BaseModel
import threading
from typing import Optional

log = get_logger("API-Factory")
router = APIRouter()

{get_block(529, 619).replace('@app.', '@router.').replace('_run_in_thread', 'run_in_thread')}
"""

with open("app/api/factory.py", "w", encoding="utf-8") as f:
    f.write(factory_code)

# Remove the blocks from server.py backwards to keep indices stable
new_lines = lines[:381] + lines[619:]

# Inject routers
for idx, line in enumerate(new_lines):
    if "app.include_router(backtest.router)" in line:
        new_lines.insert(idx+1, "app.include_router(screener.router)\napp.include_router(sentiment.router)\napp.include_router(factory.router)\n")
        break

for idx, line in enumerate(new_lines):
    if "from app.api import market, watchlist, data_sync, backtest" in line:
        new_lines[idx] = "from app.api import market, watchlist, data_sync, backtest, screener, sentiment, factory\n"
        break

with open("server.py", "w", encoding="utf-8") as f:
    f.writelines(new_lines)

print("Extraction completed!")
