import threading

def run_in_thread(fn, *args, **kwargs):
    """在后台线程运行阻塞任务"""
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()
