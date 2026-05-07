import os
import tushare as ts

ts.set_token("5051cf6cf52ca062ca348ab11c615ecb6b7909085d33cc11bc6f7ece")
pro = ts.pro_api()

try:
    print("Testing pro.stock_basic...")
    df = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,area,industry,list_date')
    print("Success, rows:", len(df))
    print(df.head())
except Exception as e:
    print("stock_basic error:", e)

try:
    print("Testing ts.pro_bar (qfq)...")
    df = ts.pro_bar(ts_code='000001.SZ', adj='qfq', start_date='20260320', end_date='20260327')
    print("Success, rows:", len(df))
    print(df.head())
except Exception as e:
    print("pro_bar error:", e)

try:
    print("Testing pro.daily...")
    df = pro.daily(ts_code='000001.SZ', start_date='20260320', end_date='20260327')
    print("Success, rows:", len(df))
    print(df.head())
except Exception as e:
    print("daily error:", e)
