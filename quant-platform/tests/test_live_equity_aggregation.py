"""净值曲线聚合单测(2026-07-16:统一日收盘口径,1日档=最近2日EOD折线)。

锁死三条不变量:
- days<=1(最近2日 EOD 折线): 返回最近 2 个有交易时段快照的交易日,每天一个点
  (<=15:30 的最后一条=收盘;最新一天盘中则取最新一条),过滤夜间/重启 stray 点。
- days>=2(每日 EOD): 每日只返回一个点(优先 <=15:30 的收盘快照),不返回全部 5min 点。
- stray 点(启动备份在 00:17/16:16 写入)不进曲线。
"""
from datetime import date, timedelta

import pytest

from app.live_trader.config import LiveTraderConfig
from app.live_trader.store import LiveTraderStore


def _make_store(tmp_path) -> LiveTraderStore:
    db = str(tmp_path / "lt_test.duckdb")
    cfg = LiveTraderConfig(db_path=db, lock_file=str(tmp_path / "lt.lock"),
                           wal_path=str(tmp_path / "deals.wal"))
    return LiveTraderStore(cfg)


@pytest.fixture
def store(tmp_path):
    """带自动 close 的 store(停 flusher 线程,否则 pytest 退出时挂起)。"""
    s = _make_store(tmp_path)
    yield s
    s.close()


def _insert(store: LiveTraderStore, rows):
    """rows: [(date_str, time_str, cash, market_value, total), ...]"""
    with store._db_lock:
        for d, t, c, mv, tot in rows:
            store._conn.execute(
                "INSERT INTO live_assets_backup "
                "(backup_date, backup_time, cash, frozen_cash, market_value, total_asset) "
                "VALUES (?,?,?,0,?,?)",
                [d, t, c, mv, tot],
            )


def _d(off: int) -> str:
    """相对今天偏移 off 天的 ISO 日期。"""
    return (date.today() + timedelta(days=off)).isoformat()


class TestDays1TwoPointLine:
    def test_days1_returns_two_recent_trading_days_eod(self, store):
        # 3 个交易日,各有盘中点+收盘+stray → 1日档取最近 2 日各一个 EOD 点
        _insert(store, [
            (_d(-3), "00:17", 100, 50, 150),   # stray
            (_d(-3), "15:00", 100, 60, 160),   # 前3日收盘(不取,只取最近2日)
            (_d(-2), "09:30", 100, 62, 162),
            (_d(-2), "15:00", 100, 65, 165),   # 昨日收盘 → 取
            (_d(-2), "16:16", 100, 65, 165),   # stray(盘后)
            (_d(-1), "10:00", 100, 63, 163),
            (_d(-1), "15:00", 100, 66, 166),   # 前1日收盘 → 取
        ])
        pts = store.get_equity_points(1)
        # 最近 2 个交易日,每天 1 个收盘点,连成折线
        assert len(pts) == 2
        assert [p["date"] for p in pts] == [_d(-2), _d(-1)]
        assert [p["time"] for p in pts] == ["15:00", "15:00"]
        assert [p["total"] for p in pts] == [165, 166]

    def test_days1_today_intraday_latest_plus_yesterday_close(self, store):
        # 昨天收盘 + 今天盘中点 → 1日档=昨收 + 今日最新(盘中实时),2 点连线
        _insert(store, [
            (_d(-1), "15:00", 100, 60, 160),   # 昨日收盘
            (_d(0), "00:17", 100, 60, 160),    # 今天 stray(不选,走 09:25-15:05 锁交易日)
            (_d(0), "10:00", 100, 58, 158),
            (_d(0), "11:30", 100, 59, 159),    # 今日最新盘中点
        ])
        pts = store.get_equity_points(1)
        assert len(pts) == 2
        assert [p["date"] for p in pts] == [_d(-1), _d(0)]
        assert [p["time"] for p in pts] == ["15:00", "11:30"]
        assert [p["total"] for p in pts] == [160, 159]

    def test_days1_only_one_day_data_falls_back_to_single(self, store):
        # 全新账户只有 1 个交易日数据 → 退化为 1 个点(画不出折线是数据问题,非逻辑问题)
        _insert(store, [
            (_d(0), "10:00", 100, 58, 158),
            (_d(0), "15:00", 100, 60, 160),
        ])
        pts = store.get_equity_points(1)
        assert len(pts) == 1
        assert pts[0]["date"] == _d(0)
        assert pts[0]["time"] == "15:00"


class TestDailyEod:
    def test_daily_one_point_per_day_prefers_close(self, store):
        # 三天,每天多条快照(含 stray),EOD 应取 15:00 收盘点
        _insert(store, [
            (_d(-3), "00:17", 100, 50, 150),   # stray
            (_d(-3), "09:30", 100, 50, 150),
            (_d(-3), "15:00", 100, 60, 160),   # 收盘 → EOD
            (_d(-3), "16:16", 100, 60, 160),   # stray(盘后)
            (_d(-2), "09:35", 100, 62, 162),
            (_d(-2), "15:00", 100, 65, 165),   # 收盘 → EOD
            (_d(-1), "10:00", 100, 63, 163),
            (_d(-1), "14:30", 100, 64, 164),
            (_d(-1), "15:00", 100, 66, 166),   # 收盘 → EOD
        ])
        pts = store.get_equity_points(5)
        # 每天一个点,共 3 个
        assert len(pts) == 3
        assert [p["date"] for p in pts] == [_d(-3), _d(-2), _d(-1)]
        # 每个点都是 15:00 收盘点,不是 stray 也不是 09:30
        assert [p["time"] for p in pts] == ["15:00", "15:00", "15:00"]
        assert [p["total"] for p in pts] == [160, 165, 166]

    def test_daily_not_returns_all_5min_points(self, store):
        # 单日塞 20 个盘中点 + stray,daily 档只应返回 1 个点
        rows = [(_d(-1), f"{h:02d}:{m:02d}", 100, 50 + h, 150 + h)
                for h in range(9, 15) for m in (0, 30)]
        rows.append((_d(-1), "00:17", 100, 50, 150))  # stray
        _insert(store, rows)
        pts = store.get_equity_points(30)
        assert len(pts) == 1, "daily 档应每日聚合为 1 点,不得返回全部 5min 点"

    def test_daily_fallback_to_last_when_no_close_point(self, store):
        # 某天只有盘前 stray,无 <=15:30 的点 → 退到当日最后一条
        _insert(store, [
            (_d(-1), "08:00", 100, 50, 150),
            (_d(-1), "08:30", 100, 51, 151),
        ])
        pts = store.get_equity_points(5)
        assert len(pts) == 1
        assert pts[0]["time"] == "08:30"  # 当日最后一条兜底
