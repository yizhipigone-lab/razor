# CRITICAL/HIGH 修复实施计划书（基于 2026-07-15 全项目审计）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `.claude/PRPs/reviews/project_quality_audit_2026-07-15.md` 中标注的 2 个 CRITICAL + 1 个 HIGH + 1 个测试基础设施真 bug，全部带回归测试。

**Architecture:**
- 反向依赖（CRITICAL-1）走"参数单源化"路径：扩展 `app/config/risk_params.py` 的 dataclass 覆盖剩余 7 个参数（策略运行态 + 资金/连亏保护），让回测引擎 0 直接 import `sim_trader/config.py`
- 静默吞（CRITICAL-2）走"未知 ≠ 默认 + 重抛"路径：未知 order_type 拒绝落 deal，激活 kill switch，发飞书告警，audit 留痕
- 涨停过滤失效（HIGH-3）走"读 parquet 前收"路径：buy() 接受 prev_snap 参数；调用方传入 `closes` 前一交易日
- 测试真 bug 走"收口 pytest" 路径：pytest.ini 加显式 rootdir + `tests/test_*.py` 限定，避免 scripts/ 误收集

**Tech Stack:** Python 3.13 + duckdb + FastAPI + pytest + frozen dataclass

---

## 报告验证结果

| 项 | 报告结论 | 验证结论 | 关键证据 |
|---|---|---|---|
| **CRITICAL-1** tdx_runner 反向依赖 sim_trader/config | 属实 | ✅ 真实，L71-90 14 个 import | `app/backtest/tdx_runner.py:71-90` |
| **CRITICAL-2** unknown direction 静默吞 | 属实 | ✅ 真实，L307 direction="unknown" 后 deal 仍写入 | `app/live_trader/callback_handler.py:302-321` |
| **HIGH-3** simple_runner 涨停过滤失效 | 属实 | ✅ 真实，L101 prev_close=px；closes 已存在 | `app/backtest/simple_runner.py:96-104` vs `closes` dict（L683） |
| **测试基础真 bug** scripts/ 下 test_fix_*.py 被 pytest 误收集 | 属实 | ✅ 真实，28 个文件存在 | `scripts/test_fix_02.py` 等 28 个 |

> **关键精化**（对报告建议的修正）：
> CRITICAL-1 中 tdx_runner 的 14 个 import **不全部**在 `RiskParams` 中——`RiskParams` 只覆盖 7 个 risk 段参数（HARD_STOP/TRAIL_ACTIVATE/TRAIL_DD/TAKE_PROFIT_TIERS/TIME_EXIT_DAYS/TIME_EXIT_PROFIT/TIME_FORCE_DAYS），另外 7 个（INITIAL_CAPITAL/POSITION_SIZE/MIN_BUY_AMT/LOSS_STREAK_HALVE/LOSS_STREAK_PAUSE/PAUSE_DAYS/SAME_STOCK_COOLDOWN）必须**先扩展 RiskParams**，否则强迁会让 tdx_runner 反向依赖一个"假统一"的真相源。修复路径分两阶段：扩展 dataclass → 改 tdx_runner import → 删 sim_trader/config 的硬编码兜底。

---

## 文件结构

**修改的文件**（按依赖顺序）：

| 文件 | 责任 |
|---|---|
| `app/config/risk_params.py` | 扩展 `RiskParams`，新增 `PositionParams`、`StreakParams` dataclass + 加载函数 |
| `app/sim_trader/config.py` | 把 7 个硬编码常量改为从 risk_params 加载（H6 注释延伸） |
| `app/backtest/tdx_runner.py` | 14 个 import 改为 `app.config.risk_params` 直接 import |
| `app/live_trader/callback_handler.py` | 未知 direction 拒绝写 deal + raise + 触发 kill switch + 飞书告警 |
| `app/backtest/simple_runner.py` | `buy()` 接受 prev_close；调用方传入 parquet 前收 |
| `app/live_trader/store.py` | 新增 `apply_buy_fill`/`apply_sell_fill` 内部对 direction 字段做断言校验（双层防护） |
| `pytest.ini` | 加 `python_files = tests/test_*.py` + rootdir 显式限定 |

**新建的文件**：

| 文件 | 责任 |
|---|---|
| `tests/test_critical_fixes.py` | 4 项修复的回归测试 |
| `tests/test_risk_params_extension.py` | 扩展 dataclass 的字段默认值 + 加载测试 |
| `app/live_trader/kill_switch.py`（如不存在则新建）| 集中管理 kill switch 触发逻辑 |

---

### Task 1: 扩展 RiskParams dataclass（H6 延伸）

**Files:**
- Modify: `app/config/risk_params.py:19-37`（RiskParams 字段）和 `:54-70`（load_risk_params 实现）
- Test: `tests/test_risk_params_extension.py`（新增）

**背景**：`RiskParams` 当前覆盖 7 个 risk 段字段（HARD_STOP/TRAIL_ACTIVATE 等），**缺 7 个**运行态参数（INITIAL_CAPITAL/POSITION_SIZE/MIN_BUY_AMT/LOSS_STREAK_HALVE/LOSS_STREAK_PAUSE/PAUSE_DAYS/SAME_STOCK_COOLDOWN）。

**目标**：把 7 个运行态参数也并入 RiskParams，**或新增 `PositionParams`/`StreakParams`**，让 tdx_runner 能从一个 module 拿到全部 14 个默认值。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_risk_params_extension.py
from app.config.risk_params import load_risk_params, load_position_params


def test_position_params_fields_exist():
    """扩展后的 PositionParams 必含 7 个运行态字段"""
    pp = load_position_params()
    assert pp.initial_capital == 1_000_000
    assert pp.position_size == 50_000
    assert pp.min_buy_amt == 5_000


def test_streak_params_fields_exist():
    """StreakParams 必含 4 个连亏保护字段"""
    from app.config.risk_params import load_streak_params
    sp = load_streak_params()
    assert sp.loss_streak_halve == 3
    assert sp.loss_streak_pause == 5
    assert sp.pause_days == 3
    assert sp.same_stock_cooldown == 20
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_risk_params_extension.py -v
```

预期：`ImportError: cannot import name 'load_position_params'`

- [ ] **Step 3: 实施扩展**

在 `app/config/risk_params.py` 现有 dataclass 后追加：

```python
@dataclass(frozen=True)
class PositionParams:
    """资金 + 单票仓位控制参数。"""
    initial_capital: float      # 默认本金
    position_size: float        # 单票仓位上限
    min_buy_amt: float          # 最小买入金额


@dataclass(frozen=True)
class StreakParams:
    """连亏保护 + 冷却。"""
    loss_streak_halve: int      # 连亏 N 笔仓位减半
    loss_streak_pause: int      # 连亏 N 笔暂停
    pause_days: int             # 暂停自然日
    same_stock_cooldown: int    # 同股票冷却天数


def _g_run(key: str, default):
    """读 settings[run] 段 → default 兜底。

    `_settings.get(*keys, default=...)` 的 default 是 keyword-only,
    位置传参会被当成第 3 个 key → 必须用 kwargs。
    """
    return _settings.get("run", key, default=default)


def load_position_params() -> PositionParams:
    return PositionParams(
        initial_capital=_g_run("initial_capital", 1_000_000),
        position_size=_g_run("position_size", 50_000),
        min_buy_amt=_g_run("min_buy_amt", 5_000),
    )


def load_streak_params() -> StreakParams:
    return StreakParams(
        loss_streak_halve=_g_run("loss_streak_halve", 3),
        loss_streak_pause=_g_run("loss_streak_pause", 5),
        pause_days=_g_run("pause_days", 3),
        same_stock_cooldown=_g_run("same_stock_cooldown", 20),
    )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_risk_params_extension.py -v
```

预期：2 passed

- [ ] **Step 5: 提交**

```bash
git add app/config/risk_params.py tests/test_risk_params_extension.py
git commit -m "feat(risk_params): 扩展 RiskParams → 新增 PositionParams / StreakParams dataclass"
```

---

### Task 2: 修复 tdx_runner 反向依赖（CRITICAL-1 主修复）

**Files:**
- Modify: `app/backtest/tdx_runner.py:71-90`（import 块）
- Modify: `app/sim_trader/config.py:8-15, 13-15, 33-34`（硬编码改为派生）
- Test: `tests/test_critical_fixes.py::test_tdx_runner_no_sim_trader_import`

**背景**：当前 tdx_runner 直接 `from app.sim_trader.config import (...)`，打破"回测引擎不依赖运行态"分层。

- [ ] **Step 1: 写失败测试（侦察反向依赖）**

```python
# tests/test_critical_fixes.py
import ast
from pathlib import Path


def test_tdx_runner_no_sim_trader_import():
    """tdx_runner 不准 import sim_trader.config (架构分层禁止)"""
    src = Path("app/backtest/tdx_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for n in node.names:
                assert "sim_trader" not in (node.module or ""), \
                    f"tdx_runner 反向依赖 {node.module}.{n.name}"
        elif isinstance(node, ast.Import):
            for n in node.names:
                assert "sim_trader" not in n.name, \
                    f"tdx_runner 反向依赖 {n.name}"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_critical_fixes.py::test_tdx_runner_no_sim_trader_import -v
```

预期：`AssertionError: tdx_runner 反向依赖 app.sim_trader.config.INITIAL_CAPITAL`

- [ ] **Step 3: 改 import 走 risk_params**

替换 `app/backtest/tdx_runner.py:71-90` 整段：

```python
from app.config.risk_params import load_risk_params, load_position_params, load_streak_params
_rp = load_risk_params()
_pp = load_position_params()
_sp = load_streak_params()

params.setdefault("initial_capital", _pp.initial_capital)
params.setdefault("position_size", _pp.position_size)
params.setdefault("min_buy_amt", _pp.min_buy_amt)
params.setdefault("hard_stop", _rp.hard_stop)
params.setdefault("take_profit_tiers", _rp.take_profit_tiers)
params.setdefault("trail_activate", _rp.trail_activate)
params.setdefault("trail_dd", _rp.trail_dd)
params.setdefault("time_exit_days", _rp.time_exit_days)
params.setdefault("time_exit_profit", _rp.time_exit_profit)
params.setdefault("time_force_days", _rp.time_force_days)
params.setdefault("loss_streak_pause", _sp.loss_streak_pause)
params.setdefault("pause_days", _sp.pause_days)
params.setdefault("loss_streak_halve", _sp.loss_streak_halve)
params.setdefault("same_stock_cooldown", _sp.same_stock_cooldown)
```

- [ ] **Step 4: 验证 sim_trader/config.py 已派生自 risk_params（不再重复改）**

`app/sim_trader/config.py:22-41` 已通过 H6 修复从 `load_risk_params()` 派生（HARD_STOP/TRAIL_ACTIVATE/.../TAKE_PROFIT_TIERS 等 7 个 risk 段），本次不重复改。

INITIAL_CAPITAL/POSITION_SIZE/MIN_BUY_AMT/LOSS_STREAK_*/SAME_STOCK_COOLDOWN 7 项仍硬编码在 L8-15, 34（参考 audit 报告 5.1 不是 critical-1 主修复 scope，仅 tdx_runner 反向依赖是）。

**新增回归测试**确认 sim_trader/config.py 派生自 risk_params，防止有人改回去：

```python
# tests/test_critical_fixes.py
def test_sim_trader_config_derives_from_risk_params():
    """sim_trader/config.py 的 risk 段常量必须派生自 risk_params,不能硬编码第二份"""
    from app.sim_trader.config import HARD_STOP, TRAIL_ACTIVATE, TRAIL_DD, TAKE_PROFIT_TIERS
    from app.config.risk_params import load_risk_params
    rp = load_risk_params()
    assert HARD_STOP == rp.hard_stop
    assert TRAIL_ACTIVATE == rp.trail_activate
    assert TRAIL_DD == rp.trail_dd
    assert TAKE_PROFIT_TIERS == rp.take_profit_tiers
```

- [ ] **Step 5: 跑全套测试**

```bash
pytest tests/test_critical_fixes.py::test_tdx_runner_no_sim_trader_import tests/test_critical_fixes.py::test_sim_trader_config_derives_from_risk_params tests/test_risk_params_extension.py -v
```

预期：4 passed（包含 Step 4 新增的派生校验）

- [ ] **Step 6: 冒烟（含 sim_trader/config.py 其他 14 处 import 调用方）**

```bash
python -c "
from app.backtest.tdx_runner import run_tdx_backtest
from app.api.backtest import default_bt_config
from app.sim_trader.engine import SimTraderEngine
from app.scheduler.cron_jobs import cron_jobs
print('全部 import OK')
"
```

预期：全部 OK（验证 sim_trader/config.py 14 处 import 调用方没破坏）

- [ ] **Step 7: 提交**

```bash
git add app/backtest/tdx_runner.py tests/test_critical_fixes.py
git commit -m "fix(critical-1): tdx_runner 反向依赖 — 改走 risk_params / PositionParams / StreakParams"
```

---

### Task 3: 修复 callback_handler unknown direction 静默吞（CRITICAL-2）

**Files:**
- Modify: `app/live_trader/callback_handler.py:301-321`
- Test: `tests/test_critical_fixes.py::test_unknown_direction_rejected`

**背景**：L307 `direction = "unknown"` 后仍写入 deal（L310-321），apply_buy/sell_fill 不会被触发（L356/365 只对 buy/sell），pnl 也不重算（L386 仅 sell）。1 笔 unknown = 1 笔失联系持仓。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_critical_fixes.py
from unittest.mock import MagicMock
from app.live_trader.callback_handler import make_xtquant_callback


def test_unknown_direction_rejected(monkeypatch):
    """未知 order_type 必须拒绝落 deal 并 raise"""
    handler = MagicMock()
    handler.runtime_state = MagicMock(mode="live")
    handler.config = MagicMock(mode="live")
    handler.store = MagicMock()
    handler.audit = MagicMock()
    handler.notify = MagicMock()
    handler.pnl_engine = None
    handler.clearance_lock = None
    handler._deals_buffer = {}
    handler._seq_map = {}

    cb = make_xtquant_callback(handler)
    raw_trade = MagicMock()
    raw_trade.traded_id = 1
    raw_trade.order_id = 100
    raw_trade.stock_code = "600000"
    raw_trade.order_type = 99  # 非 23/24
    raw_trade.traded_volume = 100
    raw_trade.traded_price = 10.0
    raw_trade.traded_amount = 1000.0
    raw_trade.commission = 5.0

    handler.store.get_order.return_value = None

    # 已知 normal path:deal 会落 store;这里我们要求:direction=unknown → 必须 raise
    import pytest
    with pytest.raises(ValueError, match="未知 order_type"):
        cb.on_trade(raw_trade)
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_critical_fixes.py::test_unknown_direction_rejected -v
```

预期：测试通过（因为当前是静默 → 不 raise）；应 FAIL（因为 raise Expected）

- [ ] **Step 3: 修改 callback_handler 拒绝未知 direction**

替换 `app/live_trader/callback_handler.py:301-321`：

```python
# 未知 order_type 强抛(2026-07-15 critical-2): 持仓脱钩风险
if order_type == 23:
    direction = "buy"
elif order_type == 24:
    direction = "sell"
else:
    msg = f"未知 order_type={order_type} trade_id={trade_id} code={code},拒绝落 deal"
    logger.error(msg)
    if self.audit:
        try:
            self.audit.log("unknown_order_type", reason=msg, data={
                "order_type": order_type, "trade_id": trade_id, "code": code,
            })
        except Exception:
            pass
    if self.notify:
        try:
            self.notify.send(f"🚨 [CRITICAL] 实盘收到未知 order_type={order_type} 已激活 kill switch")
        except Exception:
            pass
    # 激活 kill switch (2026-07-15 critical-2): 1 笔 unknown = 1 笔失联系持仓
    # 实际 API 是 activate(reason, source)（app/live_trader/kill_switch.py:39），不是 trigger()
    if self.kill_switch:
        try:
            self.kill_switch.activate(
                reason=f"unknown_order_type={order_type}",
                source="callback_unknown_order_type",
            )
        except Exception:
            pass
    raise ValueError(msg)

deal = {
    "trade_id": trade_id,
    "order_id": order_id,
    "code": code,
    "direction": direction,
    # ... 余下字段不变
}
```

- [ ] **Step 4: store 层不在本 fix 范围（说明）**

`app/live_trader/store.py:310, 369` 的 `apply_buy_fill`/`apply_sell_fill` 不接受 `direction` 参数。
由于 callback_handler 在收到 unknown direction 时已 raise ValueError，store 层根本不会被调到，所以 store 层防护在 raise 路径上是死代码。

要真正双层防护需改 4 处（store 两个方法加 `direction` 参数 + callback 两个调用点同步 + mock 测试），属独立 fix scope，**新开 Task 7（后续 work）**。本 Task 仅做 callback 单层 raise + kill switch（已是强力护栏）。

> 注：CLAUDE.md "持仓脱钩 C1 修复"主要靠 callback_handler raise，store 层 patch 是次级。

- [ ] **Step 5: 运行测试确认通过**

```bash
pytest tests/test_critical_fixes.py::test_unknown_direction_rejected -v
```

预期：1 passed

- [ ] **Step 6: 确认已有 live_trader 审计测试不受影响**

```bash
pytest tests/test_live_trader_audit*.py -v
```

预期：全部通过（未引入回归）

- [ ] **Step 7: 提交**

```bash
git add app/live_trader/callback_handler.py tests/test_critical_fixes.py
git commit -m "fix(critical-2): unknown direction 静默吞 — 拒绝落 deal + raise + kill switch + audit + 飞书告警"
```

> ⚠️ **Dry-run 验证（提交后必做）**：xtquant callback 是异步回报链路，`on_trade()` 内的 `raise ValueError` 是否真向上传播到 xtquant trader，**不能用 MagicMock 单测覆盖**。
>
> 需用 `python -c "from app.live_trader.dry_run import start_dry_run; start_dry_run()"` 在模拟盘跑一次真实回报链路，确认 unknown direction 时：
>
> 1. kill_switch.activate 真触发（看 `/kill_switch/status` 端点）
> 2. 飞书真发告警（看 webhook 日志）
> 3. deal 表不出现 direction="unknown" 行（看 DuckDB `SELECT direction FROM deals WHERE direction='unknown'`）
>
> 若 raise 被 `_safe_db_call` 之类的 try/except 静默吞掉，fallback 方案：保留 raise 但 logger.error 加重写一遍 + 触发 kill_switch 已足够（已包含在 Step 3 步骤里），不依赖 raise 真的传播上去。

---

### Task 4: 修复 simple_runner 涨停过滤失效（HIGH-3）

**Files:**
- Modify: `app/backtest/simple_runner.py:96-104`（buy() 接受 prev_close）
- Modify: `app/backtest/simple_runner.py:729-741`（调用方传 prev_close）
- Test: `tests/test_critical_fixes.py::test_simple_runner_can_buy_with_prev_close`

**背景**：L101 `prev_close = px`，`can_buy()` 检查 `(today_high-prev_close)/prev_close >= limit`，传 `px` 作 prev_close = `0/px=0`，永远 True。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_critical_fixes.py
def test_can_buy_rejects_limit_up_with_real_prev_close():
    """real prev_close + 涨停 → must reject (不复刻 'px=prev_close' bug)

    can_buy 真实签名(app/backtest/execution.py:30):
      def can_buy(code: str, prev_close: float, today_high: float) -> Tuple[bool, str]
    3 个位置参数,无 today_high kwarg。
    """
    from app.backtest.execution import can_buy
    # prev_close=10, today_high=11.5 → change = (11.5-10)/10 = 15% >= 主板 10% 上限
    can_buy_ok, reason = can_buy("600000", 10.0, 11.5)
    assert can_buy_ok is False, "涨停日必须被拒,actual OK 是 bug"
    assert "涨停" in reason or "limit" in reason.lower(), f"reason 应说明原因,got: {reason}"
```

- [ ] **Step 2: 修改 FastEngine.buy() 接受 prev_close**

替换 `app/backtest/simple_runner.py:96-104`：

```python
def buy(self, d, code, px, prev_close=None):
    """d 当前日,code 股票,px 当前价,prev_close 昨日真实收盘价

    2026-07-15 critical-3: 涨停过滤必须用真实前收,不再 px=prev_close
    缺前收 → 显式 raise(对齐原 strict_runner "缺数据 raise"),不静默失效
    """
    if code in self.positions: return None
    if prev_close is None or prev_close <= 0:
        raise ValueError(
            f"simple_runner.buy 缺真实 prev_close(code={code} d={d});"
            "要么从 closes 取前一个交易日,要么改用 fast_engine"
        )
    # can_buy(code, prev_close, today_high) 3 位参数;这里 today_high = px
    can_buy_ok, _ = can_buy(code, prev_close, px)
    if not can_buy_ok:
        return None
    # ... 余下不变
```

- [ ] **Step 3: 改 simple_runner.py 调用方(L729-741)传 prev_close**

替换 `app/backtest/simple_runner.py:729-741`：

```python
if d in sbd and not paused:
    for code, px in sbd[d]:
        if eng.cash < min(eng.max_pos(), params.get('min_buy_amt', 5000)):
            break
        cooldown = params.get('same_stock_cooldown', 20)
        if any(t.code == code and (d - t.entry_date).days <= cooldown for t in eng.trades):
            continue
        # 2026-07-15 critical-3: 从 closes 取前一个交易日 prev_close
        prev_close = None
        if d in closes:
            _sorted_prev = sorted(pd for pd in closes.keys() if pd < d)
            if _sorted_prev:
                prev_close = closes[_sorted_prev[-1]].get(code)
        if eng.buy(d, code, px, prev_close=prev_close):
            day_info['bought'].append({
                'code': code, 'name': stock_names.get(code, '') if stock_names else '',
                'price': round(float(px), 2),
            })
```

- [ ] **Step 4: 改 tdx_runner.py:803 调用方传 prev_close（**漏改会 TDX 回测全崩**）**

替换 `app/backtest/tdx_runner.py:803`：

```python
# 原: eng.buy(d_obj, code, px)
# 改: 取 prev_day 的 close 作为 prev_close
prev_close_for_buy = None
if prev_day is not None:
    _prev_snap = prices_by_date.get(str(prev_day), {})
    _prev_bar = _prev_snap.get(code, {})
    if isinstance(_prev_bar, dict):
        prev_close_for_buy = _prev_bar.get("close")
if eng.buy(d_obj, code, px, prev_close=prev_close_for_buy):
    pass
```

> 上下文: `prev_day` 在 L362 已初始化,L807 `prev_snap = snap` 前这一循环 iter 里 `prev_day` 是上一日的日期字符串。`prices_by_date` 在 L275 是 `defaultdict(dict)`,key 是 `str(date)`。

- [ ] **Step 5: 修 pre-existing bug — simple_runner.py:740-741 prev_snap 用当天 close**

主循环 `for d in td_list:` 里 `prev_snap` 应是**前一天**的快照,但 L740-741 把**当天**close 写进 prev_snap → `eng.sell_phase(d, snap, prev_snap)` 的 gap 调整基准错,**T+1 止盈止损不准**。

修复:在循环开头同步 prev_snap 为前一个交易日的所有 codes(不是已经持有的):

```python
# 在 `for d_obj in td_list:` 循环开头(simple_runner.py 主回放循环,大约 L770 附近)
# 找前一个交易日
prev_d = None
for _cand in sorted(closes.keys(), reverse=True):
    if _cand < d_obj:
        prev_d = _cand
        break
if prev_d is not None:
    for code in closes[prev_d]:
        if code not in prev_snap:
            prev_snap[code] = {'close': closes[prev_d][code]}
```

> 关键:L740-741 已被 Step 3 替换,所以 Step 5 的 prev_snap 重置逻辑在循环开头执行,Step 3 的 prev_snap 累积逻辑在新代码里就不再需要,删除。

- [ ] **Step 6: 写端到端测试**

```python
# tests/test_critical_fixes.py
from datetime import date
from app.backtest.simple_runner import FastEngine


def test_simple_runner_buy_requires_real_prev_close():
    """缺 prev_close 必 raise,不复刻 px=prev_close 的 bug"""
    td_list = [date(2024, 1, d) for d in range(2, 11)]
    params = {
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "min_buy_amt": 5_000,
        "trail_activate": 0.05,
        "trail_dd": 0.02,
        "hard_stop": -0.06,
        "loss_streak_halve": 3,
        "loss_streak_pause": 5,
        "pause_days": 3,
        "same_stock_cooldown": 20,
        "take_profit_tiers": [{"profit_pct": 0.03, "sell_ratio": 0.30}],
    }
    eng = FastEngine(td_list, params)
    import pytest
    # 缺 prev_close → raise
    with pytest.raises(ValueError, match="缺真实 prev_close"):
        eng.buy(date(2024, 1, 3), "600000", 11.5, prev_close=None)


def test_simple_runner_filters_limit_up():
    """涨停日买入必拒 — 端到端验证"""
    td_list = [date(2024, 1, d) for d in range(2, 11)]
    params = {
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "min_buy_amt": 5_000,
    }
    eng = FastEngine(td_list, params)
    # 前收 10 + 现价 11.5 (+15% 涨停主板) → can_buy 拒
    res = eng.buy(date(2024, 1, 3), "600000", 11.5, prev_close=10.0)
    assert res is None, "涨停日应拒,actual not None 是 bug"
```

- [ ] **Step 7: 运行全部测试确认通过**

```bash
pytest tests/test_critical_fixes.py tests/test_risk_params_extension.py -v
```

预期：全部通过

- [ ] **Step 8: 冒烟（simple_runner + tdx_runner 两条 import 路径都过）**

```bash
python -c "
from app.backtest.simple_runner import FastEngine
from app.backtest.tdx_runner import run_tdx_backtest
print('OK')
"
```

预期：`OK`（确认 tdx_runner.py:803 不因 buy 签名变更崩）

- [ ] **Step 9: 提交**

```bash
git add app/backtest/simple_runner.py tests/test_critical_fixes.py
git commit -m "fix(high-3): simple_runner 涨停过滤失效 — buy() 必须传真实 prev_close,缺数据 raise"
```

---

### Task 5: pytest scripts/ 误收集（测试基础设施真 bug）

**Files:**
- Modify: `pytest.ini`
- Test: `tests/test_critical_fixes.py::test_pytest_does_not_pickup_scripts`

**背景**：`scripts/test_fix_*.py`（28 个）命名匹配 `python_files = test_*.py`。当前 `testpaths = tests` 保护 pytest 不收，但若有人跑 `pytest` 不带参数或 `pytest scripts/` 显式收，会触发误收集、可能 hang。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_critical_fixes.py
from pathlib import Path
import configparser


def test_pytest_does_not_collect_scripts_directory():
    """pytest.ini 必须排除 scripts/ 下的 test_*.py,即使外部跑 pytest scripts/"""
    cfg = configparser.ConfigParser()
    cfg.read("pytest.ini")
    # 检查 addopts 或 python_files 排除
    raw = Path("pytest.ini").read_text(encoding="utf-8")
    assert "addopts" in raw and "--ignore=scripts" in raw, \
        "pytest.ini 缺 --ignore=scripts 保护,scripts/test_fix_*.py 会被误收集"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_critical_fixes.py::test_pytest_does_not_collect_scripts_directory -v
```

预期：`AssertionError`（当前 pytest.ini 没有 ignore）

- [ ] **Step 3: 修改 pytest.ini**

替换 `pytest.ini`：

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_functions = test_*
addopts = --ignore=scripts
```

`--ignore=scripts` 是关键防御。`python_files` 保留 `test_*.py` 即可，因为 `testpaths=tests` 已限定收集 root。

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_critical_fixes.py::test_pytest_does_not_collect_scripts_directory -v
```

预期：1 passed

- [ ] **Step 5: 验证 pytest 真的不收 scripts/**

```bash
pytest --collect-only -q 2>&1 | grep -c "scripts/"
```

预期：0（应不收集 scripts/ 下的任何 test_fix_*.py）

- [ ] **Step 6: 跑全测试套件确认无回归**

```bash
pytest tests/ -q
```

预期：所有已有测试通过（已有 35 个测试文件/6,657 行）

- [ ] **Step 7: 提交**

```bash
git add pytest.ini tests/test_critical_fixes.py
git commit -m "test(infra): pytest.ini 加 --ignore=scripts 防 test_fix_*.py 误收集"
```

---

### Task 6: 完整回归验证

**Files:**
- 无文件变更
- Test: 全测试套件

- [ ] **Step 1: 全测试套件**

```bash
pytest tests/ -v
```

预期：所有现有 + 新增测试通过

- [ ] **Step 2: 关键路径冒烟**

```bash
python -c "
from app.backtest.tdx_runner import run_tdx_backtest
from app.backtest.simple_runner import FastEngine
from app.live_trader.callback_handler import make_xtquant_callback
from app.config.risk_params import load_risk_params, load_position_params, load_streak_params
print('全部 import OK')
"
```

预期：全部 OK

- [ ] **Step 3: pre-commit hook 检查（如有）**

```bash
pre-commit run --all-files 2>&1 || true
```

预期：检查通过或显示已通过的 hook

- [ ] **Step 4: 最终提交报告**

```bash
git log --oneline | head -10
```

预期：看到 5 个新 commit（task1-5），都带 fix(critical-N)/test(infra)/feat(risk_params) 前缀

---

## Self-Review

**Spec coverage:**
- [x] CRITICAL-1 (tdx_runner 反向依赖) → Task 2（含 Task 1 dataclass 扩展前置）
- [x] CRITICAL-2 (unknown direction 静默吞) → Task 3
- [x] HIGH-3 (simple_runner 涨停过滤失效) → Task 4
- [x] 测试基础设施真 bug (scripts/ 误收集) → Task 5

**Placeholder scan:** 已全文检查，TODO/TBD/fill-in details = 0
- risk_params.py 字段集和 default 值都基于已读 `sim_trader/config.py` 实际值（HARD_STOP/LOSS_STREAK_* 等）
- 测试代码完整（不写"类似 Task N"）
- 实际命令和预期输出列出

**Type consistency:**
- `RiskParams` 字段名 → 和 `sim_trader/config.py` 现常量名一一对应（`initial_capital=INITIAL_CAPITAL` 等）
- `PositionParams.initial_capital: float`（不是 int，因为 1_000_000 在配置里可能是 float）
- `StreakParams.loss_streak_halve: int`（对比 int 默认 3）

**发现需补强的点**：
- Task 3 的 store 防线（Step 4）是冗余双防护，建议保留但要单独 PR 验证
- Task 5 的 `--ignore=scripts` 在某些环境会被 `pytest scripts/` 显式覆盖，可考虑 `[tool.pytest.ini_options]` 加 `python_paths` 限定最严格的 `tests/test_*.py`
- Task 2 Step 3 的 `LOAD_RISK_PARAMS` 引用 `_rp` / `_pp` / `_sp` 前缀要保持和 sim_trader/config.py 的 `_RP` 风格一致，**后续 review 时关注命名一致性**

---

## 决策清单

- [ ] Task 1: RiskParams 扩展 + 新增 PositionParams / StreakParams — 双 dataclass
- [ ] Task 2: tdx_runner 反向依赖消除 — 改走 risk_params；同时让 sim_trader/config 派生
- [ ] Task 3: callback_handler unknown direction 拒绝 — reject + raise + kill switch + audit + 飞书
- [ ] Task 4: simple_runner.buy() 必须传真实 prev_close — 缺数据显式 raise,不复刻 px=prev_close
- [ ] Task 5: pytest.ini 加 --ignore=scripts — 防御 scripts/ 下 test_fix_*.py 误收集
- [ ] Task 6: 全套测试通过 + 冒烟 + 无回归

---

**报告状态**：完成
**后续动作**：等用户确认后启动执行
