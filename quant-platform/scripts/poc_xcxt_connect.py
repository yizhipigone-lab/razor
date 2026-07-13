"""XCXT POC 连接验证脚本(v5.4 阶段0)

只读验证,不下单。验证:
1. xtdata.connect() 基础行情连接
2. XtQuantTrader(path, session) 交易连接
3. subscribe(StockAccount) 账户订阅
4. query_stock_asset 资金查询
5. query_stock_positions 持仓查询
6. query_stock_orders 委托查询
7. callback 注册(不触发真实下单)

账号从环境变量 QMT_ACCOUNT_ID 读,不硬编码。
路径从环境变量 QMT_USERDATA_PATH 读,默认 D:\\Program Files\\XCXT\\userdata_mini。
"""
import os
import sys
import time
import threading

# 配置(从环境变量读,不硬编码账号)
QMT_USERDATA_PATH = os.environ.get("QMT_USERDATA_PATH", r"D:\Program Files\XCXT\userdata_mini")
QMT_ACCOUNT_ID = os.environ.get("QMT_ACCOUNT_ID", "")

if not QMT_ACCOUNT_ID:
    print("FAIL: 请设置环境变量 QMT_ACCOUNT_ID")
    sys.exit(1)

print(f"=" * 60)
print(f"XCXT POC 连接验证")
print(f"=" * 60)
print(f"userdata_mini: {QMT_USERDATA_PATH}")
print(f"account_id:    {QMT_ACCOUNT_ID}")
print(f"=" * 60)

# 回调收集器
callback_events = []
callback_lock = threading.Lock()


def make_callback():
    from xtquant.xttrader import XtQuantTraderCallback

    class PocCallback(XtQuantTraderCallback):
        def on_disconnected(self):
            with callback_lock:
                callback_events.append(("on_disconnected", None))
            print("[CB] on_disconnected")

        def on_account_status(self, status):
            with callback_lock:
                callback_events.append(("on_account_status", status))
            print("[CB] on_account_status")

        def on_stock_order(self, order):
            with callback_lock:
                callback_events.append(("on_stock_order", order))
            print("[CB] on_stock_order")

        def on_stock_trade(self, trade):
            with callback_lock:
                callback_events.append(("on_stock_trade", trade))
            print("[CB] on_stock_trade")

        def on_order_error(self, err):
            with callback_lock:
                callback_events.append(("on_order_error", err))
            print("[CB] on_order_error")

        def on_cancel_error(self, err):
            with callback_lock:
                callback_events.append(("on_cancel_error", err))
            print("[CB] on_cancel_error")

        def on_order_stock_async_response(self, seq, order_id, err_msg):
            with callback_lock:
                callback_events.append(("on_order_stock_async_response", (seq, order_id)))
            print(f"[CB] on_order_stock_async_response seq={seq} order_id={order_id}")

    return PocCallback()


def safe_getattr(obj, field, default=None):
    """getattr 安全取值(防版本差异)"""
    return getattr(obj, field, default)


def main():
    # ===== 1. xtdata 基础连接 =====
    print("\n[1/7] xtdata.connect() 基础行情连接...")
    try:
        from xtquant import xtdata
        rc = xtdata.connect()
        print(f"  xtdata.connect() 返回: {rc} (类型: {type(rc).__name__})")
        # 返回值可能是 None / 0 / 列表,不同版本不同
        print("  xtdata 基础连接: OK")
    except Exception as e:
        print(f"  FAIL: {e}")
        return False

    # ===== 2. XtQuantTrader 交易连接 =====
    print("\n[2/7] XtQuantTrader 交易连接...")
    try:
        from xtquant.xttrader import XtQuantTrader
        from xtquant.xttype import StockAccount

        session_id = int(time.time() * 1000) % 1000000
        print(f"  session_id: {session_id}")

        callback = make_callback()
        # 关键: callback 必须在 start 前注册(v5.1 修订 #3)
        trader = XtQuantTrader(QMT_USERDATA_PATH, session_id, callback)
        print("  XtQuantTrader 实例化: OK")

        trader.register_callback(callback)
        print("  register_callback: OK")

        trader.start()
        print("  start: OK")

        connect_rc = trader.connect()
        print(f"  connect() 返回: {connect_rc} (0=成功)")
        if connect_rc != 0:
            print(f"  FAIL: 连接失败,返回 {connect_rc}")
            last_err = trader.get_last_error()
            print(f"  get_last_error: {last_err}")
            return False
        print("  交易连接: OK")
    except Exception as e:
        import traceback
        print(f"  FAIL: {e}")
        traceback.print_exc()
        return False

    # ===== 3. subscribe 账户订阅 =====
    print("\n[3/7] subscribe(StockAccount)...")
    try:
        account = StockAccount(QMT_ACCOUNT_ID, "STOCK")
        sub_rc = trader.subscribe(account)
        print(f"  subscribe 返回: {sub_rc} (0=成功)")
        if sub_rc != 0:
            print(f"  WARN: subscribe 返回 {sub_rc},尝试继续")
        else:
            print("  账户订阅: OK")
    except Exception as e:
        print(f"  FAIL: {e}")
        return False

    # ===== 4. query_stock_asset 资金查询 =====
    print("\n[4/7] query_stock_asset 资金查询...")
    try:
        asset = trader.query_stock_asset(account)
        if asset is None:
            print("  返回 None(可能账户无数据或未登录)")
        else:
            print(f"  account_id:  {safe_getattr(asset, 'account_id', '?')}")
            print(f"  cash(可用):  {safe_getattr(asset, 'cash', '?')}")
            print(f"  frozen_cash: {safe_getattr(asset, 'frozen_cash', '?')}")
            print(f"  market_value:{safe_getattr(asset, 'market_value', '?')}")
            print(f"  total_asset: {safe_getattr(asset, 'total_asset', '?')}")
            print("  资金查询: OK")
    except Exception as e:
        print(f"  FAIL: {e}")

    # ===== 5. query_stock_positions 持仓查询 =====
    print("\n[5/7] query_stock_positions 持仓查询...")
    try:
        positions = trader.query_stock_positions(account)
        if positions is None:
            print("  返回 None")
        else:
            print(f"  持仓数量: {len(positions)}")
            for i, pos in enumerate(positions[:5]):  # 只显示前5只
                code = safe_getattr(pos, 'stock_code', '?')
                vol = safe_getattr(pos, 'volume', 0)
                can_use = safe_getattr(pos, 'can_use_volume', 0)
                avg = safe_getattr(pos, 'avg_price', 0)
                print(f"  [{i}] {code} 总{vol} 可卖{can_use} 均价{avg}")
            print("  持仓查询: OK")
    except Exception as e:
        print(f"  FAIL: {e}")

    # ===== 6. query_stock_orders 委托查询 =====
    print("\n[6/7] query_stock_orders 委托查询...")
    try:
        orders = trader.query_stock_orders(account, cancelable_only=False)
        if orders is None:
            print("  返回 None")
        else:
            print(f"  委托数量: {len(orders)}")
            for i, o in enumerate(orders[:3]):
                oid = safe_getattr(o, 'order_id', '?')
                code = safe_getattr(o, 'stock_code', '?')
                status = safe_getattr(o, 'order_status', '?')
                print(f"  [{i}] oid={oid} {code} status={status}(类型{type(status).__name__})")
            print("  委托查询: OK")
            # 关键: 验证 order_status 是 int 还是 str(H6 状态码)
            if orders:
                s = safe_getattr(orders[0], 'order_status', None)
                print(f"  >>> order_status 类型: {type(s).__name__} 值: {s}")
                print(f"  >>> 验证: int 类型则确认 48-57+255 状态码方案")
    except Exception as e:
        print(f"  FAIL: {e}")

    # ===== 7. 连接状态检查 =====
    print("\n[7/7] 连接状态检查...")
    try:
        connected = trader.connected
        print(f"  trader.connected: {connected}")
        print("  连接状态: OK" if connected else "  WARN: connected=False")
    except Exception as e:
        print(f"  FAIL: {e}")

    # ===== 等待 3 秒看有无 callback =====
    print("\n等待 3 秒收集 callback 事件...")
    time.sleep(3)
    with callback_lock:
        print(f"  收到 callback 事件数: {len(callback_events)}")
        for evt in callback_events:
            print(f"    - {evt[0]}")

    # ===== 清理 =====
    print("\n清理连接...")
    try:
        trader.stop()
        print("  trader.stop(): OK")
    except Exception as e:
        print(f"  stop 异常: {e}")

    print("\n" + "=" * 60)
    print("POC 连接验证完成")
    print("=" * 60)
    return True


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
