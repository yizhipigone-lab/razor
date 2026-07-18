# 5m 信号窗口与涨停过滤完整性修复 — 实现计划（Part 1：回测侧）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成回测侧修复：新建涨停判断共享模块、改造 `execution.can_buy` 与 `simple_runner.buy`、修复 `tdx_runner` 涨停前收与 5m 降级。

**Architecture:** 新建 `app/utils/limit_up.py` 作为涨停判断唯一真相源；`execution.can_buy` 增加 `strict` 参数；`tdx_runner` 5m 缺失时降级日线 close；涨停前收缺失时回退 parquet 日线。

**Tech Stack:** Python, pytest, pandas, pyarrow/parquet

**设计文档：** [docs/superpowers/specs/2026-07-18-5m-signal-window-and-limit-up-filter-design.md](docs/superpowers/specs/2026-07-18-5m-signal-window-and-limit-up-filter-design.md)

**Part 2：** [docs/superpowers/plans/2026-07-18-5m-signal-window-and-limit-up-filter-plan-part2.md](docs/superpowers/plans/2026-07-18-5m-signal-window-and-limit-up-filter-plan-part2.md)

---

## 文件结构（Part 1）

| 文件 | 动作 | 职责 |
|---|---|---|
| `app/utils/limit_up.py` | 新建 | 涨停幅度表、价格有效性检查、`is_limit_up` 共享函数 |
| `tests/test_limit_up.py` | 新建 | `limit_up` 模块单元测试 |
| `app/backtest/execution.py:21-47` | 修改 | 删除本地 `get_limit_up_pct`，`can_buy` 加 `strict` 参数并委托 `is_limit_up` |
| `app/backtest/simple_runner.py:101-136` | 修改 | `buy()` 缺 `prev_close` 时走 `strict=False` 兼容路径并加 deprecation warning |
| `app/backtest/tdx_runner.py` | 修改 | 5m bar 缺失降级日线 close；涨停前收从 parquet 兜底 |
| `tests/test_execution.py` | 修改 | 更新 `can_buy` 测试，覆盖 strict、NaN、None |
| `tests/test_critical_fixes.py:35-58` | 修改 | 旧三参兼容契约 + 四参 fail-closed 契约 |
| `tests/test_tdx_runner_prev_close.py` | 新建 | parquet 前收读取测试 |
| `tests/test_tdx_runner_fallback.py` | 新建 | 5m 降级日线 close 单元测试 |

---

## Task 1: 新建涨停判断共享模块

**Files:**
- Create: `app/utils/limit_up.py`
- Test: `tests/test_limit_up.py`

### Step 1: 写失败测试

创建 `tests/test_limit_up.py`：

```python
import math
import pytest
from app.utils.limit_up import get_limit_up_pct, is_limit_up, _is_valid_price


class TestGetLimitUpPct:
    def test_main_board(self):
        assert abs(get_limit_up_pct("600519") - 0.10) < 1e-6

    def test_gem(self):
        assert abs(get_limit_up_pct("300750") - 0.20) < 1e-6

    def test_star(self):
        assert abs(get_limit_up_pct("688981") - 0.20) < 1e-6

    def test_bse(self):
        assert abs(get_limit_up_pct("830123") - 0.30) < 1e-6


class TestIsValidPrice:
    def test_valid(self):
        assert _is_valid_price(10.0) is True

    def test_zero_invalid(self):
        assert _is_valid_price(0.0) is False

    def test_negative_invalid(self):
        assert _is_valid_price(-1.0) is False

    def test_nan_invalid(self):
        assert _is_valid_price(float("nan")) is False

    def test_none_invalid(self):
        assert _is_valid_price(None) is False


class TestIsLimitUp:
    def test_normal_not_limit(self):
        is_limit, reason = is_limit_up("600519", 100.0, 103.0)
        assert is_limit is False
        assert reason == "OK"

    def test_limit_up_blocked(self):
        is_limit, reason = is_limit_up("300750", 100.0, 120.0)
        assert is_limit is True
        assert "limit_up" in reason

    def test_strict_missing_prev_close(self):
        is_limit, reason = is_limit_up("600519", 0.0, 103.0, strict=True)
        assert is_limit is True
        assert reason == "missing_price_data"

    def test_non_strict_missing_prev_close(self):
        is_limit, reason = is_limit_up("600519", 0.0, 103.0, strict=False)
        assert is_limit is False
        assert reason == "missing_price_data_ok"

    def test_nan_input_strict(self):
        is_limit, reason = is_limit_up("600519", float("nan"), 103.0, strict=True)
        assert is_limit is True
        assert reason == "missing_price_data"

    def test_none_input_strict(self):
        is_limit, reason = is_limit_up("600519", None, 103.0, strict=True)
        assert is_limit is True
        assert reason == "missing_price_data"
```

### Step 2: 运行测试确认失败

```bash
pytest tests/test_limit_up.py -v
```

Expected: 大量 `ModuleNotFoundError` / `ImportError`。

### Step 3: 实现 `app/utils/limit_up.py`

```python
from typing import Tuple


LIMIT_UP_MAP = {
    "300": 0.20,
    "301": 0.20,
    "688": 0.20,
    "8": 0.30,
    "4": 0.30,
}
DEFAULT_LIMIT_UP = 0.10


def get_limit_up_pct(code: str) -> float:
    """根据股票代码前缀返回涨停幅度。"""
    if code.startswith(("300", "301", "688")):
        return LIMIT_UP_MAP["300"]
    if code.startswith(("8", "4")):
        return LIMIT_UP_MAP["8"]
    return DEFAULT_LIMIT_UP


def _is_valid_price(x) -> bool:
    """价格有效性检查：非 None、非 NaN、大于 0。"""
    return x is not None and x > 0


def is_limit_up(
    code: str,
    prev_close: float,
    price: float,
    strict: bool = True,
) -> Tuple[bool, str]:
    """判断 price 是否达到 code 的涨停价。

    Returns:
        (是否涨停, reason)
    """
    if not (_is_valid_price(prev_close) and _is_valid_price(price)):
        if strict:
            return True, "missing_price_data"
        return False, "missing_price_data_ok"

    change = (price - prev_close) / prev_close
    limit = get_limit_up_pct(code)
    if change >= limit * 0.995:
        return True, f"limit_up({change * 100:.1f}%)"
    return False, "OK"
```

### Step 4: 运行测试确认通过

```bash
pytest tests/test_limit_up.py -v
```

Expected: 10 passed。

### Step 5: Commit

```bash
git add app/utils/limit_up.py tests/test_limit_up.py
git commit -m "feat(limit_up): 新建涨停判断共享模块与单元测试"
```

---

## Task 2: 改造 `execution.can_buy`

**Files:**
- Modify: `app/backtest/execution.py:21-47`
- Test: `tests/test_execution.py`

### Step 1: 更新测试

修改 `tests/test_execution.py` 中 `TestCanBuy` 类：

```python
class TestCanBuy:
    def test_normal_buy(self):
        ok, _ = can_buy("600519", 100.0, 103.0)
        assert ok

    def test_limit_up_blocked(self):
        ok, msg = can_buy("300750", 100.0, 120.0)
        assert not ok
        assert "涨停" in msg or "limit_up" in msg

    def test_strict_prev_close_zero_rejects(self):
        ok, msg = can_buy("000001", 0, 110.0, strict=True)
        assert not ok
        assert msg == "missing_price_data"

    def test_non_strict_prev_close_zero_ok(self):
        ok, msg = can_buy("000001", 0, 110.0, strict=False)
        assert ok
        assert msg == "missing_price_data_ok"

    def test_strict_nan_rejects(self):
        ok, msg = can_buy("000001", float("nan"), 110.0, strict=True)
        assert not ok
        assert msg == "missing_price_data"

    def test_strict_none_rejects(self):
        ok, msg = can_buy("000001", None, 110.0, strict=True)
        assert not ok
        assert msg == "missing_price_data"
```

### Step 2: 运行测试确认失败

```bash
pytest tests/test_execution.py::TestCanBuy -v
```

Expected: 失败，因为 `can_buy` 还没有 `strict` 参数。

### Step 3: 修改 `app/backtest/execution.py`

保留 import 区，在文件顶部增加：

```python
from app.utils.limit_up import get_limit_up_pct as _limit_up_get_limit_up_pct, is_limit_up
```

替换 `get_limit_up_pct` 函数为委托：

```python
def get_limit_up_pct(code: str) -> float:
    """根据股票代码前缀返回涨停幅度（兼容旧导入）。"""
    return _limit_up_get_limit_up_pct(code)
```

替换 `can_buy`：

```python
def can_buy(code: str, prev_close: float, today_high: float, strict: bool = True) -> Tuple[bool, str]:
    """涨停封板不能买入

    Args:
        code: 股票代码
        prev_close: 昨日收盘价
        today_high: 今日最高价或当前拟成交价
        strict: True=缺数据按涨停处理（fail-closed）;False=兼容旧三参调用

    Returns:
        (ok, reason): ok=True 可买, ok=False 不可买及原因
    """
    is_limit, reason = is_limit_up(code, prev_close, today_high, strict=strict)
    return (not is_limit, reason)
```

删除旧的 `LIMIT_UP_MAP` / `DEFAULT_LIMIT_UP` 常量。

### Step 4: 运行测试确认通过

```bash
pytest tests/test_execution.py -v
```

Expected: 全部通过。

### Step 5: Commit

```bash
git add app/backtest/execution.py tests/test_execution.py
git commit -m "feat(execution): can_buy 加 strict 参数并委托 limit_up 模块"
```

---

## Task 3: 改造 `simple_runner.buy` 兼容路径

**Files:**
- Modify: `app/backtest/simple_runner.py:101-136`
- Test: `tests/test_critical_fixes.py:35-58`

### Step 1: 更新测试

修改 `tests/test_critical_fixes.py:35-58`：

```python
def test_simple_runner_buy_without_prev_close_warns_compat(caplog):
    """HIGH-3 (compat 4b) 回归测试 - 旧三参缺 prev_close 仍放行,但加 deprecation warning

    scripts/ 下 22 个 CLI 用旧三参 eng.buy(d, code, px) 不应崩 — 此测试守住兼容降级。
    """
    import logging
    import warnings
    from app.backtest.simple_runner import FastEngine
    td_list = [date(2024, 1, d) for d in range(2, 11)]
    params = {
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "min_buy_amt": 5_000,
    }
    eng = FastEngine(td_list, params)

    with caplog.at_level(logging.WARNING, logger="FastEngine"):
        with pytest.warns(DeprecationWarning, match="prev_close"):
            res = eng.buy(date(2024, 1, 3), "600000", 10.0, prev_close=None)

    assert res is not None
    assert getattr(res, "code", None) == "600000"
    assert any("缺 prev_close" in r.message for r in caplog.records)


def test_simple_runner_buy_with_prev_close_strict_rejects_limit_up(caplog):
    """HIGH-3 回归测试 - 主回测路径传真实 prev_close 时,涨停日应被拒"""
    import logging
    from app.backtest.simple_runner import FastEngine
    td_list = [date(2024, 1, d) for d in range(2, 11)]
    params = {
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "min_buy_amt": 5_000,
    }
    eng = FastEngine(td_list, params)

    with caplog.at_level(logging.WARNING, logger="FastEngine"):
        res = eng.buy(date(2024, 1, 3), "600000", 11.0, prev_close=10.0)
    assert res is None, "涨停日应被拒"
    assert not any("缺 prev_close" in r.message for r in caplog.records)
```

### Step 2: 运行测试确认失败

```bash
pytest tests/test_critical_fixes.py::test_simple_runner_buy_without_prev_close_warns_compat tests/test_critical_fixes.py::test_simple_runner_buy_with_prev_close_strict_rejects_limit_up -v
```

Expected: 失败。

### Step 3: 修改 `app/backtest/simple_runner.py`

在文件顶部增加 import：

```python
import warnings
from app.utils.limit_up import _is_valid_price
```

替换 `buy` 方法中 `prev_close` 处理逻辑：

```python
    def buy(self, d, code, px, prev_close=None):
        """d 当前日, code 股票, px 当前价, prev_close 昨日真实收盘价(可选)

        2026-07-18: 缺 prev_close 时走 strict=False 兼容路径(旧 scripts/ CLI 不崩),
        但加 DeprecationWarning 提示迁移到四参调用。
        主回测路径始终传真实 prev_close,走 strict=True fail-closed。
        """
        if code in self.positions: return None

        if _is_valid_price(prev_close):
            can_buy_ok, _ = can_buy(code, prev_close, px, strict=True)
        else:
            warnings.warn(
                f"eng.buy(d, code, px) 缺 prev_close，涨停过滤降级。"
                f"请改为 eng.buy(d, code, px, prev_close=...)。",
                DeprecationWarning,
                stacklevel=2,
            )
            import logging
            logging.getLogger("FastEngine").warning(
                f"buy() 缺 prev_close (code={code} d={d}),涨停过滤降级 — "
                "建议调用方从 closes 取前一个交易日"
            )
            can_buy_ok, _ = can_buy(code, px, px, strict=False)

        if not can_buy_ok:
            return None
        ...
```

保留后续买入逻辑不变。

### Step 4: 运行测试确认通过

```bash
pytest tests/test_critical_fixes.py -v
```

Expected: 全部通过。

### Step 5: Commit

```bash
git add app/backtest/simple_runner.py tests/test_critical_fixes.py
git commit -m "feat(simple_runner): buy() 缺 prev_close 走 strict=False 兼容并加 deprecation"
```

---

## Task 4: 改造 `tdx_runner` 涨停前收取数

**Files:**
- Modify: `app/backtest/tdx_runner.py`
- Test: `tests/test_tdx_runner_prev_close.py`（新建）

### Step 1: 新增 parquet 读取辅助函数

在 `app/backtest/tdx_runner.py` 顶部增加 import：

```python
from functools import lru_cache
from typing import Optional
import pandas as pd
from app.utils.limit_up import _is_valid_price
```

在模块级增加：

```python
@lru_cache(maxsize=128)
def _load_daily_parquet(code: str, parquet_dir: Optional[str] = None):
    """按 code 缓存读取 parquet 日线文件。返回 DataFrame 供只读使用。"""
    path = f"{parquet_dir or 'data/parquet/daily'}/{code}.parquet"
    try:
        return pd.read_parquet(path)
    except Exception:
        return None


def get_prev_close_from_parquet(
    code: str, trade_date: str, parquet_dir: Optional[str] = None
) -> Optional[float]:
    """从 code 的 parquet 日线文件中，读取 trade_date 前一交易日的 close。"""
    df = _load_daily_parquet(code, parquet_dir=parquet_dir)
    if df is None or df.empty:
        return None
    if "date" in df.columns:
        df = df.sort_values("date")
        prev_rows = df[df["date"] < trade_date]
        if prev_rows.empty:
            return None
        return float(prev_rows.iloc[-1]["close"])
    if isinstance(df.index, pd.DatetimeIndex):
        trade_dt = pd.to_datetime(trade_date)
        prev_rows = df[df.index < trade_dt]
        if prev_rows.empty:
            return None
        return float(prev_rows.iloc[-1]["close"])
    return None
```

### Step 2: 修改日内买入路径

定位 `app/backtest/tdx_runner.py:475-485`，替换为：

```python
                    # L29 修复: 涨停买入过滤 - 委托给 execution.can_buy
                    prev_close = None
                    if prev_day is not None:
                        prev_snap = prices_by_date.get(str(prev_day), {})
                        prev_bar = prev_snap.get(code, {})
                        if isinstance(prev_bar, dict):
                            prev_close = prev_bar.get("close")
                    if not _is_valid_price(prev_close):
                        prev_close = get_prev_close_from_parquet(code, d_str)
                    if not _is_valid_price(prev_close):
                        logger.warning(
                            "涨停判断缺 prev_close，跳过买入: code=%s date=%s", code, d_str
                        )
                        continue
                    can_buy_ok, _ = can_buy(code, prev_close, px, strict=True)
                    if not can_buy_ok:
                        continue
```

### Step 3: 修改日线买入路径

定位 `app/backtest/tdx_runner.py:877-885`，替换为：

```python
            # 2026-07-15 HIGH-3: 计算 prev_close - 用前一个交易日的 close 作为涨停判断基准
            prev_close_for_buy = None
            if d_idx > 0:
                _prev_snap = prices_by_date.get(str(td_list[d_idx - 1]), {})
                _prev_bar = _prev_snap.get(code, {})
                if isinstance(_prev_bar, dict):
                    prev_close_for_buy = _prev_bar.get("close")
            if not _is_valid_price(prev_close_for_buy):
                prev_close_for_buy = get_prev_close_from_parquet(code, d_str)
            if not _is_valid_price(prev_close_for_buy):
                logger.warning(
                    "日线回测涨停判断缺 prev_close，跳过买入: code=%s date=%s", code, d_str
                )
                continue
            if eng.buy(d_obj, code, px, prev_close=prev_close_for_buy):
                pass
```

### Step 4: 新建测试

创建 `tests/test_tdx_runner_prev_close.py`：

```python
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.backtest.tdx_runner import get_prev_close_from_parquet


class TestGetPrevCloseFromParquet:
    def test_reads_previous_day_close_string_date(self, tmp_path):
        code = "600000"
        parquet_dir = tmp_path / "daily"
        parquet_dir.mkdir()
        df = pd.DataFrame({
            "date": ["20240101", "20240102", "20240103"],
            "close": [10.0, 10.5, 11.0],
        })
        df.to_parquet(parquet_dir / f"{code}.parquet", index=False)

        prev_close = get_prev_close_from_parquet(code, "20240103", parquet_dir=str(parquet_dir))
        assert prev_close == 10.5

    def test_reads_previous_day_close_datetime_index(self, tmp_path):
        code = "600000"
        parquet_dir = tmp_path / "daily"
        parquet_dir.mkdir()
        df = pd.DataFrame({
            "close": [10.0, 10.5, 11.0],
        }, index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]))
        df.to_parquet(parquet_dir / f"{code}.parquet")

        prev_close = get_prev_close_from_parquet(code, "20240103", parquet_dir=str(parquet_dir))
        assert prev_close == 10.5

    def test_no_previous_day_returns_none(self, tmp_path):
        code = "600000"
        parquet_dir = tmp_path / "daily"
        parquet_dir.mkdir()
        df = pd.DataFrame({
            "date": ["20240101"],
            "close": [10.0],
        })
        df.to_parquet(parquet_dir / f"{code}.parquet", index=False)

        prev_close = get_prev_close_from_parquet(code, "20240101", parquet_dir=str(parquet_dir))
        assert prev_close is None

    def test_missing_file_returns_none(self, tmp_path):
        prev_close = get_prev_close_from_parquet("600000", "20240103", parquet_dir=str(tmp_path))
        assert prev_close is None
```

### Step 5: 运行相关测试

```bash
pytest tests/test_tdx_runner_prev_close.py -v
```

Expected: 4 passed。

### Step 6: Commit

```bash
git add app/backtest/tdx_runner.py tests/test_tdx_runner_prev_close.py
git commit -m "feat(tdx_runner): 涨停前收缺数据时回退 parquet 日线"
```

---

## Task 5: 改造 `tdx_runner` 5m 降级日线买入

**Files:**
- Modify: `app/backtest/tdx_runner.py`
- Test: `tests/test_tdx_runner_fallback.py`（新建）

### Step 1: 抽取价格解析函数

在 `app/backtest/tdx_runner.py` 中新增：

```python
def _resolve_intraday_buy_price(
    code: str,
    d_str: str,
    stocks_with_intraday: set,
    first_bar_of_day: dict,
    prices_by_date: dict,
):
    """解析日内回测买入价。

    Returns:
        (price, source, fallback_increment)
        source: "intraday" / "daily_fallback" / "daily" / None
    """
    if code in stocks_with_intraday:
        bar_for_code = first_bar_of_day.get((code, d_str))
        if bar_for_code is not None:
            return bar_for_code["close"], "intraday", 0
        px = prices_by_date.get(d_str, {}).get(code, {}).get("close")
        if _is_valid_price(px):
            return px, "daily_fallback", 1
        return None, None, 0

    day_price = prices_by_date.get(d_str, {}).get(code)
    if day_price is None:
        return None, None, 0
    px = day_price["close"]
    if _is_valid_price(px):
        return px, "daily", 0
    return None, None, 0
```

### Step 2: 替换原买入逻辑

定位 `app/backtest/tdx_runner.py:460-471`，替换为：

```python
                    px, source, fb_inc = _resolve_intraday_buy_price(
                        code, d_str, stocks_with_intraday, first_bar_of_day, prices_by_date
                    )
                    if px is None:
                        skipped_count += 1
                        if source is None:
                            logger.warning(
                                "无有效买入价，跳过买入: code=%s date=%s", code, d_str
                            )
                        continue
                    fallback_count += fb_inc
                    if source == "daily_fallback":
                        logger.info(
                            "5m bar 缺失，降级日线 close 买入: code=%s date=%s px=%s",
                            code, d_str, px,
                        )
```

### Step 3: 透传 fallback 计数

找到回测结果摘要组装位置，增加：

```python
result["intraday_window_fallback_count"] = fallback_count
```

### Step 4: 新建单元测试

创建 `tests/test_tdx_runner_fallback.py`：

```python
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.backtest.tdx_runner import _resolve_intraday_buy_price


class TestResolveIntradayBuyPrice:
    def test_5m_bar_available(self):
        px, source, fb = _resolve_intraday_buy_price(
            "600000", "20240103", {"600000"},
            {("600000", "20240103"): {"close": 25.0}},
            {}
        )
        assert px == 25.0
        assert source == "intraday"
        assert fb == 0

    def test_5m_missing_falls_back_to_daily_close(self):
        px, source, fb = _resolve_intraday_buy_price(
            "600000", "20240103", {"600000"},
            {},
            {"20240103": {"600000": {"close": 24.0}}}
        )
        assert px == 24.0
        assert source == "daily_fallback"
        assert fb == 1

    def test_5m_missing_and_daily_invalid_returns_none(self):
        px, source, fb = _resolve_intraday_buy_price(
            "600000", "20240103", {"600000"},
            {},
            {"20240103": {"600000": {"close": 0.0}}}
        )
        assert px is None
        assert source is None
        assert fb == 0

    def test_not_in_intraday_set_uses_daily(self):
        px, source, fb = _resolve_intraday_buy_price(
            "600000", "20240103", set(),
            {},
            {"20240103": {"600000": {"close": 23.0}}}
        )
        assert px == 23.0
        assert source == "daily"
        assert fb == 0

    def test_none_daily_close_returns_none(self):
        px, source, fb = _resolve_intraday_buy_price(
            "600000", "20240103", set(),
            {},
            {"20240103": {}}
        )
        assert px is None
        assert source is None
        assert fb == 0
```

### Step 5: 运行测试

```bash
pytest tests/test_tdx_runner_fallback.py -v
```

Expected: 5 passed。

### Step 6: Commit

```bash
git add app/backtest/tdx_runner.py tests/test_tdx_runner_fallback.py
git commit -m "feat(tdx_runner): 5m bar 缺失时降级日线 close 买入"
```

---

## Part 1 完成检查点

- [ ] `app/utils/limit_up.py` 及测试通过
- [ ] `execution.can_buy` strict 参数及测试通过
- [ ] `simple_runner.buy` 兼容路径及测试通过
- [ ] `tdx_runner` 涨停前收 parquet 兜底及测试通过
- [ ] `tdx_runner` 5m 降级日线买入及测试通过

完成后继续执行 [Part 2](docs/superpowers/plans/2026-07-18-5m-signal-window-and-limit-up-filter-plan-part2.md)。
