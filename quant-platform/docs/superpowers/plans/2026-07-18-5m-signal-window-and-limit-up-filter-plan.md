# 5m 信号窗口与涨停过滤完整性修复 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复回测 5m 窗口外信号跳过、回测涨停过滤 fail-open、实盘无涨停拒买闸三个缺口，使回测更真实、实盘更安全。

**Architecture:** 新建 `app/utils/limit_up.py` 作为涨停判断唯一真相源；`execution.can_buy` 增加 `strict` 参数；`tdx_runner` 5m 缺失时降级日线 close；`RiskGate` 新增 5c 涨停拒买闸并复用已有行情数据。

**Tech Stack:** Python, pytest, pandas, pyarrow/parquet, QMT HTTP API

**设计文档：** [docs/superpowers/specs/2026-07-18-5m-signal-window-and-limit-up-filter-design.md](docs/superpowers/specs/2026-07-18-5m-signal-window-and-limit-up-filter-design.md)

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `app/utils/limit_up.py` | 新建 | 涨停幅度表、价格有效性检查、`is_limit_up` 共享函数 |
| `tests/test_limit_up.py` | 新建 | `limit_up` 模块单元测试 |
| `app/backtest/execution.py:21-47` | 修改 | 删除本地 `get_limit_up_pct`，`can_buy` 加 `strict` 参数并委托 `is_limit_up` |
| `app/backtest/simple_runner.py:101-136` | 修改 | `buy()` 缺 `prev_close` 时走 `strict=False` 兼容路径并加 deprecation warning |
| `app/backtest/tdx_runner.py:460-485` | 修改 | 5m bar 缺失降级日线 close；涨停前收从 parquet 兜底 |
| `app/backtest/tdx_runner.py:877-885` | 修改 | 日线买入路径涨停前收从 parquet 兜底 |
| `tests/test_execution.py` | 修改 | 更新 `can_buy` 测试，覆盖 strict、NaN、None |
| `tests/test_critical_fixes.py:35-58` | 修改 | 旧三参兼容契约 + 四参 fail-closed 契约 |
| `tests/test_tdx_runner_fallback.py` | 新建 | 5m 降级日线 close 集成测试 |
| `app/live_trader/config.py` | 修改 | 加 `limit_up_gate_enabled` 开关 |
| `app/live_trader/risk_gate.py:125-165` | 修改 | 5b 之后、闸门 7 之前插入 5c 涨停拒买闸 |
| `tests/test_live_trader_smoke.py` | 修改 | 新增 5c 闸通过/关闭测试 |

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

Expected: 大量 `ModuleNotFoundError` / `ImportError`（因为 `app/utils/limit_up.py` 还不存在）。

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

Expected: 失败，因为 `can_buy` 还没有 `strict` 参数，且 `test_prev_close_zero_skips` 旧测试已被替换。

### Step 3: 修改 `app/backtest/execution.py`

保留 import 区，在文件顶部增加：

```python
from app.utils.limit_up import get_limit_up_pct, is_limit_up
```

替换 `get_limit_up_pct` 函数为委托：

```python
def get_limit_up_pct(code: str) -> float:
    """根据股票代码前缀返回涨停幅度（委托给共享模块）。"""
    return _limit_up_get_limit_up_pct(code)
```

或者直接删除本地实现，让导入别名保留兼容性。推荐做法：

```python
from app.utils.limit_up import get_limit_up_pct as _limit_up_get_limit_up_pct, is_limit_up


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

同时删除旧的 `LIMIT_UP_MAP` / `DEFAULT_LIMIT_UP` 常量（已在 `app/utils/limit_up.py` 中）。

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

    # 缺 prev_close 时 buy 不应崩,且能买入（兼容放行）
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
            # 兼容降级路径 — 给 scripts/ 下 22 个 CLI 的窗口期
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
    # 假设 df 索引或 'date' 列为交易日字符串 YYYYMMDD
    if "date" in df.columns:
        df = df.sort_values("date")
        prev_rows = df[df["date"] < trade_date]
        if prev_rows.empty:
            return None
        return float(prev_rows.iloc[-1]["close"])
    # 若索引是 date
    if isinstance(df.index, pd.DatetimeIndex):
        trade_dt = pd.to_datetime(trade_date)
        prev_rows = df[df.index < trade_dt]
        if prev_rows.empty:
            return None
        return float(prev_rows.iloc[-1]["close"])
    return None
```

调用点同步更新：
```python
prev_close = get_prev_close_from_parquet(code, d_str)
```
无需改（默认路径）。测试时传入 `parquet_dir=...`。

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
        fallback_increment: 0 或 1（仅 daily_fallback 时）
    """
    if code in stocks_with_intraday:
        bar_for_code = first_bar_of_day.get((code, d_str))
        if bar_for_code is not None:
            return bar_for_code["close"], "intraday", 0
        # 降级：用日线 close
        px = prices_by_date.get(d_str, {}).get(code, {}).get("close")
        if _is_valid_price(px):
            return px, "daily_fallback", 1
        return None, None, 0

    # 无5m数据：用日线收盘价买入
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

找到回测结果摘要组装位置（通常在函数末尾返回的 dict 中），增加：

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

## Task 6: 新增实盘 RiskGate 涨停拒买闸

**Files:**
- Modify: `app/live_trader/config.py`
- Modify: `app/live_trader/risk_gate.py:125-165`
- Test: `tests/test_live_trader_smoke.py`

### Step 1: 修改 `LiveTraderConfig`

在 `app/live_trader/config.py` 的 RiskGate 段增加：

```python
    limit_up_gate_enabled: bool = True       # 闸门5c 涨停封板拒买开关
```

在 `load_config()` 的 `LiveTraderConfig(...)` 构造参数中增加：

```python
        limit_up_gate_enabled=cfg_dict.get("limit_up_gate_enabled", True),
```

### Step 2: 修改 `RiskGate`

在 `app/live_trader/risk_gate.py` 顶部增加 import：

```python
from app.utils.limit_up import _is_valid_price, is_limit_up
```

在 5b 闸门之后、闸门 9 之前插入 5c 闸：

```python
            # 闸门 5c: 涨停封板拒买
            if is_buy and self.config.limit_up_gate_enabled:
                prev_close, price = None, None
                if quote:
                    prev_close = quote.get("lastClose") or quote.get("preClose")
                    price = quote.get("lastPrice")

                if not (_is_valid_price(prev_close) and _is_valid_price(price)):
                    # 优先复用 quote 失败,尝试 quote_source 降级
                    try:
                        from app.data_manager.quote_source import get_realtime_quotes
                        df = get_realtime_quotes([code])
                        if df is not None and not df.empty:
                            row = df.iloc[0]
                            prev_close = row.get("last_close")
                            price = row.get("price")
                    except Exception as e:
                        logger.warning(f"闸门5c quote_source 降级失败: {e}")

                if not (_is_valid_price(prev_close) and _is_valid_price(price)):
                    gates.append(self._gate("5c", "涨停拒买", False, "有效行情", "缺价fail-safe"))
                    return (False, gates, "涨停判断缺行情,fail-safe拒买")

                is_limit, reason = is_limit_up(code, prev_close, price, strict=True)
                if is_limit:
                    gates.append(self._gate("5c", "涨停拒买", False, "未涨停", f"涨停{reason}"))
                    return (False, gates, f"涨停封板拒买: {reason}")
                gates.append(self._gate("5c", "涨停拒买", True, "未涨停", "OK"))
```

### Step 3: 更新 smoke test

在 `tests/test_live_trader_smoke.py` 中 `test_risk_gate_t1_sell_check` 之后增加：

```python
def test_risk_gate_5c_limit_up_blocks(tmp_config, store):
    """闸门5c:涨停股买入被拒,且不计入连续拒绝"""
    from app.live_trader.risk_gate import RiskGate
    from app.live_trader.kill_switch import KillSwitch
    from app.live_trader.schemas import OrderIntent

    ks = KillSwitch(tmp_config, store)
    rg = RiskGate(tmp_config, store, ks, qmt_wrapper=MagicMock(connected=True))

    intent = OrderIntent(code="600000.SH", direction="buy", volume=100, price=11.0)
    passed, gates, reason = rg.check(
        intent, asset={"cash": 500000}, quote={"lastClose": 10.0, "lastPrice": 11.0}
    )
    assert passed is False
    assert "涨停" in reason
    assert not rg._check_consecutive_rejection()


def test_risk_gate_5c_limit_up_disabled(tmp_config, store):
    """闸门5c:开关关闭时放行"""
    from app.live_trader.config import LiveTraderConfig
    from app.live_trader.risk_gate import RiskGate
    from app.live_trader.kill_switch import KillSwitch
    from app.live_trader.schemas import OrderIntent

    cfg = LiveTraderConfig(**{**tmp_config.__dict__, "limit_up_gate_enabled": False})
    ks = KillSwitch(cfg, store)
    rg = RiskGate(cfg, store, ks, qmt_wrapper=MagicMock(connected=True))

    intent = OrderIntent(code="600000.SH", direction="buy", volume=100, price=11.0)
    passed, gates, reason = rg.check(
        intent, asset={"cash": 500000}, quote={"lastClose": 10.0, "lastPrice": 11.0}
    )
    assert passed is True
```

### Step 4: 运行测试

```bash
pytest tests/test_live_trader_smoke.py::test_risk_gate_5c_limit_up_blocks tests/test_live_trader_smoke.py::test_risk_gate_5c_limit_up_disabled -v
```

Expected: 通过。

### Step 5: Commit

```bash
git add app/live_trader/config.py app/live_trader/risk_gate.py tests/test_live_trader_smoke.py
git commit -m "feat(risk_gate): 新增 5c 涨停拒买闸与开关"
```

---

## Task 7: 跑基线 diff 与冒烟测试

**Files:**
- Create: `scripts/run_limit_up_baseline.py`
- Create: `scripts/diff_limit_up_baseline.py`
- Create: `docs/reports/2026-07-18-limit-up-baseline-diff.md`

### Step 1: 创建基线运行脚本

创建 `scripts/run_limit_up_baseline.py`：

```python
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.backtest.simple_runner import run_backtest


def main():
    parser = argparse.ArgumentParser(description="跑 limit-up 修复基线")
    parser.add_argument("--strategy", default="QUANTQQ", help="策略名")
    parser.add_argument("--start", default="20230101", help="起始日期")
    parser.add_argument("--end", default="20240630", help="结束日期")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    args = parser.parse_args()

    params = {
        "strategy_name": args.strategy,
        "start_date": args.start,
        "end_date": args.end,
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "min_buy_amt": 5_000,
        "hard_stop": 0.05,
        "trail_activate": 0.10,
        "trail_dd": 0.05,
        "time_exit_days": 20,
        "time_exit_profit": 0.03,
        "time_force_days": 5,
        "same_stock_cooldown": 20,
        "loss_streak_halve": 3,
        "loss_streak_pause": 5,
        "use_atr_trail": True,
        "atr_trail_multiplier": 1.0,
        "take_profit_tiers": [],
        "first_day_exit_min_profit": 0.03,
        "first_day_exit_days": 1,
        "signal_params": {},
    }

    result = run_backtest(params)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"基线已保存: {output_path}")


if __name__ == "__main__":
    main()
```

### Step 2: 跑改前基线

```bash
git stash
python scripts/run_limit_up_baseline.py --output output/baseline_before.json
```

Expected: 基线运行完成，`output/baseline_before.json` 生成。

### Step 3: 恢复修改并跑改后基线

```bash
git stash pop
python scripts/run_limit_up_baseline.py --output output/baseline_after.json
```

Expected: 改后基线运行完成，`output/baseline_after.json` 生成。

### Step 4: 创建 diff 脚本

创建 `scripts/diff_limit_up_baseline.py`：

```python
import argparse
import json
from pathlib import Path


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="对比 limit-up 修复前后基线")
    parser.add_argument("before", help="改前基线 JSON")
    parser.add_argument("after", help="改后基线 JSON")
    parser.add_argument("--output", default="docs/reports/2026-07-18-limit-up-baseline-diff.md")
    args = parser.parse_args()

    before = load(args.before)
    after = load(args.after)

    sb = before["summary"]
    sa = after["summary"]

    lines = [
        "# 5m/涨停修复基线 Diff 报告",
        "",
        f"- 策略: {before.get('strategy_name', 'QUANTQQ')}",
        f"- 区间: {before.get('start_date', '?')} ~ {before.get('end_date', '?')}",
        "",
        "| 指标 | 改前 | 改后 | 变化 |",
        "|---|---|---|---|",
        f"| 收益率 | {sb['total_return']:+.2f}% | {sa['total_return']:+.2f}% | {sa['total_return'] - sb['total_return']:+.2f}% |",
        f"| 最大回撤 | {sb['max_drawdown']:.2f}% | {sa['max_drawdown']:.2f}% | {sa['max_drawdown'] - sb['max_drawdown']:+.2f}% |",
        f"| 交易笔数 | {sb['trades']} | {sa['trades']} | {sa['trades'] - sb['trades']:+d} |",
        f"| 买入笔数 | {sb.get('buys', sb['trades'])} | {sa.get('buys', sa['trades'])} | {sa.get('buys', sa['trades']) - sb.get('buys', sb['trades']):+d} |",
        f"| 5m 降级买入笔数 | - | {sa.get('intraday_window_fallback_count', 0)} | - |",
        f"| 胜率 | {sb['win_rate']:.1f}% | {sa['win_rate']:.1f}% | {sa['win_rate'] - sb['win_rate']:+.1f}% |",
        f"| 夏普 | {sb['sharpe']} | {sa['sharpe']} | - |",
        "",
        "## 关键观察",
        "",
        "- 交易笔数变化应主要由两部分组成：5m 降级使部分跳过信号变成买入（增加），涨停 fail-closed 使部分涨停日被拒（减少）。",
        "- 若收益/回撤出现 >5% 的异常跳变，需回查具体交易明细。",
        "",
        "## 退出原因分布",
        "",
        f"- 改前: {sb['exit_reasons']}",
        f"- 改后: {sa['exit_reasons']}",
    ]

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Diff 报告已保存: {output_path}")


if __name__ == "__main__":
    main()
```

### Step 5: 生成 diff 报告

```bash
python scripts/diff_limit_up_baseline.py output/baseline_before.json output/baseline_after.json
```

Expected: `docs/reports/2026-07-18-limit-up-baseline-diff.md` 生成。

### Step 6: 人工确认可解释

阅读报告，确认：
- 买入笔数变化方向符合预期（5m 降级增加 + 涨停 fail-closed 减少）。
- 没有异常大的收益跳变（如总收益率变化 >5%）。

### Step 7: Commit 脚本与报告

```bash
git add scripts/run_limit_up_baseline.py scripts/diff_limit_up_baseline.py docs/reports/2026-07-18-limit-up-baseline-diff.md
git commit -m "docs(report): 5m/涨停修复基线 diff 报告与工具脚本"
```

---

## Self-Review Checklist

- [x] **Spec coverage**: 5m 降级（Task 5）、涨停 fail-closed（Task 2-4）、实盘闸（Task 6）、基线 diff（Task 7）均覆盖。
- [x] **Placeholder scan**: 无 TBD/TODO；测试代码均给出具体断言。
- [x] **Type consistency**: `can_buy` 签名、`_is_valid_price`、`is_limit_up` 在全部任务中一致。
- [x] **File paths**: 使用项目真实路径。
- [x] **Frequent commits**: 每个 Task 结束都有 commit。

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-18-5m-signal-window-and-limit-up-filter-plan.md`.**

Two execution options:

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
