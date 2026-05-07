import time, threading
from xtquant import xtdata

e = threading.Event()
def cb(data):
    print("Callback:", data)
    if data.get('finished') == data.get('total'):
        e.set()

stocks = ["000001.SZ", "000002.SZ", "600000.SH"]
print("Triggering batch download...")
xtdata.download_history_data2(stocks, "5m", "20260401000000", "20260403235959", callback=cb)
e.wait(10.0)
print("Result:", e.is_set())
