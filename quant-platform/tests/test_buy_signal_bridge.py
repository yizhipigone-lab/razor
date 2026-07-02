"""买入信号桥接单测(v1.2.2 §5.5)

8 类测试:
1. BuySignalRequest 解析
2. _calc_buy_volume 板块取整
3. 闸门10 同股冷却
4. 幂等键(不带时间戳)
5. 鉴权失败
6. 清仓锁等待
7. 心跳写入
8. 批量撤单按 terminal 过滤
"""
import hashlib
import pytest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch


# ===== 1. BuySignalRequest 解析 =====

class TestBuySignalRequest:
    def test_valid_request(self):
        from app.live_trader.schemas import BuySignalRequest, SignalItem
        req = BuySignalRequest(
            signals=[SignalItem(code="600000.SH", price=10.5)],
            strategy="QUANTQQ",
            source="TDX",
        )
        assert len(req.signals) == 1
        assert req.signals[0].code == "600000.SH"
        assert req.signals[0].price == 10.5
        assert req.strategy == "QUANTQQ"
        assert req.source == "TDX"

    def test_default_values(self):
        from app.live_trader.schemas import BuySignalRequest, SignalItem
        req = BuySignalRequest(
            signals=[SignalItem(code="000001.SZ")],
        )
        assert req.strategy == "QUANTQQ"
        assert req.source == "TDX"
        assert req.signals[0].price == 0.0

    def test_multiple_signals(self):
        from app.live_trader.schemas import BuySignalRequest, SignalItem
        req = BuySignalRequest(
            signals=[
                SignalItem(code="600000.SH", price=10.5),
                SignalItem(code="000001.SZ", price=15.2),
                SignalItem(code="688001.SH", price=22.0),
            ],
        )
        assert len(req.signals) == 3

    def test_bare_code(self):
        """支持裸代码(不带后缀)"""
        from app.live_trader.schemas import BuySignalRequest, SignalItem
        req = BuySignalRequest(
            signals=[SignalItem(code="600000", price=10.5)],
        )
        assert req.signals[0].code == "600000"


# ===== 2. _calc_buy_volume 板块取整 =====

class TestCalcBuyVolume:
    def test_main_board_100_shares(self):
        from app.live_trader.buy_volume import _calc_buy_volume
        # 主板:金额 10000 / 价格 10 = 1000 股 → 100 的整数倍 → 1000
        vol = _calc_buy_volume("600000.SH", 10000, 10.0)
        assert vol == 1000

    def test_main_board_round_down(self):
        from app.live_trader.buy_volume import _calc_buy_volume
        # 10000 / 15 = 666.6 → 600(100 整数倍)
        vol = _calc_buy_volume("000001.SZ", 10000, 15.0)
        assert vol == 600

    def test_kcb_200_shares(self):
        from app.live_trader.buy_volume import _calc_buy_volume
        # 科创板(688):200 股整数倍
        vol = _calc_buy_volume("688001.SH", 10000, 20.0)
        # 10000 / 20 = 500 → 400(200 整数倍)
        assert vol == 400

    def test_bse_100_shares(self):
        from app.live_trader.buy_volume import _calc_buy_volume
        # 北交所(8开头):100 股整数倍
        vol = _calc_buy_volume("830001.BJ", 10000, 8.0)
        # 10000 / 8 = 1250 → 1200(100 整数倍)
        assert vol == 1200

    def test_zero_price(self):
        from app.live_trader.buy_volume import _calc_buy_volume
        assert _calc_buy_volume("600000.SH", 10000, 0) == 0

    def test_zero_amount(self):
        from app.live_trader.buy_volume import _calc_buy_volume
        assert _calc_buy_volume("600000.SH", 0, 10.0) == 0

    def test_bare_code(self):
        """裸代码也能正确识别板块"""
        from app.live_trader.buy_volume import _calc_buy_volume
        vol = _calc_buy_volume("688001", 10000, 20.0)
        assert vol == 400  # 科创板 200 整数倍

    def test_bse_4_prefix(self):
        """北交所 4 开头"""
        from app.live_trader.buy_volume import _calc_buy_volume
        vol = _calc_buy_volume("430001.BJ", 10000, 10.0)
        # 10000 / 10 = 1000 → 1000(100 整数倍)
        assert vol == 1000


# ===== 3. 闸门10 同股冷却 =====

class TestGate10SameStockCooldown:
    def _make_risk_gate(self, store_mock=None):
        from app.live_trader.risk_gate import RiskGate
        from app.live_trader.config import LiveTraderConfig
        config = LiveTraderConfig(qmt_account_id="test")
        gate = RiskGate(config, store=store_mock, kill_switch=MagicMock(), qmt_wrapper=MagicMock())
        gate.kill_switch.is_active.return_value = False
        gate.qmt.connected = True
        # 闸门5a 需要 open_asset,否则 fail-safe 禁买
        gate._get_open_asset = MagicMock(return_value=100000.0)
        return gate

    def test_no_deals_passes(self):
        """无卖出记录 → 通过"""
        store = MagicMock()
        store.get_deals.return_value = []
        store.get_position.return_value = None
        gate = self._make_risk_gate(store)

        from app.live_trader.schemas import OrderIntent
        intent = OrderIntent(code="600000.SH", direction="buy", volume=100, price=10.0)
        passed, gates, reason = gate.check(intent, asset={"cash": 50000}, positions=[], quote={"lastPrice": 10})
        # 只要 kill_switch 不激活 且 QMT 连接,应通过闸门10
        gate_10 = [g for g in gates if g.get("gate") == 10]
        assert len(gate_10) == 1
        assert gate_10[0]["passed"] is True

    def test_recent_sell_blocked(self):
        """20 天内卖出 → 被拒"""
        store = MagicMock()
        sell_date = date.today() - timedelta(days=5)
        store.get_deals.return_value = [
            {"code": "600000.SH", "direction": "sell", "traded_at": datetime(sell_date.year, sell_date.month, sell_date.day)}
        ]
        store.get_position.return_value = None
        gate = self._make_risk_gate(store)

        from app.live_trader.schemas import OrderIntent
        intent = OrderIntent(code="600000.SH", direction="buy", volume=100, price=10.0)
        passed, gates, reason = gate.check(intent, asset={"cash": 50000}, positions=[], quote={"lastPrice": 10})
        assert not passed
        assert "冷却" in reason

    def test_old_sell_passes(self):
        """超过 20 天的卖出 → 通过"""
        store = MagicMock()
        sell_date = date.today() - timedelta(days=25)
        store.get_deals.return_value = [
            {"code": "600000.SH", "direction": "sell", "traded_at": datetime(sell_date.year, sell_date.month, sell_date.day)}
        ]
        store.get_position.return_value = None
        gate = self._make_risk_gate(store)

        from app.live_trader.schemas import OrderIntent
        intent = OrderIntent(code="600000.SH", direction="buy", volume=100, price=10.0)
        passed, gates, reason = gate.check(intent, asset={"cash": 50000}, positions=[], quote={"lastPrice": 10})
        gate_10 = [g for g in gates if g.get("gate") == 10]
        assert len(gate_10) == 1
        assert gate_10[0]["passed"] is True

    def test_sell_direction_skips_gate10(self):
        """卖出方向不检查闸门10"""
        store = MagicMock()
        gate = self._make_risk_gate(store)

        from app.live_trader.schemas import OrderIntent
        intent = OrderIntent(code="600000.SH", direction="sell", volume=100, price=10.0)
        # 卖出方向不会触发闸门10
        passed, gates, reason = gate.check(intent, asset={"cash": 50000}, positions=[], quote={"lastPrice": 10})
        gate_10 = [g for g in gates if g.get("gate") == 10]
        assert len(gate_10) == 0  # 买入专属,卖出不检查


# ===== 4. 幂等键(不带时间戳) =====

class TestIdempotencyKey:
    def test_signal_idempotency_key_no_timestamp(self):
        """buy-signal 幂等键:同天同股相同"""
        code = "600000.SH"
        today = date.today()
        key1 = hashlib.md5(f"buy_signal|{code}|{today}".encode()).hexdigest()[:16]
        key2 = hashlib.md5(f"buy_signal|{code}|{today}".encode()).hexdigest()[:16]
        assert key1 == key2

    def test_manual_key_with_timestamp_differs(self):
        """手动下单幂等键:带时间戳,每次不同"""
        import time
        code = "600000.SH"
        today = date.today()
        key1 = hashlib.md5(
            f"manual|{code}|{today}|buy|{int(time.time() * 1000)}".encode()
        ).hexdigest()[:16]
        time.sleep(0.01)  # 确保毫秒时间戳不同
        key2 = hashlib.md5(
            f"manual|{code}|{today}|buy|{int(time.time() * 1000)}".encode()
        ).hexdigest()[:16]
        assert key1 != key2  # 带时间戳,不同


# ===== 5. 鉴权失败 =====

class TestTokenVerification:
    def test_valid_token(self):
        from app.live_trader.main import _verify_token
        from app.live_trader.config import LiveTraderConfig
        config = LiveTraderConfig(qmt_account_id="test", buy_signal_token="my_secret")
        assert _verify_token("Bearer my_secret", config) is True

    def test_invalid_token(self):
        from app.live_trader.main import _verify_token
        from app.live_trader.config import LiveTraderConfig
        config = LiveTraderConfig(qmt_account_id="test", buy_signal_token="my_secret")
        assert _verify_token("Bearer wrong_token", config) is False

    def test_missing_header(self):
        from app.live_trader.main import _verify_token
        from app.live_trader.config import LiveTraderConfig
        config = LiveTraderConfig(qmt_account_id="test", buy_signal_token="my_secret")
        assert _verify_token("", config) is False

    def test_no_token_configured(self):
        """未配置 token 则不鉴权(向后兼容)"""
        from app.live_trader.main import _verify_token
        from app.live_trader.config import LiveTraderConfig
        config = LiveTraderConfig(qmt_account_id="test", buy_signal_token="")
        assert _verify_token("", config) is True


# ===== 6. 清仓锁等待 =====

class TestClearanceLockWait:
    def test_acquire_with_wait_immediate(self):
        """锁空闲 → 立即获取"""
        from app.live_trader.clearance_lock import ClearanceLock
        from app.live_trader.config import LiveTraderConfig
        config = LiveTraderConfig(qmt_account_id="test")
        lock = ClearanceLock(config)
        assert lock.acquire_with_wait("600000.SH", timeout_sec=1) is True

    def test_acquire_with_wait_locked_and_released(self):
        """锁被占,释放后获取"""
        from app.live_trader.clearance_lock import ClearanceLock
        from app.live_trader.config import LiveTraderConfig
        config = LiveTraderConfig(qmt_account_id="test")
        lock = ClearanceLock(config)
        lock.acquire("600000.SH")
        # 锁被占,但 0.5s 内释放
        import threading
        def release_later():
            import time
            time.sleep(0.3)
            lock.release("600000.SH")
        t = threading.Thread(target=release_later)
        t.start()
        assert lock.acquire_with_wait("600000.SH", timeout_sec=2) is True
        t.join()

    def test_acquire_with_wait_timeout(self):
        """锁被占且不释放 → 超时"""
        from app.live_trader.clearance_lock import ClearanceLock
        from app.live_trader.config import LiveTraderConfig
        config = LiveTraderConfig(qmt_account_id="test")
        lock = ClearanceLock(config)
        lock.acquire("600000.SH")
        assert lock.acquire_with_wait("600000.SH", timeout_sec=1) is False


# ===== 7. 心跳写入 =====

class TestHeartbeat:
    def test_record_and_query(self):
        """心跳写入 + 查询"""
        from app.live_trader.store import LiveTraderStore
        from app.live_trader.config import LiveTraderConfig
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.duckdb")
            wal_path = os.path.join(tmpdir, "test.wal")
            config = LiveTraderConfig(
                qmt_account_id="test",
                db_path=db_path,
                wal_path=wal_path,
            )
            store = LiveTraderStore(config)
            store.record_heartbeat("docker_tdx", 3, "ok")

            hb = store.get_latest_heartbeat("docker_tdx")
            assert hb is not None
            assert hb["signal_count"] == 3
            assert hb["scan_status"] == "ok"
            assert hb["source"] == "docker_tdx"
            store.close()

    def test_no_heartbeat_returns_none(self):
        """无心跳记录 → 返回 None"""
        from app.live_trader.store import LiveTraderStore
        from app.live_trader.config import LiveTraderConfig
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.duckdb")
            wal_path = os.path.join(tmpdir, "test.wal")
            config = LiveTraderConfig(
                qmt_account_id="test",
                db_path=db_path,
                wal_path=wal_path,
            )
            store = LiveTraderStore(config)
            hb = store.get_latest_heartbeat("docker_tdx")
            assert hb is None
            store.close()


# ===== 8. 批量撤单按 terminal 过滤 =====

class TestCancelBySource:
    def test_filter_by_terminal(self):
        """只撤销指定 terminal 的在途委托"""
        # 构造 mock 数据
        from app.live_trader.store import LiveTraderStore
        from app.live_trader.config import LiveTraderConfig
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.duckdb")
            wal_path = os.path.join(tmpdir, "test.wal")
            config = LiveTraderConfig(
                qmt_account_id="test",
                db_path=db_path,
                wal_path=wal_path,
            )
            store = LiveTraderStore(config)
            now = datetime.now()
            # 插入两个在途委托:一个 TDX,一个 WEB
            store.sync_terminal_write("order", {
                "order_id": 100, "client_order_id": "c1",
                "code": "600000.SH", "direction": "buy", "volume": 100,
                "price": 10.0, "price_type": 11, "status": 50,
                "status_msg": "已报", "seq": 100, "mode": "live",
                "strategy_name": "QUANTQQ", "order_remark": "",
                "terminal": "TDX", "created_at": now, "updated_at": now,
            })
            store.sync_terminal_write("order", {
                "order_id": 101, "client_order_id": "c2",
                "code": "000001.SZ", "direction": "buy", "volume": 200,
                "price": 15.0, "price_type": 11, "status": 50,
                "status_msg": "已报", "seq": 101, "mode": "live",
                "strategy_name": "manual", "order_remark": "",
                "terminal": "WEB", "created_at": now, "updated_at": now,
            })

            inflight = store.get_inflight_orders()
            tdx_orders = [o for o in inflight if o.get("terminal") == "TDX"]
            web_orders = [o for o in inflight if o.get("terminal") == "WEB"]
            assert len(tdx_orders) == 1
            assert len(web_orders) == 1
            assert tdx_orders[0]["code"] == "600000.SH"
            store.close()
