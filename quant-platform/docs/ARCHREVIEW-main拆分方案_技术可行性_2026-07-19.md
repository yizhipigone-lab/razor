# 架构技术可行性深审：live_trader/main.py 拆分方案

> 评估对象：`docs/EVAL-live_trader_main拆分评估_2026-07-19.md`
> 审计员：architect agent（Read/Grep/Glob 实际验证，禁止派子 agent）
> 日期：2026-07-19
> 总体结论：**WARNING（有条件 PASS）**。拆法主体在架构上站得住，不会"启动即崩"，但原方案有 3 个关键技术遗漏，落地前必须补，否则会埋雷。

---

## 一、6 个核心问题逐项回答

### 问题 1：_state 抽 `_state.py` 能否避免循环 import？有没有"lifespan 没跑、路由用空 _state"的隐患？

**结论：能避免循环 import；"lifespan 没跑用空 _state"的隐患当前已被路由守卫拦住，不会崩。**

**证据**：
- `_state: dict = {}` 定义在 `app/live_trader/main.py:26`，是模块级可变 dict。
- lifespan 在 `main.py:57-271` 装配组件，通过 `_state.update({...})`（L221）和 `_state["xxx"] = ...`（L83/87/237/247）写入。
- 路由函数体统一用 `store = _state.get("store")` 模式取组件（如 `main.py:481-485, 532, 565-568, 587, 602, 653` 等共 60+ 处）。
- **关键守卫**：所有依赖 lifespan 装配的路由都有显式 503 守卫，例如：
  - `main.py:499-500` `if not qmt or not qmt.connected: raise HTTPException(503, "QMT 未连接")`
  - `main.py:533-534` `if not store: raise HTTPException(503, "Store 未初始化")`
  - `main.py:664-665, 675-676, 686-687, 694-695, 704-705, 723-724, 1168-1169` 等十余处同样守卫。
- 抽 `_state.py` 后，子路由 `from ._state import state`，main 里 lifespan 完成装配时 `_state.update({...})` 仍写到**同一个 dict 对象**（dict 是引用类型，`from X import Y` 绑定的是对象引用而非快照）。

**lifespan 没跑就用空 _state 的场景**：
- 启动期间（lifespan 在跑但未完成 `_state.update`）：FastAPI 在 lifespan 完成**之前不接收请求**，所以请求处理时 _state 必然已装配。无隐患。
- 测试场景（TestClient 不开 lifespan）：测试**主动注入** `_state`，如 `tests/test_manual_order.py:34-38`。

### 问题 2：FastAPI include_router 顺序与 lifespan —— router 模块 import 时会不会读 _state？

**结论：不会。import 时只创建空 APIRouter 对象 + 注册路由元数据；读 _state 仅在请求处理时。拆分不会启动即崩。**

**证据**：
- APIRouter 实例化（`router = APIRouter()`）是模块级，但只创建空路由表。
- `@router.get("/path")` 装饰器在 import 时执行，但只把 `(path, methods, handler_fn)` 三元组注册到 router.route list，**handler 函数体不执行**。
- 路由函数体内所有的 `_state.get(...)` 是**函数运行时**才求值，import 时不触发。
- main.py `app.include_router(...)` 同样只是把 router 的 route list 复制进 app.router.routes，不调用 handler。

**import 链推断**：`main → routers.market → _state`（拿 dict 引用，不读内容）→ 全程无人读空 dict，安全。

### 问题 3：组件传递方式 —— `from ._state import state` vs `app.state` vs Depends，哪个更适合？

**结论：原方案建议的 `from ._state import state as _state` 是这个项目的最优选择。改造面 = main.py 1 行 + _state.py 几行。**

| 方案 | 改造面 | 评估 |
|---|---|---|
| `from ._state import state as _state` | main.py 顶部 1 行 + 路由函数体**零改动** | **优选**。dict 引用透明，所有 `_state.get("xxx")` 原样工作。 |
| `request.app.state.xxx` (FastAPI 标准) | 40 个路由函数体全改 | 不可取。改造面巨大且无收益。 |
| `Depends(get_store)` | 40 个路由函数签名全改 + 写一堆 provider | 不可取。改造面最大，且 503 守卫语义要重写。 |

**关键事实**：Python `from M import x` 绑定的是**对象引用**而非值快照，dict 这种 mutable 对象的"引用透明"成立。

**更小改动方案（不抽 _state.py）评估**：保留 main 的 _state，子路由 `from .main import _state`。
- **不可行（循环 import 触发）**：main.py 顶部 `app = FastAPI(lifespan=lifespan)`（L457）+ 一堆 `@app.post(...)` 必须在 routers import 之前执行。如果 main.py 顶部再 `from .routers.trade import trade_router`，routers/trade.py 顶层 `from .main import _state` 时 main 还没执行完，Python 返回部分初始化的 main 模块对象。
- 仅当 `_state: dict = {}`（L26）定义**早于** main 里所有 `from .routers import ...` 才能侥幸 work。这种"依赖 import 顺序"的脆弱结构是坑，抽 `_state.py` 是正解。**原方案判断对**。

### 问题 4：同名函数治理 —— 拆到 routers 后改名影响路由注册吗？

**结论：不影响。FastAPI 用函数对象注册，与函数名无关。**

**证据**：
- `main.py:557` `async def sync_positions(request, body=None)` 上方 `@app.post("/live/positions/sync")`
- `main.py:1133` `async def sync_positions()` 上方 `@app.post("/live/sync-positions")`
- FastAPI `@app.post(path)` 装饰器把**函数对象**注册到 `app.router.routes`，键是 `path + methods`，**不是函数名**。
- 函数名仅作 OpenAPI `operation_id` 默认值，不影响路由匹配。
- Python 模块命名空间内后定义的 `sync_positions` 会覆盖前者（L1133 覆盖 L557 的名字绑定），但**装饰器早已把两个函数对象都注册到路由表**，所以两条路径都能正确分发。

**原方案判断正确**。拆时分别命名为 `sync_positions_admin`（L557）和 `sync_positions_full`（L1133），路由表无影响。当前 main.py 内部无人通过函数名调用 `sync_positions`，改名安全，建议拆时 grep 确认。

### 问题 5：lifespan 留 main.py 的决策对不对？

**结论：现阶段（阶段 0/1）留 main.py 合理；阶段 2 抽 lifecycle.py 时再搬，但必须连 `_cleanup_zombies` / `_kill_all_subprocesses` 一起搬，原方案没说清。**

**证据**：
- lifespan 函数体在 `main.py:57-271`，关闭段 L263 调 `_kill_all_subprocesses()`。
- `_cleanup_zombies` / `_kill_all_subprocesses` 定义在 `main.py:1760-1777`，操作模块级 `_spawned_processes` / `_spawned_lock`（L1756-1757）。
- `/shutdown` 路由（L1209）和 `/live/sync/intra`（L1865）/ `/live/sync/index_daily`（L1895）都依赖这两个函数 + 两个模块级状态。

**原方案遗漏**：把 lifespan 搬 lifecycle.py 时，`_cleanup_zombies` / `_kill_all_subprocesses` / `_spawned_processes` / `_spawned_lock` 必须同搬，否则 lifespan 内 L263 引用失败。

**更优位置（建议）**：阶段 2 把 lifespan + `_cleanup_zombies` + `_kill_all_subprocesses` + `_spawned_processes` 整组搬 lifecycle.py；main.py 留 re-export 别名给外部脚本和测试用。

### 问题 6：阶段顺序 —— 阶段0 抽 _state → 阶段1 抽路由 → 阶段2 抽服务，有更优解吗？

**结论：原方案的 3 阶段顺序大体对，但漏了 2 个硬约束：auth 必须先于路由抽；scheduler.py 的反向 import 必须在抽 service 时同步改。**

**漏的硬约束 1 —— auth 必须先于路由抽**：
- 多条 trade/system 路由内部调 `_require_admin(request)` / `_is_local(request)` / `_verify_token(...)`（如 `main.py:564, 829, 1191, 1217, 1251, 1369, 1409`）。
- 如果路由先抽到 routers/，routers/trade.py 顶层 `from .main import _require_admin` —— 又回到 main↔routers 循环 import。
- 正解：先抽 `auth.py`，main.py 留 `from .auth import _verify_token, _is_local, _require_admin`，然后 routers 才能 `from .auth import _require_admin` 不绕回 main。
- **建议阶段顺序调整**：
  - 阶段 0a：抽 `_state.py` + main re-export
  - 阶段 0b：抽 `auth.py`（纯函数最安全）+ main re-export
  - 阶段 1：抽 4 个 routers（system → market → config_api → trade）
  - 阶段 2：抽 `lifecycle.py`（含 `_cleanup_zombies` 等）
  - 阶段 3：抽 `services/order_service.py` + `services/signal_service.py`，**同步改 scheduler.py 的 import 路径**

**漏的硬约束 2 —— scheduler.py 反向依赖**（详见 R1）。

---

## 二、原方案遗漏的额外架构风险（5 条）

### R1 [CRITICAL] scheduler.py 已经反向 import main，原方案完全没提

**证据**：
- `app/live_trader/scheduler.py:436` `from .main import process_buy_signals`（生产路径，auto_buy 自给自足下单用）
- `app/live_trader/scheduler.py:555` `from .main import _state`（生产路径，`_cleanup_notifications` 读 notif_store 用）

**影响**：
- 这是**生产代码**，不是测试。scheduler.py 是 lifespan 启动的核心调度器（`main.py:242-247`）。
- 抽 `services/signal_service.py` 时如果不同步改 scheduler.py L436 的 import 路径，scheduler 仍 `from .main import process_buy_signals`，而 main 已不再定义该函数（只留 re-export 别名），技术上能 work 但**埋雷**：未来若谁清理了 main.py 的 re-export 别名，auto_buy 直接崩，且只在 14:50 触发时才暴露（晚发现）。
- 抽 `_state.py` 时 scheduler.py L555 同样依赖 main 的 re-export 别名，否则 `_cleanup_notifications` 15:35 失败。

**建议**：抽 service 时**同步**把 scheduler.py 的 `from .main import X` 改为 `from .services.signal_service import process_buy_signals` 和 `from ._state import state as _state`，不要靠 main 的 re-export 兜底生产路径。re-export 仅用于外部脚本和测试。

### R2 [HIGH] 外部依赖被严重低估，原方案说"只有 1 处"是错的

**证据**（grep 全仓库 `app.live_trader.main` 的结果）：

| 文件 | 行号 | 依赖符号 | 路径性质 |
|---|---|---|---|
| `scripts/poc_dryrun_e2e.py` | 94 | `_takeover_positions` | 外部脚本（原方案已提） |
| `tests/test_buy_signal_bridge.py` | 240, 246, 252, 259 | `_verify_token` × 4 | 测试（原方案未提） |
| `tests/test_live_trader_smoke.py` | 614 | `_takeover_positions` | 测试（原方案未提） |
| `tests/test_live_trader_audit.py` | 112, 159 | `_state, app` × 2 | 测试（原方案未提） |
| `tests/test_manual_order.py` | 21, 27, 34, 36 | `main_mod`, `main_mod._is_local`, `main_mod._state.clear/update` | 测试（原方案未提） |
| `app/live_trader/scheduler.py` | 436, 555 | `process_buy_signals`, `_state` | **生产**（原方案未提） |

**原方案原话**："全仓库搜索，只有 scripts/poc_dryrun_e2e.py import 了 main 的内部函数" + "影响面：1 行"。**这两句都与事实不符**。

**实际需要的 re-export 别名清单（≥6 个符号）**：
```python
# main.py 末尾保留兼容别名（拆完所有阶段后）
from ._state import state as _state  # dict 引用透明，测试 monkey-patch 仍 work
from .auth import _verify_token, _is_local, _require_admin
from .lifecycle import _takeover_positions, _cleanup_dryrun_residue
from .lifecycle import _cleanup_zombies, _kill_all_subprocesses
# process_buy_signals 不建议留别名，scheduler 应直接 import service
```

**对测试的影响**：
- `test_manual_order.py:34` `main_mod._state.clear()` / `:36` `update(saved)` —— 只要 main.py 顶部 `from ._state import state as _state`，`main_mod._state` 还是同一个 dict 引用，clear/update 生效，**测试不破**。
- `test_manual_order.py:27` `main_mod._is_local = lambda request: True` —— monkey-patch 的是 main 模块命名空间的 `_is_local` 名字。如果路由函数内部是 `from .auth import _is_local`（绑定到 routers 模块），patch `main_mod._is_local` **失效**，测试会破。**这是隐藏最深的坑**。
- 缓解：阶段 0b 抽 auth 后，测试改用 `unittest.mock.patch("app.live_trader.routers.trade._is_local", ...)` 或 `app.live_trader.auth._is_local`。

### R3 [MEDIUM] `_state` 里混了跨请求可变业务状态（mode_switching）

**证据**：
- `main.py:1421` `_ms = _state.get("mode_switching")`
- `main.py:1424` `_state["mode_switching"] = _time.time()`
- `main.py:1473` `_state["mode_switching"] = 0`

**问题**：`mode_switching` 是 set_mode 路由的并发互斥标志（60s 超时自动恢复防死锁），是**跨请求可变业务状态**，不是组件装配。它和 store/qmt/config 等单例组件混在同一个 dict 里，架构上是味道。

**抽 _state.py 的影响**：无功能影响（仍是同一 dict），但建议抽 _state.py 时把这种"运行时业务标志"单独归类或留注释。

### R4 [MEDIUM] 模块级可变状态散落（`_instrument_name_cache` / `_last_test_notify`）

**证据**：
- `main.py:506` `_instrument_name_cache: dict = {}` —— 被 L509 `_resolve_instrument_name` 和 `positions` 路由（L552）+ `get_risk_status` 路由（L1709）共用
- `main.py:1185` `_last_test_notify = {"ts": 0.0, "_lock": threading.Lock()}` —— 被 `test_notification` 路由（L1188）用

**拆分影响**：
- `_instrument_name_cache` 跨 market router 和 trade router 的 `get_risk_status` 共用，**拆 routers 时必须明确归属**，否则两个 router 各自 import 会导致缓存分裂（一份内存两份副本，命中率为 0）。
- `_last_test_notify` 必须跟 `/live/notifications/test` 路由一起搬到 system router。

### R5 [LOW] 原方案说"无 pytest 覆盖"不准确

**证据**：`tests/test_live_trader_*.py` 和 `tests/test_buy_signal_bridge.py`、`tests/test_manual_order.py` 共 4+ 个测试文件存在，覆盖了 `_takeover_positions`、`_verify_token`、`/live/order`、`/live/positions`、`/live/config/*` 等关键路径。

**修正**：风险登记应改为"**有 pytest 但覆盖不全，且测试依赖 main 内部符号**（见 R2），拆分时必须连同测试一起改/跑"。

---

## 三、对原方案技术判断的认同/存疑/反对清单

| 原方案判断（出处） | 我的判断 | 理由 / 证据 |
|---|---|---|
| §3.1 "全局对象用 _state 字典，最好拆的一种" | **认同** | 60+ 处 `_state.get("xxx")` 模式让依赖显式且可替换，dict 引用透明使抽 _state.py 改造面 = 1 行。 |
| §3.2 "外部依赖只有 1 处，1 行别名兜底" | **反对** | 实际 ≥6 处（含 scheduler 生产路径 + 5 个测试文件），需 re-export ≥6 个符号。证据见 R1/R2。 |
| §3.3 "两个同名 sync_positions 是埋着的雷" | **认同** | L557 / L1133 Python 后定义覆盖前者。 |
| §3.3 "改名不影响路由注册" | **认同** | FastAPI 用函数对象注册，与函数名无关。 |
| §4.1 "抽 _state.py 避免 main 提前导入" | **认同** | 抽独立模块是规避 main↔routers 循环 import 的正解。 |
| §4.1 目录结构（routers/services/lifecycle/auth 分层） | **认同** | 分层合理，符合 SRP。 |
| §5 阶段 0→1→2 顺序 | **存疑** | 大体对，但漏了"auth 必须先于路由抽"+"scheduler.py 反向依赖必须同步改"。详见问题 5/6 + R1。 |
| §3.4 / §5 "技术可行，零特殊改造" | **存疑** | 主体可行，但"零特殊改造"过乐观。re-export ≥6 处；测试 monkey-patch `_is_local` 需同步改；scheduler import 需同步改。 |
| §3.4 "实盘风险中等，无 pytest 覆盖" | **存疑** | 项目有 4+ 个 pytest 文件，不是"无 pytest"。应改为"有 pytest 但覆盖不全，且测试依赖 main 内部符号"。 |
| §4.3 "lifespan 留 main.py" | **认同（阶段 1）/ 存疑（阶段 2）** | 阶段 1 留 main 合理；阶段 2 抽 lifecycle.py 时必须连 `_cleanup_zombies` 等一起搬。 |
| §5 "阶段 1 跑稳一周后再做阶段 2" | **认同** | 两步走策略对，降回归风险。 |
| 附录 A "lifespan 装配顺序依赖" 风险 | **认同** | 文档标"中"等级合理。 |

---

## 四、PASS / FAIL / WARNING 分类汇总

| 维度 | 结论 | 说明 |
|---|---|---|
| _state 抽 _state.py 避免循环 import | **PASS** | dict 引用透明 + 路由守卫齐全，不会启动即崩。 |
| FastAPI 路由注册与 lifespan 解耦 | **PASS** | 路由函数体 import 时不执行，无启动期读空 _state 风险。 |
| 同名 sync_positions 改名 | **PASS** | 函数对象注册，与函数名无关。 |
| 组件传递方式（_state vs app.state vs Depends） | **PASS** | 原方案选的 `from ._state import state` 是最优。 |
| lifespan 位置 | **WARNING** | 阶段 1 留 main OK；阶段 2 搬 lifecycle.py 必须连 `_cleanup_zombies` 等一起搬。 |
| 阶段顺序 | **WARNING** | 漏 auth 必须先于路由 + scheduler 反向依赖必须同步改。 |
| 外部依赖盘点 | **FAIL** | 原方案"只有 1 处"严重低估，实际 ≥6 处含生产路径。 |
| 测试兼容性 | **WARNING** | dict 引用透明保 `_state` patch 不破；但 `_is_local` 等 monkey-patch 需测试同步改。 |
| 实盘风险描述 | **WARNING** | "无 pytest"不准确，实际有但不全。 |

---

## 五、总体结论

**这套拆法技术上能落地，落地后不会埋雷，但前提是补上原方案遗漏的 3 件事**：

1. **补 R1**：抽 service 阶段，scheduler.py 的 `from .main import process_buy_signals`（L436）和 `from .main import _state`（L555）必须**同步改**为直接 import 新路径，不要靠 main re-export 兜底生产路径。re-export 仅用于外部脚本和测试。

2. **补 R2**：阶段 0 必须先抽 `auth.py`，再抽路由（顺序：0a _state → 0b auth → 1 routers → 2 lifecycle → 3 services）。main.py 末尾保留 ≥6 个 re-export 别名。

3. **补 R4/R5**：拆 routers 时 `_instrument_name_cache`（L506，跨 router 共用）和 `_last_test_notify`（L1185）必须明确归属并整体搬迁；测试 monkey-patch `_is_local` 的代码要同步改。

**做完这 3 件事后**：拆分在架构上是干净的，不会引入新的循环 import，不会启动即崩，不会改变任何路由的运行时行为。冒烟清单需补一条："跑 pytest tests/ 全套，确认 0 失败"。

**不建议做的优化**（YAGNI）：
- 不要把 `_state` 改成 `app.state` 或 Depends —— 改造面巨大无收益。
- 不要把 lifespan 改成 app_factory 模式 —— 当前规模不需要。
- 不要试图把 `_state` 拆成多个细粒度容器 —— OverEngineering。

**关键文件路径**：
- 待审文档：`e:/1target/p9_project/quant-platform/docs/EVAL-live_trader_main拆分评估_2026-07-19.md`
- 主目标：`e:/1target/p9_project/quant-platform/app/live_trader/main.py`（1930 行）
- 隐藏生产依赖：`e:/1target/p9_project/quant-platform/app/live_trader/scheduler.py:436,555`
- 测试依赖（5 个）：`tests/test_manual_order.py:21,27,34,36` / `tests/test_live_trader_audit.py:112,159` / `tests/test_buy_signal_bridge.py:240-259` / `tests/test_live_trader_smoke.py:614`
- 外部脚本：`scripts/poc_dryrun_e2e.py:94`
