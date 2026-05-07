import time, threading
from xtquant import xtdata

e = threading.Event()
def cb(data):
    print("Callback fired:", data)
    e.set()

print("Triggering download...")
res = xtdata.download_history_data2(["000001.SZ"], "5m", "20260401000000", "20260403235959", callback=cb)
print("Return val:", res)
e.wait(5.0)
if e.is_set():
    print("FINISHED EVENT!")
else:
    print("TIMEOUT EVENT!")
