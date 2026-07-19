# ExitPolicy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把分散在 9 个 seam 的退出策略"形状"收口到一个深 module `ExitPolicy`，让规则求值有单一真相，并让实盘 backtest↔live 的叠加栈分歧从"静默丢栈"升级为"显式记录的已知缺口"。

**Architecture:** 新建 `app/backtest/exit_policy.py`，**组合**现有 `exit_rule_engine`（不替换、不动 2026-07-16 刚优化的 `precompute_params`）。interface 三件套：`evaluate(ctx)`（规范有序栈，唯一真相 = 现 `check_all`）、`top(ctx)`（栈顶，行为等价现 `check`）、`preview(ctx)`（UI 同源距离，Phase 2）。in-process 纯计算，无 port/adapter；replace-don't-layer 测试。

**Tech Stack:** Python 3，dataclass（`ExitSignal`/`RuleContext` 已存在），pytest，绞杀者迁移 + TDD（RED→GREEN→迁→重构）。

**关键约束（实盘真金白银）：**
- `evaluate` 必须零行为差异地等于 `exit_rule_engine.check_all`；`top` 必须零行为差异地等于 `exit_rule_engine.check`。靠"纯委托"保证，不重写栈逻辑。
- **⚠️ 接口契约（caller 必知，审计 C1/C2 根因）**：`rule_take_profit` 会**原地改 `ctx.triggered_tiers`**（`exit_rules.py:191-192`）。所以 `evaluate`/`top`/`check_all`/`check` 每次调用都必须拿到**全新 ctx**，不得在同一 ctx 上连调（否则触发态串味）。现状所有生产 caller 每次 `build_context` 都新建 ctx，符合；**测试必须每个断言现场 `_ctx(**kw)`，绝不复用同一对象**。
- 每改一个调用方立刻 `py_compile` + 跑相关测试（CLAUDE.md 质量门禁）。
- 严禁批量替换脚本（CLAUDE.md：曾把 `var(--orange)` 改成 `#ffa657`）——逐个 Edit。
- 实盘 `exit_monitor` 迁移后必须加"只吃 top()"显式缺口注释，指向候选②-延展。

**设计决策来源：** CONTEXT.md 候选②（2026-07-19 grilling，Q1=B 统一定义+单信号派生，Q2=A 核心优先）。**本版 v2 已合入 code-reviewer 审计发现（见文末 Audit iteration）。**

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `app/backtest/exit_policy.py` | ExitPolicy 深 module：evaluate/top（+ Phase 2 preview） | 新建 |
| `tests/test_exit_policy.py` | interface 测试（含 keystone：top==evaluate[0]；preview.fired==evaluate） | 新建 |
| `app/backtest/simple_runner.py:166` | 唯一 `check_all` 调用方 → `evaluate` | 改 1 行 |
| `app/backtest/simple_runner.py:433,452` | `check` → `top`（intraday ×2） | 改 2 行 |
| `app/backtest/tdx_runner.py:266,604` | `check` → `top`（daily/5m ×2） | 改 2 行 |
| `app/backtest/simulate_one_trade.py:191` | `check` → `top` | 改 1 行 |
| `app/sim_trader/engine.py:460` | `check` → `top` | 改 1 行 |
| `app/sim_trader/intraday_monitor.py:179` | `check(skip_eod_only=True)` → `top(skip_eod_only=True)` | 改 1 行 |
| `app/live_trader/exit_monitor.py:67-68` | `check(skip_eod_only=True)` → `top(skip_eod_only=True)` + 显式缺口注释 | 改 + 注释 |
| `app/live_trader/main.py:1505-1744` | risk-status 路由 → `preview`（Phase 2） | Phase 2 改 |

**不在本计划范围（CONTEXT.md 待办 / 后续候选）：** lot 取整收口（LotRounder）、build_context/hold_days 统一、双 priority map 统一、实盘真正吃栈（候选②-延展）。

---

# Phase 1 — 核心求值收口（clean win，behavior-equivalent）

Phase 1 结束时：ExitPolicy 上线、9 个调用方全迁、测试绿、回测/实盘求值行为零变化。可独立交付。

## Task 1: 建 ExitPolicy 骨架（evaluate + top + skip_eod_only + 单例）

**Files:**
- Create: `app/backtest/exit_policy.py`
- Test: `tests/test_exit_policy.py`

- [ ] **Step 1: 先写失败测试（RED）**

Create `tests/test_exit_policy.py`:

```python
"""ExitPolicy interface 测试。in-process，直接测，无 adapter。

keystone（Q1=B）：top 是 evaluate 的栈顶派生 —— 单一真相。

⚠️ 关键（审计 C1/C2 根因）：rule_take_profit 原地改 ctx.triggered_tiers
（exit_rules.py:191-192）。所以每次调用 evaluate/top/check 都必须用**全新 ctx**，
否则触发态串味。本文件所有断言都用 _ctx(**kw) 现场构造，绝不复用同一对象。
"""
import pytest

from app.backtest.exit_rules import RuleContext, exit_rule_engine
from app.backtest.exit_policy import ExitPolicy, exit_policy


def _ctx(**kw) -> RuleContext:
    """构造一次性 RuleContext。默认值会触发 TP1+TR（盈利够、回撤够）；
    想要"无信号"基线用 _ctx(close=10.1, low=10.15)。"""
    base = dict(
        entry_price=10.0, peak_price=10.5, shares=1000,
        open=10.2, high=10.6, low=10.1, close=10.4,
        hold_days=5, first_day_hold_value=2,
        hard_stop=-0.06, take_profit_tiers=[{"profit_pct": 0.03, "sell_ratio": 0.3}],
        trail_activate=0.05, trail_dd=0.02, use_high_for_tp=True,
        priority_mode="trailing_first", tp_stack_mode=True, tp1_fill_pct=0.03,
    )
    return RuleContext(**{**base, **kw})


@pytest.fixture
def policy() -> ExitPolicy:
    return ExitPolicy(exit_rule_engine)


# 场景：no_signal 无任何触发 / hs 硬止损 / tp_stack TP1 触发
SCENARIOS = [
    ("no_signal", dict(close=10.1, low=10.15, high=10.2)),
    ("hs",        dict(low=9.3, close=9.4)),
    ("tp_stack",  dict(high=11.0, close=10.7, low=10.65, peak_price=11.0)),
]


# --- evaluate：规范栈，唯一真相 = check_all（两侧各用全新 ctx） ---

@pytest.mark.parametrize("name,kw", SCENARIOS)
def test_evaluate_equals_check_all(policy, name, kw):
    assert policy.evaluate(_ctx(**kw)) == exit_rule_engine.check_all(_ctx(**kw))


# --- top：栈顶，行为等价 check（两侧各用全新 ctx） ---

@pytest.mark.parametrize("name,kw", SCENARIOS)
def test_top_equals_check(policy, name, kw):
    assert policy.top(_ctx(**kw)) == exit_rule_engine.check(_ctx(**kw))

def test_top_skip_eod_only_equals_check(policy):
    kw = dict(close=10.1, low=10.15, high=10.2)
    assert policy.top(_ctx(**kw), skip_eod_only=True) == \
           exit_rule_engine.check(_ctx(**kw), skip_eod_only=True)


# --- keystone：top == evaluate[0]（单一真相；两侧各用全新 ctx） ---

@pytest.mark.parametrize("name,kw", SCENARIOS)
def test_top_is_evaluate_head(policy, name, kw):
    stack = policy.evaluate(_ctx(**kw))
    head = stack[0] if stack else None
    assert policy.top(_ctx(**kw)) == head, (
        f"top 必须等于 evaluate 栈顶（单一真相）。场景={name}: "
        f"top={policy.top(_ctx(**kw))} head={head}"
    )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_exit_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.backtest.exit_policy'`

- [ ] **Step 3: 写最小实现（GREEN）**

Create `app/backtest/exit_policy.py`:

```python
"""ExitPolicy — 退出策略"形状"的深 module（CONTEXT.md 候选②）。

组合 exit_rule_engine（不替换、不动其 precompute_params 优化）。把"求值形状"锁成单一真相：
  evaluate(ctx) -> list[ExitSignal]   规范有序栈（唯一真相 = exit_rule_engine.check_all）
  top(ctx)      -> Optional[ExitSignal]  栈顶（单信号派生 = exit_rule_engine.check，行为等价）
  preview(ctx)  -> dict   UI 同源距离（Phase 2，见文末待办）

in-process 纯计算（bar + 持仓 + params → 信号），interface 处无 port/adapter。
测试姿态：replace, don't layer（引擎层原 ~0 测试，新测试直接写在本 interface 上）。

⚠️ 接口契约（caller 必知）：evaluate 经 check_all→rule_take_profit 会原地改
ctx.triggered_tiers（exit_rules.py:191-192）。调用方必须传一次性 ctx，不得在
evaluate/top 之间复用同一 ctx。现状所有生产 caller 每次 build_context 都新建，符合。

显式缺口（CONTEXT.md 候选②）：实盘/盘中 scan 仍只调 top()（最高优先级单信号），
不处理同 bar 多信号叠加。让实盘真正吃栈留作候选②-延展。
"""
from typing import List, Optional

from app.backtest.exit_rules import exit_rule_engine, RuleContext, ExitSignal


class ExitPolicy:
    """退出策略深 module。组合一个 ExitRuleEngine，锁求值形状。"""

    def __init__(self, engine=exit_rule_engine):
        self._engine = engine

    def evaluate(self, ctx: RuleContext) -> List[ExitSignal]:
        """规范有序栈（唯一真相）。语义 = exit_rule_engine.check_all：
        trailing_first 下 TP1 部分卖在前、TR/HS 全卖在后，按 remaining 预算。"""
        return self._engine.check_all(ctx)

    def top(self, ctx: RuleContext, skip_eod_only: bool = False) -> Optional[ExitSignal]:
        """栈顶信号（单信号调用方用）。语义 = exit_rule_engine.check，行为等价。
        透传 skip_eod_only（盘前盘中跳过"仅尾盘"规则，如 FD；live/intraday 调用方需要）。
        单一真相约束：top(ctx) == (evaluate(ctx)[0] if evaluate(ctx) else None)；
        等价性由 tests/test_exit_policy.py::test_top_is_evaluate_head 守。
        实现走 check 短路（不必算完整栈）。"""
        return self._engine.check(ctx, skip_eod_only=skip_eod_only)

    # Phase 2（见本计划 Phase 2 Task 6）：
    # def preview(self, ctx: RuleContext) -> dict: ...


# 模块级单例（与 exit_rule_engine 单例对称；调用方 import 这个）
exit_policy = ExitPolicy()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_exit_policy.py -v`
Expected: PASS — `test_evaluate_equals_check_all`、`test_top_equals_check`、`test_top_skip_eod_only_equals_check`、`test_top_is_evaluate_head` 各 3 个 parametrize 场景全过。

- [ ] **Step 5: py_compile 语法检查**

Run: `python -m py_compile app/backtest/exit_policy.py`
Expected: 无输出（成功）

- [ ] **Step 6: Commit**

```bash
git add app/backtest/exit_policy.py tests/test_exit_policy.py
git commit -m "feat(backtest): 新增 ExitPolicy 深 module（evaluate/top）+ interface 测试

CONTEXT.md 候选②。组合 exit_rule_engine 不替换。evaluate=check_all（规范栈，
唯一真相），top=check（栈顶，行为等价，透传 skip_eod_only）。keystone：top==evaluate[0]。
测试每个断言用全新 ctx（规避 rule_take_profit 原地改 triggered_tiers）。"
```

---

## Task 2: 迁移唯一 check_all 调用方（simple_runner:166 → evaluate）

**Files:**
- Modify: `app/backtest/simple_runner.py` (line 166 附近)

- [ ] **Step 1: 读现状确认行号与上下文**

Run: `python -c "s=open('app/backtest/simple_runner.py',encoding='utf-8').read().splitlines(); [print(i+1, s[i]) for i in range(163,170)]"`
Expected: 看到 line 166 是 `for signal in exit_rule_engine.check_all(ctx):`（确认未漂移；若漂移用 Grep `check_all` 重定位）。

- [ ] **Step 2: 改调用**

Edit `app/backtest/simple_runner.py`，把：

```python
            for signal in exit_rule_engine.check_all(ctx):
```

改为：

```python
            for signal in exit_policy.evaluate(ctx):
```

并在文件顶部 import 区加：

```python
from app.backtest.exit_policy import exit_policy
```

> 注：若 `exit_rule_engine` 在本文件已无其他引用，**不要**顺手删（YAGNI，留到 Phase 1 末尾 Task 5 统一清理）。

- [ ] **Step 3: py_compile + 跑测试**

Run:
```bash
python -m py_compile app/backtest/simple_runner.py
python -m pytest tests/test_exit_policy.py -v
```
Expected: 编译成功；ExitPolicy 测试仍全过（行为零变化）。

- [ ] **Step 4: Commit**

```bash
git add app/backtest/simple_runner.py
git commit -m "refactor(simple_runner): check_all → exit_policy.evaluate

唯一 check_all 调用方迁到 ExitPolicy。零行为变化（evaluate 委托 check_all）。"
```

---

## Task 3: 迁移 8 个 check 调用方 → top（逐个 Edit，禁批量脚本）

**Files (逐个改):**
- `app/backtest/simple_runner.py:433` 和 `:452`
- `app/backtest/tdx_runner.py:266` 和 `:604`
- `app/backtest/simulate_one_trade.py:191`
- `app/sim_trader/engine.py:460`
- `app/sim_trader/intraday_monitor.py:179`
- `app/live_trader/exit_monitor.py:67-68`（见 Task 4 单独处理：加缺口注释）

> 约束：CLAUDE.md 禁止脚本批量改。**每个文件用一次 Edit，改完立刻 py_compile。**
> 通用替换：`exit_rule_engine.check(ctx)` → `exit_policy.top(ctx)`；`exit_rule_engine.check(ctx, skip_eod_only=True)` → `exit_policy.top(ctx, skip_eod_only=True)`（top 已在 Task 1 支持该 kwarg）。

- [ ] **Step 1: 先确认 8 处调用形状（审计已核实，迁移前再 grep 兜底）**

Run (Grep): pattern `exit_rule_engine\.check\(` , output content, -n true, glob `**/*.py`, path `app/`
Expected: 8 处（simple_runner:433,452 / tdx_runner:266,604 / simulate_one_trade:191 / sim_trader/engine.py:460 / sim_trader/intraday_monitor.py:179 / live_trader/exit_monitor.py:68），调用形状均为 `check(ctx)` 或 `check(ctx, skip_eod_only=True)`。若有其他 kwarg 或形状不符，停下核对。

- [ ] **Step 2: 逐文件迁移（每个文件 = 1 个 Edit + 1 个 py_compile）**

对下面 7 个文件（exit_monitor 留 Task 4），每个执行：
1. 顶部加 `from app.backtest.exit_policy import exit_policy`（若是函数内 lazy import，改 lazy import 来源）。
2. 把 `exit_rule_engine.check(ctx...)` 改为 `exit_policy.top(ctx...)`。

```
app/backtest/simple_runner.py:433   sig = exit_rule_engine.check(ctx)            → exit_policy.top(ctx)
app/backtest/simple_runner.py:452   sig = exit_rule_engine.check(ctx)            → exit_policy.top(ctx)
app/backtest/tdx_runner.py:266      signal = exit_rule_engine.check(ctx)         → exit_policy.top(ctx)
app/backtest/tdx_runner.py:604      signal = exit_rule_engine.check(ctx)         → exit_policy.top(ctx)
app/backtest/simulate_one_trade.py:191  signal = exit_rule_engine.check(ctx)     → exit_policy.top(ctx)
app/sim_trader/engine.py:460        signal = exit_rule_engine.check(ctx)         → exit_policy.top(ctx)
app/sim_trader/intraday_monitor.py:179  signal = exit_rule_engine.check(ctx, skip_eod_only=True)  → exit_policy.top(ctx, skip_eod_only=True)
```

> 每个文件改完立刻 `python -m py_compile <file>`，编译过再改下一个。

- [ ] **Step 3: 跑相关测试套件确认无回归**

Run:
```bash
python -m pytest tests/test_exit_policy.py tests/test_live_trader_smoke.py -v
python -m pytest tests/ -k "backtest or exit or sim" -v 2>&1 | tail -30
```
Expected: ExitPolicy 测试全过；既有回测/sim 冒烟无新增失败（行为等价）。

- [ ] **Step 4: Commit**

```bash
git add app/backtest/simple_runner.py app/backtest/tdx_runner.py app/backtest/simulate_one_trade.py app/sim_trader/engine.py app/sim_trader/intraday_monitor.py
git commit -m "refactor: 7 处 exit_rule_engine.check → exit_policy.top

simple_runner:433,452 / tdx_runner:266,604 / simulate_one_trade:191 /
sim_trader/engine.py:460 / sim_trader/intraday_monitor.py:179 全迁到 ExitPolicy.top。
行为等价（top 委托 check，透传 skip_eod_only）。"
```

---

## Task 4: 迁移实盘 exit_monitor:68 + 加显式缺口注释

**Files:**
- Modify: `app/live_trader/exit_monitor.py` (line 66-68 附近)

> 唯一真金白银路径，单独一个 Task，加"只吃 top()"缺口注释（CONTEXT.md 候选②显式缺口）。

- [ ] **Step 1: 读现状**

Run: `python -c "s=open('app/live_trader/exit_monitor.py',encoding='utf-8').read().splitlines(); [print(i+1, s[i]) for i in range(64,71)]"`
Expected: line 67-68：
```
67  from app.backtest.exit_rules import exit_rule_engine
68  signal = exit_rule_engine.check(ctx, skip_eod_only=True)
```

- [ ] **Step 2: 改调用 + 加缺口注释**

Edit `app/live_trader/exit_monitor.py`，把：

```python
            # 复用 exit_rules.check (v2 A2: signal 字段映射修正)
            from app.backtest.exit_rules import exit_rule_engine
            signal = exit_rule_engine.check(ctx, skip_eod_only=True)
```

改为：

```python
            # ⚠️ 显式缺口（CONTEXT.md 候选②，2026-07-19 grilling Q1=B）：
            # 实盘 scan 只取栈顶信号（top），不处理同 bar 多信号叠加（TP1 部分卖 + trailing 余量）。
            # 即 backtest 的 VERA 叠加栈在实盘被"显式降级为单信号"——已记录的已知缺口，不是 bug。
            # 让实盘真正吃栈（改本文件 scan 模型 + MAX_SELL_PER_SCAN）= 候选②-延展。
            from app.backtest.exit_policy import exit_policy
            signal = exit_policy.top(ctx, skip_eod_only=True)
```

- [ ] **Step 3: py_compile + 冒烟**

Run:
```bash
python -m py_compile app/live_trader/exit_monitor.py
python -m pytest tests/test_live_trader_smoke.py -v
```
Expected: 编译成功；live_trader 冒烟过。

- [ ] **Step 4: Commit**

```bash
git add app/live_trader/exit_monitor.py
git commit -m "refactor(live_trader): exit_monitor.check → exit_policy.top + 显式缺口注释

实盘 scan 迁到 ExitPolicy.top。加 CONTEXT.md 候选②缺口注释：实盘只吃栈顶、
不处理叠加栈（候选②-延展）。行为等价（top 委托 check）。"
```

---

## Task 5: Phase 1 收尾验证 + 清理冗余 import

**Files:**
- Verify: 全部迁移文件
- Cleanup: 删除迁移后已无引用的 `exit_rule_engine` import（仅限确认无引用的文件）

- [ ] **Step 1: 全量 py_compile 受影响文件**

Run:
```bash
python -m py_compile app/backtest/exit_policy.py app/backtest/simple_runner.py app/backtest/tdx_runner.py app/backtest/simulate_one_trade.py app/sim_trader/engine.py app/sim_trader/intraday_monitor.py app/live_trader/exit_monitor.py
```
Expected: 全部无输出（成功）。

- [ ] **Step 2: 确认迁移完整性（无遗漏 check/check_all 直调）**

Run (Grep): pattern `exit_rule_engine\.check(_all)?\(` path `app/`
Expected: 只剩 `app/backtest/exit_rules.py`（定义）和 `app/backtest/exit_policy.py`（委托）；**app/ 下其余文件 0 处**。若有遗漏，补迁。

- [ ] **Step 3: 清理确认无引用的 exit_rule_engine import（逐文件，谨慎）**

对每个迁移文件，Grep 该文件内是否还有 `exit_rule_engine` 的其他引用（如 build_context 路径可能仍用）。**只在确认 0 引用时**才删 `from app.backtest.exit_rules import exit_rule_engine` 这行；否则保留。每删一个立刻 py_compile。

> ⚠️ 风险：`RuleContext`/`ExitSignal` 类型可能仍从 `exit_rules` import——那行不能删。只删 `exit_rule_engine` 符号引用。

- [ ] **Step 4: 跑 ExitPolicy + 回测相关测试**

Run:
```bash
python -m pytest tests/test_exit_policy.py -v
python -m pytest tests/ -k "backtest or exit or sim or live" -v 2>&1 | tail -40
```
Expected: ExitPolicy 全过；既有套件无新增失败。

- [ ] **Step 5: Commit（若有清理）**

```bash
git add -A
git commit -m "chore(backtest): Phase 1 收尾 — 清理冗余 exit_rule_engine import

ExitPolicy 候选② Phase 1 完成：9 个调用方全迁，evaluate/top 行为等价，
实盘缺口显式记录。"
```

---

# Phase 2 — UI preview()（risk-status 路由收口）

> ⚠️ **Phase 2 风险高于 Phase 1**（审计 H1/H2/H3 已点名）：涉及前端 wire-format 契约、TP per-tier 设计、live 路由 ctx 字段来源。建议 Phase 1 上线稳定后再做。Task 6 先建 preview + keystone，Task 7 审前端契约，Task 8 迁路由。
>
> **审计要点（已并入下文）：** preview 出**英文短码 key**（HS/TR/TP1/...，非中文）（C3）；TP 按 `take_profit_tiers` **逐档**展开（TP1/TP2/...）（H3）；Task 8 ctx 必须带 `use_high_for_tp=True`（H1）和从 `tp_triggered` 解析的 `triggered_tiers`（H2）；路由实际在 `main.py:1505`（非 1497）（M1）；`exit_monitor._build_context` 耦合实例不可复用，路由内联最小 ctx（M3）。

## Task 6: 实现 preview() + keystone 测试（preview.fired == evaluate）

**Files:**
- Modify: `app/backtest/exit_policy.py`
- Test: `tests/test_exit_policy.py`

> 设计要点（防漂移）：`preview` 的**触发判定 fired** 直接调 `rule_fn`（与 evaluate 同源，**不**从收盘距离反推）。距离（remaining/budget）仅展示近似；status 由 fired 决定。TP 逐档：每档 fired = (idx ∈ triggered_tiers) 或 (tp 口径利润 ≥ 档位 profit_pct)；整体 TP fired 由 `rule_take_profit` 决定（keystone 锁：任一 TP* fired ⟺ TP ∈ evaluate 信号）。

- [ ] **Step 1: 先写失败测试（RED）**

在 `tests/test_exit_policy.py` 追加：

```python
# --- preview：UI 同源距离；英文短码 key；keystone = fired 与 evaluate 一致 ---

def test_preview_uses_english_keys(policy):
    """preview 出英文短码 key（HS/TR/TP1/...），非中文规则名（审计 C3）。"""
    pv = policy.preview(_ctx(close=10.1, low=10.15, high=10.2))
    assert "HS" in pv and "TR" in pv
    assert any(k.startswith("TP") for k in pv)   # 至少 TP1
    assert all(k == k.upper() for k in pv)        # 全英文大写

def test_preview_tp_per_tier(policy):
    """多档 TP 按 take_profit_tiers 展开（审计 H3）：TP1/TP2/...。"""
    ctx = _ctx(
        close=10.1, low=10.15, high=10.2,
        take_profit_tiers=[{"profit_pct": 0.03, "sell_ratio": 0.3},
                           {"profit_pct": 0.06, "sell_ratio": 0.3}],
    )
    pv = policy.preview(ctx)
    assert "TP1" in pv and "TP2" in pv

def test_preview_fired_matches_evaluate(policy):
    """keystone：preview 的 fired 判定 == evaluate 是否产出该规则信号（防 UI 漂移）。
    每个场景两侧各用全新 ctx（规避 triggered_tiers 原地改）。"""
    scenarios = {
        "no_signal": dict(close=10.1, low=10.15, high=10.2),
        "hs":        dict(low=9.3, close=9.4),
        "tp1":       dict(high=10.5, close=10.45, low=10.4, peak_price=10.5),
    }
    for name, kw in scenarios.items():
        firing_reasons = {sig.reason for sig in policy.evaluate(_ctx(**kw))}
        pv = policy.preview(_ctx(**kw))
        # 非分档规则（HS/TR）：fired 必须与 evaluate 一致
        for key in ("HS", "TR"):
            if key in pv:
                assert pv[key]["fired"] == any(r.startswith(key) for r in firing_reasons), (
                    f"preview/evaluate 漂移！场景={name} 规则={key} "
                    f"preview.fired={pv[key]['fired']} evaluate信号={firing_reasons}"
                )
        # TP 整体：任一 TP* fired ⟺ evaluate 有 TP 信号
        tp_fired_any = any(pv[k]["fired"] for k in pv if k.startswith("TP"))
        tp_in_eval = any(r.startswith("TP") for r in firing_reasons)
        assert tp_fired_any == tp_in_eval, (
            f"TP 整体漂移！场景={name} preview.TP任一fired={tp_fired_any} eval有TP={tp_in_eval}"
        )
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m pytest tests/test_exit_policy.py -k preview -v`
Expected: FAIL — `AttributeError: 'ExitPolicy' object has no attribute 'preview'`

- [ ] **Step 3: 实现 preview()（英文 key + 逐档 TP）**

在 `app/backtest/exit_policy.py`：

(a) 顶部加规则中文名 → 英文短码常量表（审计 M2：不内联中文字面量）：

```python
# 规则中文名（ALL_RULES_TRAILING 的 name 字段，exit_rules.py:317-326）→ 英文短码
# 实现前先 Grep '"硬止损"|"移动止盈"|"多档阶梯止盈"' app/backtest/exit_rules.py 确认未漂移
_NAME_TO_KEY = {
    "硬止损": "HS",
    "保本止损": "BE",
    "首日弱势离场": "FD",
    "强制时间退出": "TF",
    "移动止盈": "TR",
    "时间条件退出": "TC",
    "成交量高潮离场": "CLIMAX",
    # "多档阶梯止盈" 特殊处理：逐档展开成 TP1/TP2/...，不在此表
}
```

(b) 在 `ExitPolicy` 类内替换原 Phase 2 注释：

```python
    def preview(self, ctx: RuleContext) -> dict:
        """每条规则的触发判定 + 展示距离（供 UI 风控面板渲染）。

        防漂移核心：fired 直接调 rule_fn（与 evaluate 同源），不从距离反推。
        距离（remaining/budget）仅展示近似。取代 main.py:1505-1744 的 close 口径改写。
        英文短码 key（HS/TR/TP1/...）；TP 按 ctx.take_profit_tiers 逐档展开。

        返回：{key: {fired, status, trigger_value, current, remaining, budget, message}}
        status ∈ safe/warning/danger。
        """
        rules = self._engine._rules_trailing  # trailing_first 默认；与 evaluate/check 同源
        items = {}
        for _prio, rule_fn, name, _eod in rules:
            if name == "多档阶梯止盈":
                items.update(self._tp_tier_entries(ctx))   # 逐档 TP1/TP2/...
                continue
            try:
                sig = rule_fn(ctx)
            except Exception:
                sig = None
            key = _NAME_TO_KEY.get(name, name)
            items[key] = self._entry_for(name, ctx, sig is not None)
        return items

    @staticmethod
    def _entry_for(name: str, ctx: RuleContext, fired: bool) -> dict:
        """非分档规则的展示距离。距离数学移植自 main.py（仅展示）；fired/status 权威来自 rule_fn。"""
        cur = (ctx.close - ctx.entry_price) / ctx.entry_price * 100 if ctx.entry_price else 0.0
        if name == "硬止损":
            trig = ctx.hard_stop * 100
            return {"fired": fired, "status": "danger" if fired else "safe",
                    "trigger_value": trig, "current": cur,
                    "remaining": 0.0 if fired else abs(trig - cur),
                    "budget": abs(trig), "message": f"距硬止损 {trig:.1f}% 差 {abs(trig-cur):.1f}%"}
        if name == "移动止盈":
            trig = ctx.trail_dd * 100
            peak_pnl = (ctx.peak_price - ctx.entry_price) / ctx.entry_price * 100 \
                       if ctx.entry_price and ctx.peak_price else 0.0
            dd = peak_pnl - cur
            return {"fired": fired, "status": "danger" if fired else "safe",
                    "trigger_value": trig, "current": dd,
                    "remaining": 0.0 if fired else max(0, trig - dd),
                    "budget": trig, "message": f"回撤 {dd:.1f}%/{trig:.1f}%"}
        # BE/FD/TF/TC/CLIMAX：骨架条目
        return {"fired": fired, "status": "warning" if fired else "safe",
                "trigger_value": 0.0, "current": cur,
                "remaining": 0.0, "budget": 0.0, "message": name}

    @staticmethod
    def _tp_tier_entries(ctx: RuleContext) -> dict:
        """TP 逐档展开（审计 H3）。每档 fired = (idx ∈ triggered_tiers) 或
        (tp 口径利润 ≥ 档位 profit_pct)。tp 口径：use_high_for_tp=True 用 high，否则 close。
        距离仅展示；整体 TP 与 rule_take_profit 的一致性由 keystone 测试守。"""
        price = ctx.high if ctx.use_high_for_tp else ctx.close
        profit = (price - ctx.entry_price) / ctx.entry_price if ctx.entry_price else 0.0
        cur = (ctx.close - ctx.entry_price) / ctx.entry_price * 100 if ctx.entry_price else 0.0
        entries = {}
        for idx, tier in enumerate(ctx.take_profit_tiers or []):
            pct = tier.get("profit_pct", 0)
            already = idx in ctx.triggered_tiers
            would_fire = (not already) and profit >= pct
            fired = already or would_fire
            entries[f"TP{idx+1}"] = {
                "fired": fired,
                "status": "warning" if fired else "safe",
                "trigger_value": pct * 100, "current": cur,
                "remaining": 0.0 if fired else max(0, (pct - profit) * 100),
                "budget": pct * 100,
                "message": f"止盈{idx+1}档({pct*100:.1f}%){'已触发' if already else ('触发' if would_fire else '未触发')}",
            }
        return entries
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_exit_policy.py -v`
Expected: PASS（含 3 个 preview 用例）。**若 `test_preview_fired_matches_evaluate` 的 TP 用例失败**：说明 `_tp_tier_entries` 的 would_fire 近似与 `rule_take_profit` 实际触发不一致——核对 `rule_take_profit`（exit_rules.py:159-214）的档位判定条件（含 `tp_stack_mode`/`tp1_fill_pct` 影响），对齐 would_fire 判定。这是 keystone 设计意图：用它抓 TP 漂移。

- [ ] **Step 5: Commit**

```bash
git add app/backtest/exit_policy.py tests/test_exit_policy.py
git commit -m "feat(backtest): ExitPolicy.preview + keystone（fired 与 evaluate 同源）

preview 英文短码 key、TP 逐档展开。fired 直接调 rule_fn（不漂移），距离仅展示。
keystone 测试锁 preview.fired == evaluate。取代 main.py:1505-1744 的 close 口径改写。"
```

---

## Task 7: 审前端 risk-status 契约（迁移前必做）

**Files:**
- Read: `static/js/live_trader.js`（消费 risk_items 的部分，约 715-762 行；TP 分档显示约 523 行）

> 目的：迁路由前确认前端读哪些字段，保证 preview() 出来的 wire 格式覆盖前端所需。审计 M4 指出现路由还输出 `activated/peak_pnl/drawdown/trigger_days/current_days/sell_ratio/triggered` 等字段；前端进度条只读 `type/status/remaining/budget/message`，但 TP 分档 `sell_ratio/triggered` 在 ~523 行另有可视化。

- [ ] **Step 1: Grep 前端消费点**

Run (Grep): pattern `risk_items|global_status|risk-status|trigger_value|remaining|sell_ratio|triggered` path `static/js/`
列出每个字段的前端用法。

- [ ] **Step 2: 记录契约清单 + 决定字段取舍**

在本文档下方追加 `## Frontend Contract (audited YYYY-MM-DD)` 节，列出前端实际读取的字段。对 preview 未提供的字段（如 `sell_ratio`、`triggered`），决定：
- (a) 在 `_tp_tier_entries`/`_entry_for` 补齐（后端保证一致，不改前端）——**首选**；
- (b) 若前端确未用，记录可安全丢弃。

> ⚠️ 审计 M4：`tp_triggered_flag`/per-tier `sell_ratio` 在前端 ~523 行有可视化，**不能丢**。Task 6 的 `_tp_tier_entries` 已含 `fired`，若前端还读 `sell_ratio`/`triggered`，在此步补进 entries。

- [ ] **Step 3: （如发现契约缺口）补 preview 返回字段**

在 `_entry_for`/`_tp_tier_entries` 补 Task 7 Step 2 发现的字段。**不改前端**（后端前端一致由后端保证）。

---

## Task 8: 迁移 risk-status 路由（main.py:1505-1744 → preview）

**Files:**
- Modify: `app/live_trader/main.py:1505-1744`（审计 M1：路由起于 1505，非 1497；1497 是上一函数的 docstring 收尾）

> Phase 2 最大改动。策略：保留路由的 pos 读取 + wire shaping 骨架，把中间逐规则改写块换成 `exit_policy.preview(ctx)`。**审计已决定**：`exit_monitor._build_context` 耦合实例（self.qmt/_calc_hold_days/_load_risk_params/audit，见 exit_monitor.py:100-190）**不可复用**——路由内联一个最小 ctx 构建（M3）。

- [ ] **Step 1: 读完整路由现状（确认行号边界）**

Run: `python -c "s=open('app/live_trader/main.py',encoding='utf-8').read().splitlines(); [print(i+1, s[i]) for i in range(1503,1508)]"`
确认 1505 是 `@app.get("/live/config/risk-status")`，1744 是 `return {...}` 收尾。

- [ ] **Step 2: 替换逐规则改写块（含审计 H1/H2 修复）**

把 `# ----- HS 硬止损 -----` 到 `# ----- TP 多档止盈 -----` 结束（main.py:1578-1716，~140 行）替换为：

```python
        # 审计 H2：把 pos 的 tp_triggered(JSON: [{"tier": i}, ...]) 解析成 triggered_tiers 集合
        import json as _json
        triggered_tiers = set()
        try:
            _tt = _json.loads(tp_triggered) if isinstance(tp_triggered, str) else (tp_triggered or [])
            triggered_tiers = {t.get("tier") for t in _tt if isinstance(t, dict) and t.get("tier") is not None}
        except Exception:
            triggered_tiers = set()

        # 审计 M3：_build_context 耦合实例不可复用 → 路由内联最小 ctx
        # 审计 H1：use_high_for_tp=True（对齐所有 live/sim 兄弟路径，TP 用 high 检测）
        from app.backtest.exit_rules import RuleContext
        from app.backtest.exit_policy import exit_policy
        last_price = float(pos.get("last_price") or avg_cost)  # 实时价(refresh_quotes 3s 写)
        bar_high = float(pos.get("high") or last_price)        # 缺则退化为现价
        bar_low = float(pos.get("low") or last_price)
        ctx = RuleContext(
            entry_price=avg_cost,
            peak_price=peak_price or avg_cost,
            triggered_tiers=triggered_tiers,           # H2：已触发档位
            open=float(pos.get("open") or last_price),
            high=bar_high, low=bar_low, close=last_price,
            hold_days=holding_days, first_day_hold_value=2,
            hard_stop=rp.hard_stop,
            take_profit_tiers=rp.take_profit_tiers,
            trail_activate=rp.trail_activate, trail_dd=rp.trail_dd,
            time_exit_days=rp.time_exit_days, time_exit_profit=rp.time_exit_profit,
            time_force_days=rp.time_force_days,
            first_day_exit_min_profit=rp.first_day_exit_min_profit,
            first_day_exit_days=rp.first_day_exit_days,
            use_atr_trail=rp.use_atr_trail, atr_trail_multiplier=rp.atr_trail_multiplier,
            use_high_for_tp=True,                       # H1
            priority_mode="trailing_first", tp_stack_mode=True, tp1_fill_pct=0.03,
        )
        preview = exit_policy.preview(ctx)
        risk_items = _shape_preview_for_wire(preview, profit_rate)
```

> pos 的 `high/low/open` 字段来源：若 `live_positions` 表无这些列（exit_monitor._build_context 当时是从 QMT tick 取），需从 `qmt.get_realtime_quotes([code])` 取（候选③ Live Quote Facade 上线后改走 facade）。**实现时确认字段来源，不留占位**——这是 Phase 2 主要实现风险点。

- [ ] **Step 3: 实现 _shape_preview_for_wire 映射 helper**

在 main.py 模块级（或 `app/live_trader/utils.py`）加：

```python
def _shape_preview_for_wire(preview: dict, profit_rate: float) -> list:
    """把 ExitPolicy.preview 输出映射成前端期望的 risk_items。
    preview 已是英文短码 key（HS/TR/TP1/...），直接用作 type。"""
    items = []
    for key, info in preview.items():
        items.append({
            "type": key,                             # HS/TR/TP1/...
            "label": _KEY_LABEL.get(key, key),
            "trigger_value": info.get("trigger_value", 0),
            "current_pnl": profit_rate,
            "remaining": info.get("remaining", 0),
            "budget": info.get("budget", 0),
            "status": info.get("status", "safe"),
            "message": info.get("message", ""),
            "fired": info.get("fired", False),
        })
    return items

_KEY_LABEL = {"HS": "硬止损", "TR": "移动止盈", "BE": "保本止损", "FD": "首日弱势离场",
              "TF": "强制时间退出", "TC": "时间条件退出", "CLIMAX": "成交量高潮离场"}
# TP1/TP2/... 的 label 留 preview 的 message 已含"止盈N档"
```

> 若 Task 7 审出前端还读 `sell_ratio/triggered`（TP 分档可视化），在此 helper 对 `key.startswith("TP")` 的条目补这两个字段（从 preview 的对应 entry 或 pos 的 take_profit_tiers 取）。

- [ ] **Step 4: py_compile + 冒烟 + 前端字段对比**

Run:
```bash
python -m py_compile app/live_trader/main.py
python -m pytest tests/test_live_trader_smoke.py tests/test_exit_policy.py -v
```
手动（或 bb-browser skill）：启动 live_trader，访问 `/live/config/risk-status`，对比迁移前后 JSON：`risk_items` 字段集 ⊇ Task 7 审出的前端契约；`global_status` 计算逻辑保留（`STATUS_PRIORITY` 取最高）。

- [ ] **Step 5: Commit**

```bash
git add app/live_trader/main.py
git commit -m "refactor(live_trader): risk-status 路由 → ExitPolicy.preview

删 main.py:1578-1716 的 close 口径逐规则改写，改走 exit_policy.preview（与
引擎同源）。ctx 带 use_high_for_tp + 解析 triggered_tiers（审计 H1/H2）。
锁 CLAUDE.md 后端前端一致。"
```

---

## Final Verification（Phase 1 + Phase 2 各自收尾时都跑）

- [ ] `python -m pytest tests/test_exit_policy.py -v`（interface 测试全过）
- [ ] `python -m pytest tests/ -k "backtest or exit or sim or live" -v`（无新增失败）
- [ ] Grep `exit_rule_engine\.check` in `app/`：仅 exit_rules.py（定义）+ exit_policy.py（委托）有，其余 0 处。
- [ ] 实盘侧手动核：risk-status 面板对某持仓显示的 HS/TR/TP 触发状态，与该持仓实际是否被 exit_monitor 触发**一致**（候选②验收点：UI 与引擎对齐）。

---

## 自检（v2，合入审计发现后的 fresh-eye review）

**1. Spec 覆盖：** evaluate（Task 1）✓ / top + skip_eod_only（Task 1，已前置）✓ / preview（Task 6，英文 key + 逐档 TP）✓ / keystone top⟺evaluate[0]（Task 1，全新 ctx）✓ / keystone preview.fired⟺evaluate（Task 6）✓ / 9 调用方（Task 2+3+4）✓ / 显式缺口（Task 4）✓ / 审计 H1 use_high_for_tp + H2 triggered_tiers（Task 8 Step 2）✓ / H3 逐档 TP（Task 6）✓ / M1 行号 1505（Task 8）✓ / M3 _build_context 不可复用（Task 8，已决定）✓。

**2. 占位符扫描：** Task 8 Step 2 的 `bar_high/bar_low` 来源已注明实现时从 qmt/facade 确认——属需实现时填实的已知风险，非偷懒占位。其余步骤代码完整。

**3. 类型一致：** `evaluate(ctx)->List[ExitSignal]`、`top(ctx, skip_eod_only=False)->Optional[ExitSignal]`、`preview(ctx)->dict` 全计划一致；`exit_policy` 单例贯穿；`_shape_preview_for_wire`/`_entry_for`/`_tp_tier_entries`/`_NAME_TO_KEY`/`_KEY_LABEL` 命名一致；preview key 全英文短码（HS/TR/TP1）在 Task 6/7/8 一致 ✓。

**遗留/风险（显式标注）：**
- Task 8 的 pos `high/low/open` 字段来源（live_positions 表是否有这些列）是 Phase 2 主要实现风险——需实现时确认，可能需从 QMT tick 取（候选③ facade 上线后改走 facade）。
- `_tp_tier_entries` 的 would_fire 是展示近似，靠 keystone 测试抓与 `rule_take_profit` 的偏差；若 TP 用例失败须对齐 exit_rules.py:159-214。
- `evaluate` 是否需透传 `skip_eod_only`（当前唯一 evaluate 调用方不传）→ YAGNI 暂不加。

---

## Audit iteration（2026-07-19，code-reviewer 审计后修订记录）

本 v2 已合入以下审计发现（审计结论 FAIL → 修订后 PASS-WITH-WARNINGS）：

| 审计项 | 级别 | 处理 |
|---|---|---|
| C1/C2 测试复用同一 ctx → rule_take_profit 改 triggered_tiers 串味 | CRITICAL | **已修**：Task 1/6 所有断言用 `_ctx(**kw)` 现场构造全新 ctx；顶部"关键约束"明记此契约 |
| C3 preview 中文 key vs 测试英文 code 不匹配 | CRITICAL | **已修**：preview 出英文短码 key（`_NAME_TO_KEY`），Task 6 测试对齐 |
| H1 Task 8 ctx 缺 use_high_for_tp | HIGH | **已修**：Task 8 Step 2 显式 `use_high_for_tp=True` |
| H2 Task 8 ctx 缺 triggered_tiers | HIGH | **已修**：Task 8 Step 2 解析 pos `tp_triggered` → `triggered_tiers` |
| H3 TP 族级 vs 逐档 | HIGH | **已修**：Task 6 `_tp_tier_entries` 按 `take_profit_tiers` 逐档展开 |
| M1 行号 1497 → 1505 | MEDIUM | **已修**：Task 8 标注 1505 |
| M2 中文字面量 | MEDIUM | **已修**：`_NAME_TO_KEY` 常量表 |
| M3 _build_context 复用不可行 | MEDIUM | **已决定**：Task 8 内联最小 ctx，不试图复用 |
| M4 wire 字段丢失 | MEDIUM | **已处理**：Task 7 审前端契约，缺口补进 preview/handler |
| L1 未用 replace import | LOW | **已修**：删 import，改用 `_ctx(**kw)` |
| L2 _ctx 默认值与注释不符 | LOW | **已修**：注释更正 + 新增 no_signal 场景 |

**审计核实的正确部分（无需改）：** 9 个调用方行号+形状全对；check/check_all 签名对；keystone 不变量数学上成立（问题在测试不在不变量）；ExitSignal/RuleContext 字段对；_PREVIEW_NAME_TO_TYPE 映射对；Phase 1 先于 Phase 2 的排序对。
