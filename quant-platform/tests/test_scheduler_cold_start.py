"""LiveScheduler 冷启动场景测试

覆盖 2026-07-15 线上假阳性告警:
非交易时间(23:28)重启 live_trader → _tick() 触发 signal_heartbeat_check →
auto_buy 已启用但当日无心跳 → 误报警

根因: _check_signal_heartbeat 不区分"今日 14:55 之前服务一直在跑"和
"今日 14:55 之后才启动",后者心跳不存在是预期。

修复: 记录进程启动日期,跨日/冷启动跳过 signal_heartbeat_check 告警。
"""
from datetime import date
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def scheduler():
    """最小化 LiveScheduler 实例(只 _check_signal_heartbeat 路径需要 store/notifier/audit)"""
    from app.live_trader.scheduler import LiveScheduler

    class MockConfig:
        exit_scan_interval_sec = 60.0
        buy_signal_cutoff = "14:59"
        auto_buy_time = "14:50"
        daily_summary_enabled = False
        daily_summary_time = "15:30"

    class MockRuntimeState:
        auto_buy_enabled = True

    store = MagicMock()
    store.get_latest_heartbeat.return_value = None  # 模拟无心跳

    audit = MagicMock()
    notifier = MagicMock()
    runtime_state = MockRuntimeState()

    return LiveScheduler(
        config=MockConfig(),
        store=store,
        notifier=notifier,
        audit=audit,
        runtime_state=runtime_state,
    )


def test_cold_start_skips_heartbeat_check(scheduler):
    """跨日冷启动(进程 _process_start_date 早于今日)→ 跳过心跳检查,不告警。

    修复前:任何 current_time >= "14:55" 都会触发检查,
            重启瞬间无心跳会刷 WARNING + audit.log + notifier.send
    修复后:若进程启动日期不是今日 → 视为冷启动,只 info,不做告警
    """
    # 模拟跨日冷启动:进程昨日启动,今日 23:28 重启
    scheduler._process_start_date = date(2026, 7, 14)

    scheduler._check_signal_heartbeat()

    # 冷启动不该触发任何告警
    scheduler.audit.log.assert_not_called()
    scheduler.notifier.send.assert_not_called()


def test_same_day_start_runs_heartbeat_check(scheduler):
    """今日启动的进程(服务一直在跑)→ 正常触发心跳检查。

    auto_buy 已启用 + 无心跳 → 应该告警(原行为)。
    """
    # 模拟今日启动
    scheduler._process_start_date = date.today()

    scheduler._check_signal_heartbeat()

    # 今日启动 + 无心跳 → 应触发告警
    scheduler.audit.log.assert_called_once()
    call_args = scheduler.audit.log.call_args
    assert "signal_heartbeat_missing" in call_args[0]


def test_cold_start_does_not_add_to_executed_today(scheduler):
    """冷启动场景:不应该把 signal_heartbeat_check 加入 _executed_today。

    否则 _tick() 下一轮(下一次重启前不会重启,但同进程会)会跳过正常的检查。
    """
    scheduler._process_start_date = date(2026, 7, 14)
    assert "signal_heartbeat_check" not in scheduler._executed_today

    scheduler._check_signal_heartbeat()

    # 冷启动不应该标记为"已执行"
    assert "signal_heartbeat_check" not in scheduler._executed_today


def test_init_sets_process_start_date():
    """LiveScheduler __init__ 应设置 _process_start_date = date.today()。"""
    from app.live_trader.scheduler import LiveScheduler

    class MockConfig:
        exit_scan_interval_sec = 60.0

    s = LiveScheduler(config=MockConfig())
    assert hasattr(s, "_process_start_date")
    assert s._process_start_date == date.today()