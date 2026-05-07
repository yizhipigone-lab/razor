import re
import os

with open("server.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

def get_block(start_idx, end_idx):
    return "".join(lines[start_idx:end_idx])

data_sync_code = f"""from fastapi import APIRouter
from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db
import threading
from server.websocket.manager import sync_broadcast
from utils.threading import run_in_thread
import datetime

log = get_logger("API-DataSync")
router = APIRouter()
sync_lock = threading.Lock()
is_syncing = False

{get_block(295, 482)}
{get_block(693, 783)}
{get_block(1117, 1159)}
"""

with open("app/api/data_sync.py", "w", encoding="utf-8") as f:
    f.write(data_sync_code)

backtest_code = f"""from fastapi import APIRouter
from typing import Optional, List
from pydantic import BaseModel
from core.logger import get_logger
from core.settings import settings
from database.duckdb_manager import db
import threading
from server.websocket.manager import sync_broadcast
from utils.threading import run_in_thread

log = get_logger("API-Backtest")
router = APIRouter()
stop_events = {{}}

{get_block(482, 693)}
{get_block(1017, 1117)}
{get_block(1159, 1205)}
"""

with open("app/api/backtest.py", "w", encoding="utf-8") as f:
    f.write(backtest_code)

print("data_sync.py and backtest.py written!")
