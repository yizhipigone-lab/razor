# 计划书：live_trader/main.py 阶段 1.4 — trade router 拆分

> 日期：2026-07-19
> 性质：**高风险实施前规划**（trade 涉及真实下单/撤单/信号并发）
> 前置：阶段 1.1/1.2/1.3 已完成（system/market/config_api router），main.py 1007 行
> 状态：**待 code-reviewer 审计 → 迭代 → 用户批准后才动手**

---

## TL;DR

抽 `routers/trade.py`（10 个 trade 路由），**3 个核心服务函数（`place_order_service` / `process_buy_signals` / `_process_one_signal`）留 main.py**（阶段 3 才搬 services/），trade router 用**函数内 import** 调它们。预计 main.py 1007 → ~720 行。

**为什么这步风险最高**：trade 路由是真实交易动作（下单/撤单/信号），搬错即实盘事故。且 `place_order_service` / `process_buy_signals` 被**路由 + scheduler 生产路径**共用，import 链不能断。

---

## 1. 路由清单（10 个）+ 依赖矩阵

| # | 路由 | 行号 | _state 依赖 | auth | 核心服务(函数内import) | 其他 |
|---|---|---|---|---|---|---|
| 1 | POST /live/kill-switch/activate | 513 | kill_switch | — | — | — |
| 2 | POST /live/kill-switch/deactivate | 525 | kill_switch | — | — | — |
| 3 | POST /live/reconcile | 535 | reconciler | — | — | — |
| 4 | POST /live/exit-scan | 544 | exit_monitor | — | — | — |
| 5 | GET /live/audit/replay/{order_id} | 554 | audit | — | — | — |
| 6 | **POST /live/order** | 563 | config/runtime_state/kill_switch/qmt | — | **place_order_service** | schemas/price_type/xtquant_compat/hashlib |
| 7 | POST /live/order/cancel | 674 | store/qmt | _require_admin | — | — |
| 8 | **POST /live/buy-signal** | 881 | config/runtime_state/kill_switch | **_verify_token** | **process_buy_signals** | schemas |
| 9 | POST /live/cancel-by-source | 923 | store/qmt | — | — | xtquant_compat |
| 10 | GET /live/buy-signal/pending | 964 | — | — | — | 预留空端点 |

## 2. 核心：3 个服务函数**留 main.py**（阶段 1.4 不搬）

| 函数 | main.py 行号 | 被谁调 | 为何留 main |
|---|---|---|---|
| `place_order_service` | 654 | /live/order(路由6) + process_buy_signals | 阶段 3 搬 services/order_service.py |
| `process_buy_signals` | 713 | /live/buy-signal(路由8) + **scheduler.py:436(生产)** | 阶段 3 搬 services/signal_service.py（同步改 scheduler） |
| `_process_one_signal` | 814 | process_buy_signals 内部 | 跟 process_buy_signals 一起搬 |

**trade router 怎么调它们**：函数内 `from ..main import place_order_service, process_buy_signals`（运行时 import，main 已加载完，不循环）。

## 3. trade.py 顶部 import 设计

```python
import time
from datetime import date

from fastapi import APIRouter, HTTPException, Request

from core.logger import get_logger

from .._state import state as _state
from ..auth import _require_admin, _verify_token  # buy-signal 用 _verify_token

logger = get_logger("live_trader.routers.trade")

router = APIRouter()
```

> **审计修订（architect + code-reviewer W1.2）**：
> - ✅ 补 `import time` + `from datetime import date`：place_order 路由 L632 用 `date.today()`/`time.time()`，不补 = `NameError`
> - ✅ 删 `import asyncio`：trade router 本体不用（并发在 process_buy_signals 内），留着是死代码违反 YAGNI

**不顶部 import**：`place_order_service` / `process_buy_signals`（用函数内 import，避免 main↔routers 循环）。

## 4. 风险点（按严重级）

### 🔴 R1 [CRITICAL] place_order 链路 —— 真实下单
- **路由6 `/live/order`** 调 `place_order_service` → `OrderExecutor.execute` → 真实 QMT 下单。
- 搬迁后若函数内 `from ..main import place_order_service` 失败（任何拼写/路径错），下单端点直接 500，实盘无法手工下单。
- **🔴 dry-run 403 检查时机**（审计 W1.1，用户硬约束）：main.py:580-582 `runtime_state.is_live()` 失败→403 是**路由层**检查，在 place_order_service 调用**之前**。**必须随路由6 搬、保留在 service 调用前、顺序不可改**。漏搬或挪到 service 后 = dry-run 模式误下真单（实盘事故）。
- **🔴 source 参数语义**（审计 W4.1，用户硬约束）：路由6 调用签名 `place_order_service(intent, source="WEB", lock_wait_sec=30)`，与 _process_one_signal 的 `source="TDX"/lock_wait=5s` 不同。搬路由6 时**核对 main.py:648 一字不动**（WEB/30s 决定价格策略 + terminal 标记 + 清仓锁等待）。
- **缓解**：搬完后**必须实测** place_order_service 的 import 链（py_compile + import 验证 + pytest test_manual_order 全过）。⚠️ test_manual_order 走 **mock executor**，真实 QMT 下单链路**不在测试覆盖内**，搬完需人工 dry-run 切 live 冒烟（审计 W6.2）。

### 🔴 R2 [CRITICAL] buy-signal 链路 —— 信号并发 + scheduler 共用
- **路由8 `/live/buy-signal`** 调 `process_buy_signals`（asyncio 并发 + 心跳 + 幂等）。
- `process_buy_signals` 同时被 **`scheduler.py:436`（auto_buy 生产路径）** 调用。
- 搬迁后 main.py 仍定义 `process_buy_signals`（留 main），scheduler 的 `from .main import process_buy_signals` 不变 —— **阶段 1.4 不动 scheduler**（阶段 3 才改）。
- **关键**：`_verify_token` 已在 auth.py（阶段 0b），trade router 顶部 `from ..auth import _verify_token`，buy-signal 路由内 `_verify_token(authorization, config)` 不变。
- **buy-signal HTTP 层 4 步**（审计 W1.3）：路由整段(L881-920) **原样搬**，含 ① `runtime_state.buy_enabled` 检查(L894-898) ② `_verify_token` 鉴权(L901) ③ `BuySignalRequest` 校验(L905) ④ `kill_switch` 检查(L911) —— 不只是调 process_buy_signals。
- **缓解**：buy-signal 的 4 个 token 分支由 test_buy_signal_bridge 覆盖，搬完跑这个测试。

### 🟡 R3 [HIGH] 路由顺序与 FastAPI 注册
- 10 路由搬到 trade.py 后，main.py `app.include_router(trade_router)` 注册。
- `/live/audit/replay/{order_id}` 是**路径参数路由**，搬迁后确认仍能匹配（FastAPI 路径参数不受 router 拆分影响，但需验证）。
- **缓解**：搬完 grep 确认 10 路由都注册到 app + pytest。

### 🟡 R4 [HIGH] asyncio 在 main 的归属
- `process_buy_signals`（留 main）用 asyncio.Semaphore/gather/to_thread。
- trade router 本身**不需要** asyncio（并发逻辑在 process_buy_signals 内）。
- main.py 顶部 `import asyncio` 仍被 process_buy_signals 用 → **不是死 import，保留**。

### 🟢 R5 [MEDIUM] 路由内 import 调整（相对路径单点→双点）
- **🔴 漏点（审计 architect，最高价值）**：搬到 routers/trade.py 后，相对 main.py 的单点 import 必须改双点（多一层 routers/ 目录）：
  - /live/order：`from .schemas` → `from ..schemas`、`from .price_type` → `from ..price_type`
  - /live/buy-signal：`from .schemas` → `from ..schemas`
  - **不改 = ImportError**（最易漏，因为语法看起来没错）
- cancel-by-source 的 `from app.utils.xtquant_compat`（L934）是绝对路径，不变。
- **seq>0 守卫**（审计 W4.2）：cancel-by-source L947-949 `if seq > 0 and qmt.connected: qmt.cancel_order(seq)` 必须保留，漏 = seq=0 触发 `cancel_order(0)` 未定义行为。

## 5. 拆分顺序（风险从低到高）

按"先简单后复杂"，**每个 router 搬完立刻冒烟**：

1. **batch 1（6 个简单路由）**：kill-switch/activate + deactivate + reconcile + exit-scan + audit/replay + buy-signal/pending
   - 这 6 个（pending 是空端点）依赖单一 _state 组件，无核心服务调用。
2. **batch 2（2 个中等路由）**：order/cancel + cancel-by-source
   - 用 _state(store/qmt) + _require_admin/xtquant_compat。
3. **batch 3（2 个高风险路由）**：/live/order（place_order_service）+ /live/buy-signal（process_buy_signals + _verify_token）
   - 最核心，最后搬，搬完重点验证。

> 实际操作可合并 batch，但**冒烟必须覆盖 place_order + buy-signal**。

## 6. 验证清单（实施后必跑）

| # | 验证项 | 方法 | 预期 |
|---|---|---|---|
| 1 | py_compile | `python -m py_compile main.py routers/trade.py` | 通过 |
| 2 | import 链 + 身份 | `from app.live_trader.main import place_order_service, process_buy_signals` | 可 import（留 main） |
| 3 | 10 路由注册 | `from app.live_trader.main import app; paths` | 10 路由全在 |
| 4 | asyncio 非死 import | grep `asyncio\.` in main.py | **≥3 处**（L776 Semaphore / L779 gather / L874 to_thread，都在留 main 的服务内） |
| 5 | **pytest 全套** | 4 文件 81 用例 | 全过（test_manual_order 的 place_order + test_buy_signal_bridge 的 4 token 分支）；smoke 需覆盖 10 路由注册 + 基本响应（审计 W6.1：补 8 路由归宿） |
| 6 | main.py 残留 | grep 10 路由装饰器 in main.py | 0 |
| 7 | _verify_token/_require_admin 残留 | grep in main.py | 0（在 auth.py，main 只剩 re-export import 行） |
| 8 | **trade.py 相对 import 双点**（审计 architect） | `grep -nE "from \.schemas\|from \.price_type" routers/trade.py` | **0 命中**（必须是 `..` 双点，否则 ImportError） |

## 7. 行数预估

- 搬走 10 路由约 ~285 行
- main.py **1007 → ~720 行**（= 1007 − 285）。注：place_order_service + process_buy_signals + _process_one_signal 这 ~250 行**原本就在 main 内、本次不搬所以保留**（非新增），阶段 3 搬 services 时才带走，届时 main 到 ~250。

## 8. 回滚预案

- 所有改动未 commit，`git diff` 可见。
- 若任一冒烟失败：`git checkout app/live_trader/main.py` 回退（routers/trade.py 删除即可，它是新文件）。
- 回滚不影响阶段 1.1/1.2/1.3（已独立的 router）。

## 9. 不做的事（YAGNI）

- ❌ 不搬 place_order_service / process_buy_signals / _process_one_signal（阶段 3）
- ❌ 不改 scheduler.py:436（阶段 3）
- ❌ 不治同名 sync_positions（那俩在 system router，已处理）
- ❌ 不重构 place_order 路由的市价单估算逻辑（只搬位置）

---

## 审计修订记录（2026-07-19，Step 3 迭代）

经 code-reviewer + architect 双 agent 审计（报告归档 `AUDIT-trade_router计划书_文档质量_2026-07-19.md` + `ARCHREVIEW-trade_router计划书_技术可行性_2026-07-19.md`），已逐条处理：

| Finding | 来源 | 处理 |
|---|---|---|
| F2.1 batch1 标题"5 个"vs"6 个" | code-reviewer | ✅ §5 已改 |
| F2.2 asyncio "≥4 处"实际 3 处 | code-reviewer | ✅ §6 验证项 4 已改 |
| trade.py 顶部漏 `date`/`time` | architect + code-reviewer W1.2 | ✅ §3 已补（不补 = NameError） |
| trade.py 顶部多余 `import asyncio` | architect | ✅ §3 已删（死代码） |
| R1 漏 dry-run 403 检查时机 | code-reviewer W1.1 | ✅ §4 R1 已补（漏 = 误下真单） |
| R1 漏 source 参数语义 | code-reviewer W4.1 | ✅ §4 R1 已补 |
| R1 漏 mock 提示 | code-reviewer W6.2 | ✅ §4 R1 已补 |
| **R5 漏相对 import 单点→双点** | architect | ✅ §4 R5 已补（**最高价值**，不补 = ImportError） |
| R5 漏 seq>0 守卫 | code-reviewer W4.2 | ✅ §4 R5 已补 |
| R2 漏 buy-signal HTTP 层 4 步 | code-reviewer W1.3 | ✅ §4 R2 已补 |
| 验证清单漏 grep 双点 | architect | ✅ §6 验证项 8 已加 |
| 行数预估表述误导 | code-reviewer W2.3 | ✅ §7 已改 |
| 8/10 路由测试归宿 | code-reviewer W6.1 | ✅ §6 验证项 5 已补 smoke |

**两份审计共识**：技术方案站得住 —— 循环 import 规避、3 核心服务留 main、scheduler:436 不动、函数内 import 模式已被 system/market/config_api 三个 router 验证、路由+服务行号 100% 准确。所有 CRITICAL/HIGH 已修入正文，MEDIUM/LOW 已在上表记录处理。**计划书达"可施工"标准**。

---

## 待办

- [x] Step 2：code-reviewer + architect 双 agent 审计（完成）
- [x] Step 3：据审计意见迭代（完成，见上表）
- [ ] Step 4：用户批准后实施
