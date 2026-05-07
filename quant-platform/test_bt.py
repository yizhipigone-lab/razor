import sys
import os
import pandas as pd
from datetime import date
sys.path.insert(0, r"d:\anti\p8")
from app.screener.strategies.ma_fan_out_v1 import generate_signals
from database.duckdb_manager import db

codes = ['002279'] # 久其软件
bars_raw = db.load_all_bars(freq="daily", start=date(2025,1,1), end=date(2026,3,10), codes=codes)
bars = bars_raw.df() if hasattr(bars_raw, 'df') else pd.DataFrame(bars_raw)
bars['date'] = pd.to_datetime(bars['date']).dt.date
print(f"Loaded bars length: {len(bars)}")
print(bars.tail())

res = generate_signals(bars.copy())
print("Signals generated. Checking buy signals...")
if 'buy_signal' in res.columns:
    buys = res[res['buy_signal'] == True]
    print(f"Number of buy signals: {len(buys)}")
    print(buys[['code', 'date', 'close', 'ma5', 'ma10', 'ma20', 'ma30', 'ma60', 'buy_signal']])
else:
    print("No buy_signal column.")

EOF
