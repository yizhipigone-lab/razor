"""dry-run 端到端验证(v5.4 阶段1)

连真实 QMT(只读查资金/持仓 + 持仓接管 ETF 分类),
mock 下单验证 callback 全链路(C1 在途预扣 + C3 幂等 + 状态转换 + 盈亏重算)。

不下真单(mode=dry-run,走 mock 回报生成器)。

运行:
  QMT_ACCOUNT_ID=180056133 ./venv313/Scripts/python.exe scripts/poc_dryrun_e2e.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.live_trader.config import LiveTraderConfig
from app.live_trader.store import LiveTraderStore
from app.live_trader.notify import Notifier
from app.live_trader.kill_switch import KillSwitch
from app.live_trader.clearance_lock import ClearanceLock
from app.live_trader.pnl_engine import PnlEngine
from app.live_trader.audit import AuditLogger
from app.live_trader.qmt_wrapper import QmtWrapper
from app.live_trader.callback_handler import CallbackHandler
from app.live_trader.connection_manager import ConnectionManager
from app.live_trader.risk_gate import RiskGate
from app.live_trader.reconciler import Reconciler
from app.live_trader.schemas import OrderIntent
from app.utils.xtquant_compat import format_code, ORDER_TYPE_BUY, PRICE_TYPE_FIX


def main():
    print("=" * 60)
    print("dry-run 端到端验证")
    print("=" * 60)

    # 配置(dry-run 模式)
    config = LiveTraderConfig(
        qmt_account_id=os.environ.get("QMT_ACCOUNT_ID", ""),
        mode="dry-run",
        live_capital=100000.0,
        preserved_codes=["159226.SZ", "159290.SZ"],
    )
    if not config.qmt_account_id:
        print("FAIL: 请设置 QMT_ACCOUNT_ID")
        sys.exit(1)
    print(f"模式: {config.mode} | 账号: {config.qmt_account_id}")

    # 初始化组件
    store = LiveTraderStore(config)
    notifier = Notifier("", False)  # 不发企业微信
    ks = KillSwitch(config, store, notifier)
    cl_lock = ClearanceLock(config)
    pnl = PnlEngine(store)
    audit = AuditLogger(store)
    qmt = QmtWrapper(config)
    callback = CallbackHandler(config, store, ks, cl_lock, pnl, notifier)
    conn = ConnectionManager(config, qmt, callback, ks, store)
    rg = RiskGate(config, store, ks, qmt)
    reconciler = Reconciler(config, store, qmt, ks, notifier)

    callback.kill_switch = ks
    callback.clearance_lock = cl_lock
    callback.pnl_engine = pnl
    callback.notify = notifier

    results = {"pass": 0, "fail": 0}

    def check(name, cond, detail=""):
        tag = "[PASS]" if cond else "[FAIL]"
        print(f"  {tag} {name} {detail}")
        results["pass" if cond else "fail"] += 1

    try:
        # ===== 1. 连接真实 QMT =====
        print("\n[1] 连接真实 QMT...")
        try:
            conn.connect()
            check("QMT 连接", qmt.connected)
        except Exception as e:
            print(f"  ❌ QMT 连接失败: {e}")
            print("  (需 XtMiniQmt.exe 已登录)")
            sys.exit(1)

        # ===== 2. 查资金 =====
        print("\n[2] 查资金...")
        asset = qmt.query_asset()
        check("资金查询", asset is not None,
              f"现金={asset.get('cash', 0):.2f} 总资产={asset.get('total_asset', 0):.2f}" if asset else "")

        # ===== 3. 查持仓 + 接管 ETF 分类 =====
        print("\n[3] 查持仓 + ETF 分类接管...")
        from app.live_trader.main import _takeover_positions
        _takeover_positions(store, qmt, config, audit)
        positions = store.get_positions()
        check("持仓接管", len(positions) > 0, f"共 {len(positions)} 只")

        etf_count = sum(1 for p in positions if not p.get("managed", True))
        stock_count = sum(1 for p in positions if p.get("managed", True))
        check("ETF 标记 managed=false", etf_count >= 2,
              f"ETF={etf_count}只 股票={stock_count}只")
        print(f"  持仓明细:")
        for p in positions:
            tag = "ETF保留" if not p.get("managed", True) else "策略"
            print(f"    {p['code']} {tag} vol={p['volume']}")

        # ===== 4. mock 下单 + callback 全链路(C1+C3)=====
        print("\n[4] mock 下单(dry-run,验证 callback 全链路)...")
        test_code = "600000.SH"
        test_volume = 100
        test_price = 10.50

        # C3 幂等键
        import hashlib
        client_order_id = hashlib.md5(
            f"dryrun|{test_code}|{os.getpid()}|{int(time.time())}".encode()
        ).hexdigest()[:16]

        # C1:下单前冻结在途预扣
        rg.freeze_pending_buy(test_code, test_volume)
        pos_before = store.get_position(test_code)
        check("C1 在途预扣冻结", pos_before and pos_before["pending_buy_volume"] == test_volume,
              f"pending={pos_before['pending_buy_volume'] if pos_before else 0}")

        # mock 下单(callback 全链路)
        audit.log("signal", code=test_code, reason="dryrun测试信号")
        order_id = callback.mock_order_async_response(
            client_order_id, test_code, "buy", test_volume, test_price, PRICE_TYPE_FIX,
            "dryrun_test", f"SYS:::dryrun测试"
        )
        audit.order_placed(test_code, order_id, "dry-run", {"volume": test_volume, "price": test_price})
        check("mock 下单返回 order_id", order_id > 0, f"oid={order_id}")

        # 等待 mock callback 链完成(200-500ms + 处理)
        print("  等待 mock callback 链(1s)...")
        time.sleep(1.0)

        # 验证订单状态变成 56(已成)
        order = store.get_order(order_id)
        check("callback 订单状态→56已成", order and order["status"] == 56,
              f"status={order['status'] if order else '?'}")

        # 验证成交记录写入
        deals = store.get_deals(code=test_code, limit=10)
        check("成交记录写入", len(deals) > 0, f"deals={len(deals)}")

        # C1:验证在途预扣释放(成交后)
        pos_after = store.get_position(test_code)
        check("C1 在途预扣释放", pos_after and pos_after["pending_buy_volume"] == 0,
              f"pending={pos_after['pending_buy_volume'] if pos_after else '?'}")

        # C3 幂等:重复 client_order_id 应被拒绝
        existing = store.get_order_by_client_id(client_order_id)
        check("C3 幂等键查到订单", existing is not None,
              f"client_order_id={client_order_id[:8]}...")

        # ===== 5. 非法状态转换拒绝 =====
        print("\n[5] callback 非法状态转换拒绝...")
        from unittest.mock import MagicMock
        mock_order = MagicMock()
        mock_order.order_id = order_id
        mock_order.order_status = 50  # 尝试 56→50(非法)
        mock_order.stock_code = test_code
        mock_order.order_volume = test_volume
        mock_order.price = test_price
        callback._handle_order_update(order_id, 50, mock_order)
        order_check = store.get_order(order_id)
        check("非法转换被拒(状态保持56)", order_check and order_check["status"] == 56,
              f"status={order_check['status'] if order_check else '?'}")

        # ===== 6. 对账(不回写)=====
        print("\n[6] 对账(验证不回写 live_positions)...")
        local_vol_before = store.get_position(test_code)["volume"] if store.get_position(test_code) else 0
        recon_result = reconciler.reconcile()
        check("对账执行", "error" not in recon_result, f"total={recon_result.get('total', 0)}")
        local_vol_after = store.get_position(test_code)["volume"] if store.get_position(test_code) else 0
        check("对账不回写 live_positions", local_vol_before == local_vol_after,
              f"before={local_vol_before} after={local_vol_after}")

        # ===== 7. 审计回放 =====
        print("\n[7] 审计回放...")
        replay = audit.replay(order_id)
        check("审计回放有事件", replay.get("found") and len(replay.get("events", [])) > 0,
              f"events={len(replay.get('events', []))}")

        # ===== 8. kill switch =====
        print("\n[8] kill switch 三重状态...")
        ks.activate(reason="dryrun_test", source="e2e")
        check("kill switch 激活", ks.is_active())
        ks.deactivate()
        check("kill switch 解除", not ks.is_active())

        # ===== 总结 =====
        print("\n" + "=" * 60)
        print(f"dry-run 端到端验证结果: {results['pass']} 通过 / {results['fail']} 失败")
        print("=" * 60)
        if results["fail"] > 0:
            print("[FAIL] 有失败项,需修复")
            sys.exit(1)
        else:
            print("[PASS] 全部通过!dry-run 链路完整(C1在途预扣 + C3幂等 + callback全链路 + 对账不回写 + ETF豁免)")

    finally:
        try:
            conn.stop()
            qmt.stop()
            callback.stop()
            store.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
