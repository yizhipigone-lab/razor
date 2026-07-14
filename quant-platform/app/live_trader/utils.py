"""实盘交易公共工具函数（2026-07-14）

从 exit_monitor / sim_trader 提取的公共逻辑，供风控面板等模块调用。
"""
from datetime import date
from functools import lru_cache


@lru_cache(maxsize=1)
def _load_trading_calendar() -> set:
    """加载交易日历（缓存，进程级只读一次）。"""
    try:
        import json, pathlib
        cal_file = pathlib.Path(__file__).parent.parent.parent / "data" / "trading_calendar.json"
        if cal_file.exists():
            with open(cal_file, "r", encoding="utf-8") as f:
                dates = json.load(f)
            return {date.fromisoformat(d) if isinstance(d, str) else d for d in dates}
    except Exception:
        pass
    return set()


def calc_trading_days(entry_date) -> int:
    """返回 entry_date 到今日经历的交易日数（至少返回 1）。

    有交易日历时用交易日历计算，无日历则 fallback 自然日。
    与 exit_monitor._calc_hold_days 逻辑完全一致。
    """
    try:
        if entry_date is None:
            return 1
        if hasattr(entry_date, "date"):
            entry_d = entry_date.date()
        else:
            entry_d = entry_date
        cal = _load_trading_calendar()
        today = date.today()
        if cal:
            window = sorted(d for d in cal if entry_d <= d <= today)
            return max(1, len(window))
        return max(1, (today - entry_d).days)
    except Exception:
        return 1
