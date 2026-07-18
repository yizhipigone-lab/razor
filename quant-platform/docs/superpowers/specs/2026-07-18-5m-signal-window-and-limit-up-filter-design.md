# 5m 信号窗口与涨停过滤完整性修复 — 设计文档

- **日期**：2026-07-18
- **状态**：设计评审中
- **背景**：在 TDX 回测性能优化实施过程中，顺手发现两个已登记但尚未立项的逻辑缺口：
  1. 5m 数据窗口外的买入信号会被直接跳过（[docs/REPORT-TDX回测性能优化实施_2026-07-18.md:96](docs/REPORT-TDX回测性能优化实施_2026-07-18.md)）。
  2. 涨停过滤在缺数据时降级为 fail-open（放行），且实盘 RiskGate 根本没有买入侧涨停过滤闸。

## 1. 我们想干什么（大白话）

现在回测和实盘里有三个"暗地里使坏"的问题：

1. **有些该买的信号被悄悄跳过了**。回测用 5 分钟 K 线算买入价，但 5 分钟数据窗口只有最近约 120 天。如果回测区间更长，早期那些日子的信号找不到 5 分钟 bar，代码就直接 `continue` 跳过。回测结果因此偏乐观，但报告里看不出来。
2. **涨停过滤在缺数据时会放行**。回测判断"今天涨停没"需要昨收价。如果昨收价拿不到（停牌后复牌、新股刚进池子等），代码就"不知道就当没涨停"，直接放行买入。
3. **实盘根本没有"涨停不买"这道闸**。实盘 RiskGate 现有若干风控闸门，但独独没有"涨停封板拒买"，买入全靠券商事后拒单兜底。

本项目要把这三个问题一次性修好。

## 2. 目标

- 让回测结果不再因为"静默跳过"和"涨停放行"两个偏差而偏乐观。
- 让实盘在买入前就能拦住涨停股，不再依赖券商事后拒单。
- 让回测和实盘的涨停判断口径对齐、可维护。

## 3. 范围

| 缺口 | 位置 | 修复目标 |
|---|---|---|
| A | [app/backtest/tdx_runner.py:463-464](app/backtest/tdx_runner.py#L463-L464) | 5m bar 缺失的信号日从「静默跳过」改为「降级日线 close 买入」，并加计数/告警。 |
| B-1 | [app/backtest/execution.py:41-42](app/backtest/execution.py#L41-L42)、[simple_runner.py:101-122](app/backtest/simple_runner.py#L101-L122) | 回测涨停过滤从 fail-open（缺数据放行）改为 fail-closed（缺数据禁买）。 |
| B-2 | [app/live_trader/risk_gate.py:38-168](app/live_trader/risk_gate.py#L38-L168) | 实盘 RiskGate 新增一道「涨停封板拒买」闸门。 |

**不在本次范围**：
- 不放宽 5m 数据窗口的既有约束（默认 120 天、单股 20000 根、全局 5000 万根上限）。这些约束是数据/性能层面的，单独评估成本更高。
- 不改其他风控闸的行为（如仓位上限、日亏熔断等）。
- 不动前端展示层（前端若需要展示 fallback 计数，可在后续迭代）。

## 4. 设计原则

1. **Fail-closed 优先**：数据缺失时按最坏情况处理。涨停判断缺昨收/最高价 → 禁买；5m 没 bar 但日线有 → 降级日线买入；日线也没有 → 跳过。
2. **能降级就降级**：5m 没数据时，有更粗的日线数据可用，就用日线 close 买，而不是直接跳过。
3. **口径统一**：主板 10%、创业板/科创板 20%、北交所 30%；统一触发容差 0.995。
4. **一次可控改完**：`can_buy` 加 `strict=True/False` 参数，默认 fail-closed；旧调用点逐步迁移，避免未覆盖路径被意外改变。

## 5. 详细设计

### 5.1 缺口 A：5m 窗口外信号降级日线买入

**修改位置**：[app/backtest/tdx_runner.py:460-471](app/backtest/tdx_runner.py#L460-L471)

**当前逻辑**：
- 如果 `code in stocks_with_intraday`，走 5m 分支。
- 如果当天没有 5m bar（`bar_for_code is None`），直接 `continue` 跳过。

**新逻辑**：
- 如果 `code in stocks_with_intraday` 且当天有 5m bar，仍用 5m bar 的 close。
- 如果 `code in stocks_with_intraday` 但当天没有 5m bar，**降级到日线路径**，从 `prices_by_date[d_str][code].close` 取价。
- 如果日线路径也没有有效 close，才跳过，并记录 warning。
- 新增计数 `intraday_window_fallback_count`，最终透传给回测结果摘要。

**伪代码**：

```python
if code in stocks_with_intraday:
    bar_for_code = first_bar_of_day.get((code, d_str))
    if bar_for_code is not None:
        px = bar_for_code["close"]
    else:
        # 降级：用日线 close
        px = prices_by_date.get(d_str, {}).get(code, {}).get("close")
        if not _is_valid_price(px):  # None/NaN/<=0 都 fail-closed
            skipped_count += 1
            logger.warning(
                "5m bar 缺失且日线无有效 close，跳过买入: code=%s date=%s", code, d_str
            )
            continue
        fallback_count += 1
        logger.info(
            "5m bar 缺失，降级日线 close 买入: code=%s date=%s px=%s", code, d_str, px
        )
else:
    # 原日线路径不变
    day_price = prices_by_date.get(d_str, {}).get(code)
    if day_price is None:
        continue
    px = day_price["close"]
    if not _is_valid_price(px):  # None/NaN/<=0 都 fail-closed
        logger.warning("日线 close 无效，跳过买入: code=%s date=%s", code, d_str)
        continue
```

**边界**：
- 5m bar 有但 `close <= 0`：按异常数据处理，不降级到日线，走原有错误处理。
- 日线 close 缺失/NaN/<=0/None：跳过并 warning。所有价格有效性判断复用 `_is_valid_price(x) = (x is not None and x > 0)`，同时防 None 和 NaN。
- `fallback_count` 在结果摘要里以 `intraday_window_fallback_count` 透出，方便 diff 分析。

### 5.2 缺口 B-1：回测涨停过滤 fail-closed

**修改位置**：
- 核心函数：[app/backtest/execution.py:30-47](app/backtest/execution.py#L30-L47)
- 调用点：[simple_runner.py:101-122](app/backtest/simple_runner.py#L101-L122)
- 调用点：[tdx_runner.py:475-485](app/backtest/tdx_runner.py#L475-L485)（日内）
- 调用点：[tdx_runner.py:877-885](app/backtest/tdx_runner.py#L877-L885)（日线）
- 测试契约：[tests/test_critical_fixes.py:35-58](tests/test_critical_fixes.py#L35-L58)

#### 5.2.1 `can_buy` 新接口

```python
def can_buy(
    code: str,
    prev_close: float,
    today_high: float,
    strict: bool = True,
) -> Tuple[bool, str]:
    """判断某只股票当前是否允许买入。

    Args:
        code: 股票代码
        prev_close: 昨收价（涨停判断基准）
        today_high: 今日最高价或当前拟成交价
        strict: 是否 fail-closed。True 时缺数据禁买；False 时保持旧行为兼容。

    Returns:
        (是否可买, reason)
    """
    if not (_is_valid_price(prev_close) and _is_valid_price(today_high)):
        if strict:
            return False, "missing_price_data"
        return True, "missing_price_data_ok"

    change = (today_high - prev_close) / prev_close
    limit = get_limit_up_pct(code)
    if change >= limit * 0.995:
        return False, f"limit_up({change * 100:.1f}%)"
    return True, "OK"
```

#### 5.2.2 `get_limit_up_pct` 下沉到共享模块

- 当前在 [execution.py:21-27](app/backtest/execution.py#L21-L27)。
- 移动到新建的 [app/utils/limit_up.py](app/utils/limit_up.py)，供 backtest 和 live_trader 共用。
- 规则不变：300/301/688 开头 → 20%；8/4 开头 → 30%；其他 → 10%。

#### 5.2.3 调用点迁移

**simple_runner.buy**（[simple_runner.py:101-122](app/backtest/simple_runner.py#L101-L122)）：

- `buy()` 保持原有签名以兼容 scripts/ 下 22 个旧三参 CLI（`eng.buy(d, code, px)`）。
- 内部行为：
  - 如果调用方传了真实 `prev_close`（> 0）：走 `can_buy(code, prev_close, px, strict=True)`，fail-closed。
  - 如果 `prev_close` 缺失/无效：走 `can_buy(code, 0, px, strict=False)` 保持旧行为（放行），同时加 `DeprecationWarning` 和 `logger.warning`，提示调用方迁移到四参调用。
- 这样旧脚本不会被一次性全部拒买，同时新代码有明确的迁移压力。后续版本再移除 `strict=False` 兼容分支。
- 正常新调用：`can_buy(code, prev_close, px, strict=True)`。

**tdx_runner 日内路径**（[tdx_runner.py:475-485](app/backtest/tdx_runner.py#L475-L485)）：
- 当前：`prev_close = 0` fallback，导致 `can_buy` 直接放行。
- 新逻辑：
  1. 优先从 `prices_by_date[prev_day][code].close` 取前收。
  2. 如果 snap 里没有，调用新增的 `get_prev_close_from_parquet(code, trade_date)` 从 `data/parquet/daily/{code}.parquet` 取前一交易日 close。
  3. 还拿不到 → `prev_close=0` + `strict=True` → 拒买。

**tdx_runner 日线路径**（[tdx_runner.py:877-885](app/backtest/tdx_runner.py#L877-L885)）：
- 同样从 `prices_by_date` 取前收，缺失时回退 parquet。
- 拿不到 → 拒买。

#### 5.2.4 新增 parquet 前收读取辅助函数

```python
from functools import lru_cache


@lru_cache(maxsize=128)
def _load_daily_parquet(code: str):
    """缓存读取 code 的 parquet 日线文件，避免热循环反复 IO。

    返回的 DataFrame 被所有调用方共享，调用方只读；需要修改时先 `.copy()`。
    """
    path = f"data/parquet/daily/{code}.parquet"
    # 返回 DataFrame；读取失败返回 None
    ...


def get_prev_close_from_parquet(code: str, trade_date: str) -> Optional[float]:
    """从 code 的 parquet 日线文件中，读取 trade_date 前一交易日的 close。

    实现放在 app/backtest/tdx_runner.py 内（仅限回测使用）。
    通过 _load_daily_parquet 缓存按 code 整份缓存，减少热循环中的重复 IO。

    Returns:
        前一交易日 close；拿不到返回 None（strict=True 时会拒买）。
    """
    df = _load_daily_parquet(code)
    if df is None or df.empty:
        return None
    # 找到 trade_date 前最近一个交易日的 close
    ...
```

#### 5.2.5 测试契约变更

- [tests/test_critical_fixes.py:35-58](tests/test_critical_fixes.py#L35-L58) 当前把 "缺 prev_close 时放行" 钉成预期行为。
- 需要新增/调整：
  - 四参调用（传真实 prev_close）缺数据时返回 `(False, "missing_price_data")` 并 warning。
  - 旧三参调用 `eng.buy(d, code, px)` 仍保持放行，但加 deprecation warning，作为迁移期契约。
- [tests/test_execution.py](tests/test_execution.py) 增加 `strict=True/False` 覆盖。

### 5.3 缺口 B-2：实盘 RiskGate 新增涨停拒买闸

**位置**：[app/live_trader/risk_gate.py:38-168](app/live_trader/risk_gate.py#L38-L168)

**闸门编号**：**5c**。放在 5b（单笔浮亏）之后、闸门 7（连续拒绝）之前，同属风险/仓位类。

**触发时机**：买入 intent 通过 5b 闸门后、进入闸门 7 的连续拒绝统计前。

#### 5.3.1 行情来源与延迟

`order_executor.py:125` 调用 `RiskGate.check()` 时已经传入一个 `quote` dict（来自 QMT wrapper，含 `lastPrice`、`lastClose`、`high` 等）。新闸优先复用该 `quote`，只在缺失/无效时才调 `quote_source.get_realtime_quotes([code])` 降级。

- **优先**：`quote["lastClose"]`（昨收）+ `quote["lastPrice"]`（现价）。
- **降级**：`quote_source.get_realtime_quotes([code])` 返回的 DataFrame 列 `last_close` + `price`。
- **注意**：`quote_source` 的第一源 QmtHttpAdapter 会打 `localhost:8001` HTTP，而 live_trader 自己就是 8001 服务端。如果优先复用已传入的 `quote`，可以避免下单热路径上的自调回环和四级降级超时叠加。

#### 5.3.2 判断逻辑

1. 取 `prev_close` 和 `price`：
   - 优先从 `check()` 传入的 `quote` 取 `lastClose`/`lastPrice`。
   - 任一字段无效时，降级到 `quote_source.get_realtime_quotes([code])` 的 `last_close`/`price`。
2. 数据源走项目统一 4 源优先级：QMT → TDX → 腾讯 → Parquet（[CLAUDE.md](CLAUDE.md)）。
3. 判断（None/NaN 安全）：
   - `not _is_valid_price(prev_close)` 或 `not _is_valid_price(price)` → **fail-closed，拒买**，reason `missing_price_data`。
   - `change = (price - prev_close) / prev_close`
   - `change >= get_limit_up_pct(code) * 0.995` → **涨停，拒买**，reason `limit_up`。

#### 5.3.3 为什么用现价而不是最高价

- 实盘买入是"现在下单"，应判断**当前是否涨停**。
- `high` 只代表"盘中曾经涨停"，涨停打开后仍应允许买入。
- 回测用 `today_high` 是因为回测买入价是"当日某个时刻的价格"，用最高价判断是否触板更保守；实盘场景不同，口径允许合理差异。

#### 5.3.4 拒买记录与连续拒绝

- 新闸拒绝归为 **`market` 类**（市场状态导致，非风控异常），只记 audit、**不计入闸门 7 的连续拒绝计数**，避免行情火热日大量信号撞涨停时误触发 kill_switch。
- 保留 rejection reason 供前端/日志查看。

#### 5.3.5 配置开关

- 开关载体：`LiveTraderConfig.limit_up_gate_enabled`（settings 读取，默认 `true`）。
- 实现上优先走 config.py 唯一真相源链路；如需前端热切换，可再挂 runtime_state。
- 紧急情况下设为 `false` 可关闭该闸，保留其他风控闸。

### 5.4 共享模块设计

新建 [app/utils/limit_up.py](app/utils/limit_up.py)：

```python
from typing import Tuple


def get_limit_up_pct(code: str) -> float:
    """返回 code 对应的涨停幅度。"""
    if code.startswith(("300", "301", "688")):
        return 0.20
    if code.startswith(("8", "4")):
        return 0.30
    return 0.10


def _is_valid_price(x) -> bool:
    """价格有效性检查：非 None、非 NaN、大于 0。

    注意：NaN > 0 为 False；None 被 `is not None` 短路。
    """
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

- `execution.can_buy`、`tdx_runner` 5m 降级、RiskGate 均复用 `_is_valid_price`。
- `execution.can_buy` 调用 `is_limit_up`：若涨停则不可买。
- RiskGate 调用 `is_limit_up`：若涨停则拒买。
- 这样涨停判断的核心逻辑只有一份，避免 backtest 和 live_trader 口径漂移。

## 6. 接口变更

| 接口 | 变更前 | 变更后 |
|---|---|---|
| `execution.can_buy` | `can_buy(code, prev_close, today_high)` | `can_buy(code, prev_close, today_high, strict=True)` |
| `execution.get_limit_up_pct` | 在 execution.py 内 | 移动到 `app/utils/limit_up.py` |
| `tdx_runner._run_intraday_backtest` | 5m bar 缺失 → continue | 5m bar 缺失 → 降级日线 close |
| `RiskGate` | 现有闸门 | 新增 5c 涨停拒买闸 |
| 回测结果摘要 | 无 fallback 计数 | 新增 `intraday_window_fallback_count` |

## 7. 数据流

### 7.1 回测买入价数据流

```
signal date (d_str)
  ↓
if code in stocks_with_intraday:
    first_bar_of_day.get((code, d_str))
      ├─ 命中 → px = bar["close"]                 (5m 成交价)
      └─ 缺失 → prices_by_date[d_str][code].close  (降级日线 close)
                  ├─ 命中 → px = day_close
                  └─ 缺失 → skip + warning
else:
    prices_by_date[d_str][code].close              (日线 close)
      ├─ 命中 → px = day_close
      └─ 缺失 → skip
```

### 7.2 涨停判断数据流

**回测**：
```
prev_close
  ├─ 来自 prices_by_date[prev_day][code].close
  ├─ 回退 parquet daily
  └─ 缺失 → strict=True → 拒买
today_high
  └─ 买入价 px（5m close 或日线 close）
can_buy(code, prev_close, today_high, strict=True)
```

**实盘**：
```
RiskGate.check() 已传入 quote
  ↓
quote["lastClose"] + quote["lastPrice"]（优先）
  ↓ 任一无效时降级到
quote_source.get_realtime_quotes([code])
  ↓
last_close + price
  ↓
is_limit_up(code, prev_close, price, strict=True)
  ↓
True → RiskGate 拒买（market 类 audit，不计入连续拒绝）
False → 通过
```

## 8. 测试计划

| 测试 | 内容 | 预期 |
|---|---|---|
| `tests/test_limit_up.py`（新增） | 板块识别、容差、NaN 输入、缺数据 strict/非 strict | 全部通过 |
| `tests/test_execution.py` | 更新 limit_up 测试，覆盖 strict 参数、NaN 输入 | 全部通过 |
| `tests/test_critical_fixes.py` | 四参调用缺 prev_close 时拒买；旧三参调用仍放行但加 deprecation warning | 全部通过 |
| 新增 5m fallback 集成测试 | `bar_for_code is None` 时降级日线 close | 通过 |
| `tests/test_live_trader_smoke.py` | RiskGate 新闸拒绝涨停股；开关 `false` 时放行 | 通过 |
| 新增 RiskGate 连续拒绝测试 | market 类涨停拒买不计入闸门 7 连续拒绝 | 通过 |
| 基线 diff | 跑 QUANTQQ 或典型公式，对比改前/改后 | 买入笔数、收益、回撤变化可解释 |

## 9. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| 回测数字变化影响策略评估 | 高 | 改前跑基线；diff 超阈值时先 review 再合并；fallback 计数单独列出 |
| 实盘新闸误杀正常票（tick 跳涨） | 中 | 容差 0.995 留缓冲；新闸加 warning + 计数，观察后再强制；开关可关闭 |
| NaN 数据导致 fail-closed 失效 | 高 | 所有价格有效性判断统一用 `_is_valid_price(x) = (x is not None and x > 0)`，并加 NaN/None 专项测试 |
| 实盘新闸自调 HTTP 回环延迟 | 中 | 优先复用 `RiskGate.check()` 已传入的 `quote`，只在缺失时走 quote_source 降级 |
| parquet 热循环读 IO 放大 | 中 | `_load_daily_parquet` 按 code 整份缓存，避免每次 miss 都重新读文件 |
| `test_critical_fixes.py` 契约冲突 | 中 | 预期内，同步更新测试；旧三参调用保留兼容分支 |
| 5m 降级后买入价口径变化 | 中 | 结果摘要加 `intraday_window_fallback_count`，diff 报告中单独列出 |
| 涨停拒买误触发 kill_switch | 中 | 新闸拒绝归为 `market` 类，只 audit 不计入连续拒绝计数 |
| parquet 前收读取失败 | 低 | 拿不到就 fail-closed 拒买，不会错误放行 |

**回滚开关**：
- 回测：`can_buy(..., strict=False)` 可快速切回旧行为。
- 实盘：`LiveTraderConfig.limit_up_gate_enabled=false`（或对应 runtime_state）可关闭涨停闸。

## 10. 实现顺序

1. **新建共享模块** `app/utils/limit_up.py` + 单元测试。
2. **改造回测涨停过滤**：
   - `execution.can_buy` 加 `strict` 参数。
   - `execution` 调用 `app/utils/limit_up.is_limit_up`。
   - 迁移 `simple_runner`、`tdx_runner` 调用点。
   - 新增 `get_prev_close_from_parquet` 辅助函数。
   - 更新 `tests/test_execution.py`、`tests/test_critical_fixes.py`。
3. **改造 5m 降级日线买入**：
   - 修改 `tdx_runner.py:460-471`。
   - 新增 `intraday_window_fallback_count` 计数与透出。
   - 新增集成测试。
4. **新增实盘涨停闸**：
   - `RiskGate` 加 5c 闸（5b 之后、闸门 7 之前）。
   - 优先复用 `check()` 已传入的 `quote`；缺失时调 `quote_source` 和 `app/utils/limit_up.is_limit_up`。
   - 新闸拒绝归为 `market` 类，不计入连续拒绝。
   - 加配置开关 `LiveTraderConfig.limit_up_gate_enabled`。
   - 更新 `tests/test_live_trader_smoke.py`。
5. **跑基线 diff 与冒烟测试**：
   - 跑典型公式/QUANTQQ。
   - 生成 diff 报告，确认变化可解释。

## 11. 验收标准

- [ ] 缺 `prev_close`/`today_high`（含 NaN）时，`can_buy` 返回 `(False, "missing_price_data")`。
- [ ] 5m 窗口外信号能降级到日线 close 买入，结果摘要里有 `intraday_window_fallback_count`。
- [ ] 实盘 RiskGate 能拒绝涨停股买入；开关 `false` 时放行。
- [ ] 涨停拒买记录为 `market` 类，不计入连续拒绝计数。
- [ ] `app/utils/limit_up.py` 单元测试覆盖主板/双创/北证、容差、NaN、缺数据。
- [ ] 旧三参 `eng.buy(d, code, px)` 仍保持放行并加 deprecation warning。
- [ ] 所有相关测试通过。
- [ ] 基线 diff 报告记录买入笔数、收益、回撤变化，变化可解释。

## 12. 相关文档与代码

- 审计登记：[docs/AUDIT-回测引擎层性能优化_2026-07-16.md:63,89](docs/AUDIT-回测引擎层性能优化_2026-07-16.md)
- 报告登记：[docs/REPORT-回测引擎层性能优化_2026-07-16.md:97-100](docs/REPORT-回测引擎层性能优化_2026-07-16.md)
- 报告登记：[docs/REPORT-TDX回测性能优化实施_2026-07-18.md:96](docs/REPORT-TDX回测性能优化实施_2026-07-18.md)
- 关键代码：[app/backtest/execution.py:30-47](app/backtest/execution.py#L30-L47)、[app/backtest/tdx_runner.py:460-471](app/backtest/tdx_runner.py#L460-L471)、[app/live_trader/risk_gate.py:38-168](app/live_trader/risk_gate.py#L38-L168)
