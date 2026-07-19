# 计划书：live_trader/main.py 阶段 2 — lifecycle.py 拆分

> 日期：2026-07-19
> 性质：**启动核心搬迁**（lifespan 是服务装配入口，错了服务起不来）
> 前置：阶段 1 已完成 + commit（358a1c6），main.py 741 行
> 状态：**待双 agent 审计 → 迭代 → 用户批准后才动手**

---

## TL;DR

抽 `app/live_trader/lifecycle.py`，把 **lifespan + 启动辅助 + 持仓接管 + 残留清理 + 进程管理** 整组搬走（architect 在 EVAL 审计时明确要求"整组搬"）。lifespan **整段搬不重组**（装配顺序零改动），风险主要在 import 改源有没有漏。预计 main.py **741 → ~298 行**（审计 H1 修正：_cleanup_dryrun 行号原误算 L351-495/145 行，实际 L351-461/111 行，搬走总量 ~443 行）。

**为什么这步要规划**：lifespan 是服务启动入口（装配 15 组件 + 连 QMT + 持仓接管 + 调度器启动），搬错 = 服务起不来（实盘可用性事故）。虽不动交易逻辑（资金安全无忧），但多处 import 改源（4 处 routers + main app + 2 外部）+ main 死 import 清理，机械细节多。

---

## 1. lifecycle.py 内容（6 组，整组搬）

> **审计 L4**：下表 #1-#6 是内容索引，**实施时整组一次性搬**（lifespan 跨组依赖其他 5 组，分步会产生中间断链）。

| # | 函数/变量 | main.py 行号 | 被谁调 | 备注 |
|---|---|---|---|---|
| 1 | `_check_port_in_use` | L36-40 | lifespan(L98) | 启动端口守卫，**只 lifespan 用** |
| 2 | `_acquire_lock` | L42-61 | lifespan(L104) | 文件锁，写 `_state["lock_fd"]/["lock_file"]`，**只 lifespan 用** |
| 3 | `lifespan(app)` | L65-278 | `app=FastAPI(lifespan=...)` | **启动核心**：装配 15 组件 + 连 QMT + 接管 + 调度器；关闭段调 `_kill_all_subprocesses` |
| 4 | `_takeover_positions` | L281-348 | lifespan(L165) + routers/system.py(L69/91) + poc/test | 持仓接管 |
| 5 | `_cleanup_dryrun_residue` | L351-461 | lifespan(L167) | dry-run 残留清理（111 行，审计 H1 修正：原误算 145，含 live/dry-run 分支）|
| 6 | `_spawned_processes`/`_spawned_lock`/`_cleanup_zombies`/`_kill_all_subprocesses` | L704-731 | lifespan(L270) + routers/system.py(L188/217) | 进程管理（architect 要求整组搬）|

## 2. lifespan 整段搬 —— 装配顺序不变（核心安全保障）

lifespan 函数体 L65-278 **原样搬到 lifecycle.py**，**不重组装配顺序**。理由：
- 装配顺序有依赖（callback 的 kill_switch/clearance_lock/pnl_engine 注入 L154-157；exit_monitor 依赖 risk_gate/clearance_lock 等 L150-151；executor 创建后注入 exit_monitor L246）
- 整段搬 = 顺序零改动 = 启动逻辑不变

**lifespan 内的 15 个组件 import**（L67-81 `from .config import load_config` 等，审计 M1 修正：原误算 12/13）：搬到 lifecycle.py 后相对路径**不变**（lifecycle.py 与 main.py 同级，都在 `app/live_trader/`）。

## 3. lifecycle.py 顶部 import 设计

```python
import os
import threading as _threading
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import FastAPI

from core.logger import get_logger

from ._state import state as _state

logger = get_logger("live_trader.main")  # 审计 A1/M3:沿用 main 名,保启动/接管/QMT失败等 32 处日志源标签(运维监控依赖)
```

- `asynccontextmanager`：lifespan 装饰器（L64）
- `os`：lifespan 文件操作（L274 等）+ _acquire_lock（makedirs）
- `threading as _threading`：_spawned_lock
- `date`/`datetime`：lifespan（L174 datetime.now）+ _takeover（L339 date.today）
- `FastAPI`：lifespan 类型注解
- `_state`：lifespan 装配写组件 + 关闭段读 lock_fd
- **不需要** `subprocess`/`sys`（_spawned 只 append Popen 对象，Popen 在 system router 创建）
- **审计 M2**：lifespan 函数体内的**局部 import**（`from .runtime_state import load_runtime_state` L92 / `from datetime import datetime as _dt` L132 / `from .order_executor import OrderExecutor` L237）随整段搬走，相对路径不变，**不提到顶部**

## 4. import 改动清单（机械工作重点）

### 4.1 routers/system.py（4 处函数内 import 改源）
| 行 | 当前 | 改后 |
|---|---|---|
| L69 | `from ..main import _takeover_positions` | `from ..lifecycle import _takeover_positions` |
| L91 | `from ..main import _takeover_positions` | `from ..lifecycle import _takeover_positions` |
| L188 | `from ..main import _cleanup_zombies, _spawned_lock, _spawned_processes` | `from ..lifecycle import ...` |
| L217 | `from ..main import _cleanup_zombies, _spawned_lock, _spawned_processes` | `from ..lifecycle import ...` |

### 4.2 main.py
- 顶部加 `from .lifecycle import lifespan`（app 创建用）
- 顶部加 `from .lifecycle import _takeover_positions`（**re-export 别名**，给 poc 脚本 + test 兜底，阶段 3 再改外部 import 源）
- `app = FastAPI(lifespan=lifespan, ...)` 不变（lifespan 从 import 来）

### 4.3 外部依赖（靠 main re-export 兜底，不改）
- `scripts/poc_dryrun_e2e.py:94` `from app.live_trader.main import _takeover_positions`
- `tests/test_live_trader_smoke.py:614` `from app.live_trader.main import _takeover_positions`
- 两处靠 main.py 的 re-export 别名工作，**不改**（阶段 3 改）

## 5. main.py 死 import 清理（搬走后变死）

| import | 原使用者 | 搬后状态 |
|---|---|---|
| `import socket` | `_check_port_in_use` | **死** → 删（审计确认只 _check_port 用）|
| `from contextlib import asynccontextmanager` | `lifespan` 装饰器 | **死** → 删 |
| `import os` | lifespan/_acquire_lock | **死 → 删**（审计 L2 grep 确认 main 剩余代码零 `os.`）|
| `import time` | — | **现在就死**（审计 L1：grep `time.` 零使用，顺手删）|
| `import threading as _threading`（L708 局部）| 进程管理 | 随进程管理搬走 |
| `import subprocess` / `import sys`（L706-707 局部）| 进程管理区 | 随进程管理搬走（审计 A2/L1 补）|

## 6. 风险点（按严重级）

### 🔴 R1 [HIGH] lifespan 启动核心 —— 服务可用性
- lifespan 是服务装配入口，搬错 = 服务起不来（实盘可用性事故，但**不动交易逻辑**，资金安全无忧）。
- **缓解**：lifespan 整段搬不重组 + py_compile + import 验证 + **pytest 81 全过**（含 test_live_trader_smoke 的 lifespan 相关）+ 人工启动冒烟（用户做）。

### 🟡 R2 [MEDIUM] _state 跨模块读写一致性
- lifespan 在 lifecycle.py 写 `_state`（装配组件），main/routers 读 `_state`。
- `_state` 是模块级 dict，lifecycle.py `from ._state import state as _state` 拿同一对象，main 也 import 同一对象。dict 引用透明，**读写一致**（阶段 0a 已验证模式）。
- 关闭段读 `_state["lock_fd"]/["lock_file"]`（L272-275）—— _acquire_lock 写这俩（同 lifecycle.py），OK。

### 🟡 R3 [MEDIUM] import 改源漏改
- 4 处 routers/system.py + main lifespan 来源 + 2 外部 re-export。
- **缓解**：验证清单 grep 确认 `from ..main import _takeover` / `_cleanup_zombies` 在 routers 应 0 命中（全改 lifecycle）。

### 🟢 R4 [LOW] 死 import 残留
- socket/asynccontextmanager 搬走变死。os 待 grep。
- **缓解**：py_compile + grep 确认 + 顺手清。

### 🟢 R5 [LOW] _cleanup_dryrun_residue 大函数（111 行）机械搬
- 整段搬，函数体不动。低风险（只 lifespan 调）。

### 🟡 R6 [MEDIUM] 日志源标识变化 —— 运维监控（审计 A1 + M3，**最值得修**）
- lifespan(22处) + _takeover(2处) + _cleanup_dryrun(8处) 共 **32 处 logger 调用**，原挂 `live_trader.main`。
- 若 lifecycle.py 用 `get_logger("live_trader.lifecycle")`，启动/接管/残留/QMT 失败等关键日志 logger name 全变，**运维/NSSM/告警按 `live_trader.main` 检索会失配**，可能误报"服务未起"。
- **缓解**：lifecycle.py **沿用 `get_logger("live_trader.main")`**（Python 允许多模块共用同一 logger name），§3 已改。

## 7. 验证清单（实施后必跑）

| # | 验证项 | 方法 | 预期 |
|---|---|---|---|
| 1 | py_compile | `python -m py_compile lifecycle.py main.py` | 通过 |
| 2 | import main | `from app.live_trader.main import app` | 不报错（app 创建 + lifespan 绑定）|
| 3 | lifespan 来自 lifecycle | `from app.live_trader.main import lifespan; from app.live_trader.lifecycle import lifespan as l2; assert lifespan is l2` | True |
| 4 | 44 路由不变 | `len(app.routes)` | 44 |
| 5 | main.py 残留 | grep `^async def lifespan\|^def _takeover\|^def _cleanup_dryrun\|^def _cleanup_zombies\|^def _kill_all\|^def _check_port\|^def _acquire_lock\|^_spawned` in main.py | 0 |
| 6 | routers import 源 | grep `from \.\.main import _takeover\|from \.\.main import _cleanup_zombies\|_spawned_lock\|_spawned_processes` in routers/system.py | 0（全改 lifecycle，审计 L3 扩展覆盖多符号一行）|
| 7 | main 死 import | grep `socket\.\|asynccontextmanager` in main.py | 0（已删）|
| 8 | **pytest 全套** | 4 文件 81 用例 | 全过 |
| 9 | **人工启动冒烟** | 用户在实盘启动服务 | lifespan 跑通（连 QMT + 拿锁 + 装配 + 调度器起）|

## 8. 行数预估（修正 EVAL 原估）

- 搬走：lifespan(208) + _check_port/_acquire_lock(30) + _takeover(68) + _cleanup_dryrun(**111**，审计 H1 修正：原误算 145) + 进程管理(28) ≈ **~443 行**
- main.py **741 → ~298 行**（远低于 EVAL 原估 ~500 —— 因 lifespan + 生命周期函数占 main.py 60%）
- 阶段 3 搬 services（place_order_service/process_buy_signals/_process_one_signal ~180 行）后 main.py → **~80 行**

## 9. 回滚预案

- 改动未 commit，`git diff` 可见。
- 若冒烟失败：`git checkout app/live_trader/main.py app/live_trader/routers/system.py`（lifecycle.py 删除即可，新文件）。
- 回滚不影响阶段 1（已 commit 358a1c6）。

## 10. 不做的事（YAGNI）

- ❌ 不搬 place_order_service/process_buy_signals/_process_one_signal（阶段 3）
- ❌ 不改 scheduler.py:436（阶段 3）
- ❌ 不改外部脚本/测试 import 源（靠 main re-export 兜底）
- ❌ 不重组 lifespan 装配顺序（整段搬）

---

## 审计修订记录（2026-07-19，Step 3 迭代）

经 code-reviewer + architect 双 agent 审计（报告：`AUDIT-lifecycle计划书_文档质量_2026-07-19.md` + `ARCHREVIEW-lifecycle计划书_技术可行性_2026-07-19.md`），逐条处理：

| Finding | 来源 | 处理 |
|---|---|---|
| **H1** _cleanup_dryrun 行号 L351-495 错（实际 L351-461/111 行）| code-reviewer | ✅ §1 表 + §8 算式 + R5 全改，main 终态 ~260→~298 |
| **M1** 组件数 13/12 打架（实际 15）| code-reviewer | ✅ TL;DR + §1 表 + §2 统一 15 |
| **M3+A1** logger name 变化（32 处日志，运维监控失配）| 双方共识 | ✅ §3 改沿用 `live_trader.main` + 补 R6 |
| M2 lifespan 内 3 局部 import 未说明 | code-reviewer | ✅ §3 末尾补说明 |
| L1 死 import 漏 time/subprocess/sys | code-reviewer + architect | ✅ §5 表补全 |
| L2 os 待 grep 确认 | code-reviewer | ✅ §5 改"确认可删" |
| L3 验证清单 grep 模式漏多符号 | code-reviewer | ✅ §7 项6 扩展 |
| L4 §1 编号暗示分步 | code-reviewer | ✅ §1 加"整组一次搬" |

**两份审计共识**：技术方案站得住（lifespan 整段搬不重组、_state 引用透明、_check_port/_acquire_lock/_cleanup_dryrun 均 lifespan 单点用、4 处 routers 改源清单完整、re-export 无循环 import）。所有 HIGH/MEDIUM 已修入正文，LOW 已记录处理。**计划书达"可施工"标准**。

---

## 待办

- [x] Step 2：code-reviewer + architect 双 agent 审计（完成）
- [x] Step 3：据审计迭代（完成，见上表）
- [ ] Step 4：用户批准后实施
