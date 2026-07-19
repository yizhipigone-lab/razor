# live_trader/main.py 拆分评估报告

> 评估日期：2026-07-19
> 评估对象：`app/live_trader/main.py`（1930 行 / 80KB / 40 个 HTTP 路由）
> 结论：**可行、有必要、风险可控**（前提：分阶段 + 逐接口冒烟）
> 当前阶段：**仅评估，未动代码**。本报告是拍板依据，不是执行记录。

---

## TL;DR（一句话）

main.py 把**启动、接管、40 个路由、核心业务服务、鉴权、进程清理**六件不相关的事塞进一个 1930 行的文件，是你项目自定"800 行红线"的 **2.4 倍**。它本身设计不烂（全局对象走模块级 `_state` 字典，非常好拆），但已经到了"再往里加东西就容易出事"的临界点。建议**分两阶段拆**，先抽路由，再抽服务。

---

## 1. 现状画像

### 1.1 体量

| 指标 | 数值 | 红线 |
|---|---|---|
| 总行数 | 1930 | 800 max（CLAUDE.md） |
| 文件大小 | 80KB | — |
| HTTP 路由数 | 40 个 `@app.xxx` | — |
| 顶层函数/方法 | ~25 个 | — |
| 超标倍数 | **2.4x** | — |

### 1.2 六大职责混在一起

| 职责 | 代表符号 | 大致行段 |
|---|---|---|
| 启动 + lifespan | `lifespan()` / `_check_port_in_use` / `_acquire_lock` | ~270 行（L29–L273） |
| 接管/清理 | `_takeover_positions` / `_cleanup_dryrun_residue` | ~200 行（L274–L477） |
| 40 个 HTTP 路由 | 全部 `@app.xxx` | 散落全程 |
| 核心业务服务 | `place_order_service` / `process_buy_signals` / `_process_one_signal` | ~200 行 |
| 鉴权 | `_verify_token` / `_is_local` / `_require_admin` | ~50 行 |
| 进程清理 | `_cleanup_zombies` / `_kill_all_subprocesses` | ~30 行 |

**比喻**：收银台、仓库、财务室、保安岗全塞一个大开间，谁改哪块都得在 1900 行里翻。

---

## 2. 为什么该拆（好处，人话版）

1. **改一个路由不用翻 1900 行** —— 找 `/live/order` 要从一堆无关代码里捞，拆完直接开 `routers/trade.py` 就在眼前。
2. **能并行改不冲突** —— 近期 git 记录显示 `main.py` 与 `scheduler.py` 高频同时改动，挤一个文件容易撞车；拆开后改下单逻辑和改配置接口互不干扰。
3. **职责清晰，定位 bug 快** —— 出问题第一眼就知道开哪个文件，不用 grep 半天。
4. **符合自家规矩** —— CLAUDE.md 白纸黑字"800 max"，当前是压不住的红线违反。
5. **重构风险低** —— 小文件改动影响面一眼看穿，大文件一动就怕牵一发动全身。
6. **附赠收益** —— 拆分时能顺手清掉一个历史坏味道（见 §3.3）。

---

## 3. 可行性技术评估（"行不行"的硬依据）

### 3.1 ✅ 全局对象用 `_state` 字典 —— 最好拆的一种

main.py 的所有运行时组件（store / qmt / config / audit / runtime_state 等）都塞在**模块级 `_state` 字典**里，路由函数通过 `_state.get("xxx")` 取用：

```python
# main.py 现状（路由内部）
store = _state.get("store")
qmt  = _state.get("qmt")
config = _state.get("config")
```

拆分时把 `_state` 抽到独立 `_state.py`，子路由 `from ._state import state as _state` 即可原样拿到。**注意：不能 `from .main import _state`** —— 那会触发 main↔routers 循环 import（main 顶部要 `app=FastAPI()` + `include_router` 必须先于 routers import 执行，routers 再回头 import main 会拿到半初始化模块）。dict 是引用类型，`from X import Y` 绑定对象引用而非快照，lifespan 在 main 里 `_state.update({...})` 写入的就是 `_state.py` 里那个 dict，所有 import 它的模块同时看到新值。这是判断可行性的关键事实。

### 3.2 ⚠️ 外部依赖盘点：6 处（含 2 处生产路径）

> **🔴 审计修订（2026-07-19）**：本节原写"只有 1 处、1 行别名"，经 code-reviewer + architect 双 agent 独立审计核实为**事实错误**，实际 6 处且含生产代码。已据实重写。这是本次评估最关键的修正。

grep 全仓库 `from app.live_trader.main import` / `from .main import`，共 **6 处导入点**：

| 文件:行 | 导入符号 | 性质 |
|---|---|---|
| `scripts/poc_dryrun_e2e.py:94` | `_takeover_positions` | 外部脚本 |
| `app/live_trader/scheduler.py:436` | `process_buy_signals` | **🔴 生产（auto_buy 定时下单）** |
| `app/live_trader/scheduler.py:555` | `_state` | **🔴 生产（通知清理）** |
| `tests/test_live_trader_smoke.py:614` | `_takeover_positions` | 测试 |
| `tests/test_live_trader_audit.py:112,159` | `_state`, `app` | 测试 |
| `tests/test_buy_signal_bridge.py:240-259` | `_verify_token` ×4 | 测试 |
| `tests/test_manual_order.py:21,27,34,36` | `main_mod._is_local`, `_state` | 测试 |

**最致命**：`scheduler.py:436/555` 是 lifespan 装配的生产调度器（`main.py:74/247`）反向 import main。main.py ↔ scheduler.py 是**双向耦合**（正向装配注入 + 反向运行时导入）。拆分时若只补外部脚本别名、漏改这两处，**下次定时任务（auto_buy 14:50 / 通知清理 15:35）触发即 ImportError，线上事故**。

**处理策略（两类不同处置）**：

- **生产路径（scheduler.py 2 处）—— 必须同步改 import 源**到新路径，**不能靠 main 留别名兜底**：
  - `scheduler.py:436` → `from .services.signal_service import process_buy_signals`
  - `scheduler.py:555` → `from ._state import state as _state`
  - 理由：re-export 别名是技术债，未来谁清理了它，崩溃只在定时任务触发时才暴露，发现极晚。
- **测试 + 外部脚本 —— main.py 末尾留 re-export 别名兜底**（改动面最小）：

```python
# main.py 末尾保留兼容别名（仅给测试和外部脚本，不给生产路径）
from ._state import state as _state            # noqa: E402,F401
from .auth import _verify_token, _is_local, _require_admin   # noqa: E402,F401
from .lifecycle import (_takeover_positions, _cleanup_dryrun_residue,  # noqa: E402,F401
                        _cleanup_zombies, _kill_all_subprocesses)
```

**隐藏坑（测试 monkey-patch）**：`test_manual_order.py:27` 用 `main_mod._is_local = lambda...` 打桩。若路由内部改成 `from .auth import _is_local`，patch main 模块的 `_is_local` 名字会**失效**（因为路由绑定的是 auth 模块的名字）。测试须同步改为 `patch("app.live_trader.routers.trade._is_local", ...)` 或 `patch("app.live_trader.auth._is_local", ...)`。

影响面：**scheduler.py 改 2 处 + main.py re-export ~6 行 + 1 处测试 patch 路径调整**（远超原估的"1 行"）。

### 3.3 ⚠️ 附带发现：两个同名函数 `sync_positions`（历史坏味道）

main.py 里有两个**同名**函数：

| 行号 | 路由 | 签名 | 语义 |
|---|---|---|---|
| L557 | `POST /live/positions/sync` | `sync_positions(request, body=None)` | 需 admin，支持单只 code |
| L1133 | `POST /live/sync-positions` | `sync_positions()` | 全量同步 |

Python 后定义的会覆盖前者的函数名绑定。**当前不出 bug** 是因为两者都靠 `@app.post` 装饰器在定义时立刻注册了路由，没人通过函数名调用。但这是埋着的雷 —— 任何人一旦 `from main import sync_positions` 拿到的都是 L1133 那个，与路由 `/live/positions/sync` 实际处理的不是同一个函数。

**拆分时顺手治理**：拆到 `routers/trade.py` 时分别命名为 `sync_positions_admin` 和 `sync_positions_full`，或合并成一个带 `code=None` 参数的函数。这是拆分的附赠收益。

### 3.4 可行性结论

> **审计修订（2026-07-19）**：原表"外部影响 1 处/无 pytest/工作量 1.5 天"经双 agent 审计核实有误，已修正。

| 维度 | 评估 |
|---|---|
| 技术可行 | ✅ `_state` dict + FastAPI APIRouter 标准拆法（非"零改造"，见 §3.2/§3.1） |
| 外部影响 | ⚠️ **6 处依赖**（含 scheduler 2 处生产路径），详见 §3.2 |
| 实盘风险 | 🟡 **中低** —— 有 **1399 行 pytest** 兜底（smoke 767 + audit 207 + buy_signal_bridge 425），但覆盖不全且测试依赖 main 内部符号，拆分须连同测试一起改/跑 |
| 工作量 | 轻拆 ~半天 / 重拆 **~2 天**（含 scheduler 协同改 + 测试修正，原估 1.5 天偏低） |

---

## 4. 拆分方案

### 4.1 目标目录结构

```
app/live_trader/
├── main.py              # 只剩启动骨架 + _state + lifespan（~250 行）
├── _state.py            # 模块级 _state 字典（抽出来避免循环 import）
├── routers/
│   ├── __init__.py
│   ├── market.py        # 行情/持仓/订单/成交/净值/股票池查询
│   ├── trade.py         # 下单/撤单/信号/kill-switch/对账/退出扫描
│   ├── config_api.py    # /live/config/* 热加载一组
│   └── system.py        # 通知/同步/health/shutdown/stocklist
├── services/
│   ├── __init__.py
│   ├── order_service.py # place_order_service
│   └── signal_service.py# process_buy_signals / _process_one_signal
├── lifecycle.py         # _takeover_positions / _cleanup_dryrun_residue / zombie 清理
├── auth.py              # _verify_token / _is_local / _require_admin
└── (其余已有文件不动：store.py / scheduler.py / risk_gate.py ...)
```

> ⚠️ `_state` 建议单独抽到 `_state.py`，否则 routers 子模块 `from .main import _state` 会让 main.py 被提前导入，可能引发循环依赖。抽成独立模块后所有路由 `from ._state import state` 即可。

### 4.2 路由分组（40 个 → 4 个 router）

#### routers/market.py（行情/查询类，9 个路由）

| 路由 | 行号 |
|---|---|
| `GET /live/status` | L478 |
| `GET /live/asset` | L495 |
| `GET /live/positions` | L529 |
| `GET /live/orders` | L584 |
| `GET /live/deals` | L597 |
| `GET /live/quotes` | L650 |
| `GET /live/equity` | L1733 |
| `GET /live/stocklist` | L1782 |
| `GET /live/index/members` | L1831 |

#### routers/trade.py（交易动作类，10 个路由）

| 路由 | 行号 | 备注 |
|---|---|---|
| `POST /live/order` | L711 | 依赖 `place_order_service` (L801) |
| `POST /live/order/cancel` | L822 | |
| `POST /live/cancel-by-source` | L1086 | |
| `POST /live/buy-signal` | L1044 | 依赖 `process_buy_signals` (L860) + `_verify_token` (L961) |
| `GET /live/buy-signal/pending` | L1127 | |
| `POST /live/kill-switch/activate` | L661 | |
| `POST /live/kill-switch/deactivate` | L672 | |
| `POST /live/reconcile` | L683 | |
| `POST /live/exit-scan` | L691 | |
| `GET /live/audit/replay/{order_id}` | L701 | |

#### routers/config_api.py（配置热加载，12 个路由 / 7 个语义端点）

| 路由 | 行号 |
|---|---|
| `GET/PUT /live/config/scan-interval` | L1240 / L1249 |
| `GET/PUT /live/config/auto-buy-time` | L1273 / L1282 |
| `GET/PUT /live/config/buy-ratio` | L1304 / L1314 |
| `GET/PUT /live/config/switches` | L1361 / L1370 |
| `GET/POST /live/config/mode` | L1400 / L1409 |
| `GET /live/config/risk-params` | L1481 |
| `GET /live/config/risk-status` | L1491 |

#### routers/system.py（通知/同步/系统，9 个路由）

| 路由 | 行号 | 备注 |
|---|---|---|
| `GET /live/health` | L1152 | |
| `POST /shutdown` | L1209 | 优雅关闭，依赖 `_cleanup_zombies` |
| `GET /live/notifications` | L1160 | |
| `GET /live/notifications/summary` | L1175 | |
| `POST /live/notifications/test` | L1189 | |
| `POST /live/positions/sync` | L557 | ⚠️ 同名，拆时改名 `sync_positions_admin` |
| `POST /live/sync-positions` | L1133 | ⚠️ 同名，拆时改名 `sync_positions_full` |
| `POST /live/sync/intra` | L1866 | |
| `POST /live/sync/index_daily` | L1896 | |

> 路由分组总和 9(market) + 10(trade) + 12(config) + 9(system) = **40**，与 main.py 实际 40 个 `@app` 装饰器完全对齐。config_api 的 12 是 GET/PUT 各算一个（7 个语义端点）。

### 4.3 留在 main.py 的（启动骨架，~250 行）

- `app = FastAPI(lifespan=lifespan)`
- `_check_port_in_use` / `_acquire_lock`
- `lifespan()`（装配所有组件塞进 `_state`）
- 各 router 的 `app.include_router(...)`
- `__main__` 启动入口

---

## 5. 分阶段落地顺序（风险从低到高）

> **核心原则：每抽一组立刻冒烟，绝不批量搬完再验。**

### 阶段 0a —— 抽 `_state.py`（0.5h）✅ 已完成（2026-07-19）

> **执行记录**：新建 `app/live_trader/_state.py`（空 dict + 机制/历史 docstring）；`main.py:26` 原 `_state: dict = {}` 改为 `from ._state import state as _state`；`scheduler.py:555` 生产路径 `from .main import _state` 改为 `from ._state import state as _state`。
> **验证**：py_compile 三文件通过 / `import` 链通 / **`_state is state` 身份验证 True**（证明是同一 dict 对象）/ app 路由 44 个正常 / **pytest 4 个测试文件 81 用例全过**（test_manual_order + test_live_trader_audit + test_buy_signal_bridge + test_live_trader_smoke）。
> **遗留（按计划留后续阶段）**：`test_manual_order.py:27` 的 `_is_local` patch 未改（阶段 0b 抽 auth 时处理）；`scheduler.py:436` 的 `process_buy_signals` 反向 import 未改（阶段 3 处理）。

1. 建 `routers/` `services/` 目录骨架 + `__init__.py`
2. 抽 `_state.py`（纯字典搬迁：`state: dict = {}`）
3. main.py 改 `from ._state import state as _state`
4. **🔴 同步改 `scheduler.py:555`** → `from ._state import state as _state`（生产路径，不能靠 main 别名兜底）
5. **冒烟**：服务能起、`/live/health` 正常、通知清理定时任务不报 ImportError

### 阶段 0b —— 抽 `auth.py`（必须先于路由，防循环 import）✅ 已完成（2026-07-19）

> **执行记录**：新建 `app/live_trader/auth.py`（`_verify_token` / `_is_local` / `_require_admin` 三纯函数 + 独立 logger，hmac 提到模块顶部）；main.py 顶部加 `from .auth import _verify_token, _is_local, _require_admin`（re-export），删除三函数原定义（净减约 26 行）；`test_manual_order.py` 加 `import auth as auth_mod`，patch 目标从 `main_mod._is_local` 改为 `auth_mod._is_local`。test_buy_signal_bridge 的 8 处 `from main import _verify_token` 靠 re-export 兜底，**未改**。
> **验证**：py_compile 通过 / **三函数 `main.X is auth.X` 身份一致** / **`_require_admin.__globals__ is auth.__dict__`** 证明内部 `_is_local` 绑定 auth 模块（patch auth._is_local 真正生效，architect R2 的坑已填）/ **pytest 4 文件 81 用例全过**（含 TestTokenVerification 4 个 token 分支 + 所有 _require_admin 路由不再误 403）。
> **收益**：阶段 1 抽路由时 routers 可 `from .auth import _require_admin` 不绕回 main，循环 import 已解除。

> **🔴 审计修订（architect）**：原方案漏了这一步。多条路由内部调 `_require_admin` / `_is_local` / `_verify_token`（`main.py:564,829,1191,1217,1251,1369,1409`）。若路由先抽、routers 顶层 `from .main import _require_admin` 会回到 main↔routers 循环 import。必须先抽 auth，routers 才能 `from .auth import ...` 不绕回 main。

1. 抽 `auth.py`：`_verify_token` / `_is_local` / `_require_admin`（纯函数，最安全）
2. main.py 留 `from .auth import _verify_token, _is_local, _require_admin`（re-export 给测试）
3. **同步改测试**：`test_manual_order.py:27` 的 `main_mod._is_local = lambda...` 打桩改为 `patch("app.live_trader.auth._is_local", ...)`（否则路由改 `from .auth import _is_local` 后 patch 失效）
4. **冒烟**：`pytest tests/test_buy_signal_bridge.py::TestTokenVerification -v` 全绿

### 阶段 1 —— 轻拆：抽路由（~半天）

> **进度（2026-07-19）**：✅ **第 1 步 system router 已完成**（9 路由：positions/sync, sync-positions, health, notifications×3, shutdown, sync/intra, sync/index_daily）。main.py **1899 → 1711 行（-188）**，app 总路由 44 不变（删 9 加 9），pytest 4 文件 81 用例全过。
> ⚠️ **`__file__` 路径陷阱已处理**：sync/intra + sync/index_daily 的 script_path 从三层 dirname 改四层（routers/system.py 比原 main.py 多一层），已验证 `qmt_sync_job.py` / `qmt_sync_index_job.py` 在算出的项目根存在。
> 依赖策略兑现：顶部 import `_state` + `auth`（独立模块），函数内 import `_takeover_positions` / `_cleanup_zombies` / `_spawned_*`（main 工具，阶段 2 搬 lifecycle 后改 import 源）。⏳ 剩 market / config_api / trade 三步。

> **进度续（2026-07-19）**：✅ **第 2 步 market router 已完成**（9 路由：status/asset/positions/orders/deals/quotes/equity/stocklist/index_members）。main.py **1711 → 1478 行（-233）**，app 总路由 44 不变，pytest 4 文件 81 用例全过。
> **`_instrument_name_cache` 归属**：positions(market) 与 get_risk_status(留 main) 共用此 cache，故 cache + `_resolve_instrument_name` **暂留 main.py**，market 的 positions 用函数内 `from ..main import _resolve_instrument_name`。等 get_risk_status（阶段 1.3/1.4）搬走时统一归属（避免 architect R4 警告的缓存分裂）。
> **过程小插曲（诚实记录）**：删 positions/orders/deals/quotes 时一次 Edit 把 old/new 写反成"插入"，导致这 4 路由各出现 2 份。grep 立即发现，用 `replace_all` 一次删两份修复。最终干净 —— "改完立刻验证"规则救场。
> ⏳ 剩 config_api（12 路由）/ trade（10 路由）两步。

> **进度续 2（2026-07-19）**：✅ **第 3 步 config_api router 已完成**（12 路由：scan-interval/auto-buy-time/buy-ratio/switches/mode 各 GET+PUT/POST + risk-params/risk-status，含 get_risk_status 大函数）。main.py **1478 → 1007 行（-471）**，app 总路由 44 不变，pytest 4 文件 81 用例全过。
> **顺手查 `import asyncio` 是否因 set_mode 搬走变死**：main 还有 4 处真实使用（process_buy_signals 的 Semaphore/gather + place_order 的 to_thread），**保留**。这种"删路由后查相关 import 是否变死"是拆分必须的尾巴清理。
> get_risk_status 用函数内 import `_resolve_instrument_name`（与 positions 一致）；cache + 函数定义仍留 main（现在 main 内部 0 使用，纯给 market/config_api 两个 router 函数内 import，阶段 2 统一归属到 utils 或 lifecycle）。
> 本步 0 失误（吸取 1.2 的 Edit 写反教训，4 个 Edit 方向全部核对正确）。
> ⏳ 剩 trade（10 路由，风险最高，放最后）一步即可达成阶段 1 目标 ~800 行。

> **进度续 3（2026-07-19）**：✅ **第 4 步 trade router 已完成（阶段 1 收官）**。按计划书（经 code-reviewer + architect 双 agent 审计 + 迭代 12 条 finding）实施，10 路由搬到 routers/trade.py，3 核心服务（place_order_service / process_buy_signals / _process_one_signal）留 main。main.py **1007 → 741 行（-266）**，app 总路由 44 不变，pytest 4 文件 81 用例全过。
> **验证清单 8 项全过**：py_compile / 核心服务可 import / 10 路由注册 / asyncio 4 处非死 / **trade.py 相对 import 单点 0 命中（R5 双点修复验证）** / main 残留 0 / pytest 81 过。
> **严格兑现审计修订**：R5 相对 import 双点（`from ..schemas`/`..price_type`）/ R1 dry-run 403 检查保留在 service 调用前 + `source='WEB'/lock_wait=30` 一字不动 / W4.2 seq>0 守卫保留 / W1.3 buy-signal HTTP 层 4 步整段搬。
> 本步 0 失误（4 个删除 Edit 方向全部核对正确，吸取 1.2 教训）。
> ⚠️ **诚实交代**：test_manual_order 走 mock executor（审计 W6.2），**真实 QMT 下单链路不在测试覆盖**。代码层验证 OK，但真实 place_order/buy-signal 链路需人工在实盘环境 dry-run 切 live 冒烟（由用户操作）。
> 🎉 **阶段 1 收官**：main.py **1930 → 741（-1189 行，-62%）**。40 路由全部拆到 4 个 router（system/market/config_api/trade）。剩 main 的是 lifespan + 3 核心服务 + lifecycle 工具函数（阶段 2/3 搬）。

按 **system → market → config_api → trade** 顺序逐个 router 抽（trade 最后，风险最高）：
- 每抽一个 router：
  - 把对应 `@app.xxx` 改成 `@router.xxx`
  - 路由函数体**一字不动**，只搬位置
  - 路由内部用 `from .auth import _require_admin` / `from ._state import state as _state`
  - **模块级缓存归属**（architect R4）：`_instrument_name_cache`（L506，跨 market 和 `get_risk_status` 共用）必须只在一个 router 定义、其他 import，否则缓存分裂；`_last_test_notify`（L1185）跟 `/live/notifications/test` 一起进 system router
  - main.py 加 `app.include_router(...)`
  - **立刻冒烟该 router 的全部接口 + 对应 pytest**

**预期结果**：main.py 从 1930 行降到 ~800 行。**这一步收益最大、风险最低**。

### 阶段 2 —— 抽 `lifecycle.py`（~半天）✅ 已完成（2026-07-19）

> **执行记录**：建 lifecycle.py（6 组函数整组搬：_check_port_in_use / _acquire_lock / lifespan / _takeover_positions / _cleanup_dryrun_residue / 进程管理，logger **沿用 `live_trader.main`** 兑现 R6）；main.py 删 6 组 + 死 import（socket/asynccontextmanager/os/time）+ 加 `from .lifecycle import lifespan, _takeover_positions`（re-export 给 poc/test）；routers/system.py 4 处 import 改源 main→lifecycle。main.py **741 → 283 行（-458）**。
> **验证清单 8/9 项过**：py_compile 3 文件 / **lifespan identity**（main.lifespan is lifecycle.lifespan）/ routes 44 / main 残留 0 / routers import 源 0（4 处全改）/ 死 import 0 / pytest 81 全过。⏳ 第 9 项**人工启动冒烟**（用户实盘验证 lifespan 真实启动：连 QMT + 拿锁 + 装配 + 调度器）。
> **R6 兑现**：lifecycle.py `get_logger("live_trader.main")`，32 处启动/接管/QMT 失败日志 module 字段不变，运维监控不受影响（loguru bind module）。
> **sed 删 lifespan 段**（398 行超长，Edit old_string 不现实）：grep 定位 + preview 确认范围（@asynccontextmanager 到 # ===== FastAPI app =====）+ sed 删 + py_compile/grep 残留验证，0 失误。
> **计划书流程**：经 code-reviewer + architect 双 agent 审计 + 13 处迭代（H1 行号错/M1 组件数/M3 logger 运维盲区 等）后实施。

- 抽 lifespan + `_takeover_positions` + `_cleanup_dryrun_residue` + **`_cleanup_zombies` + `_kill_all_subprocesses` + `_spawned_processes` + `_spawned_lock` 整组搬**
  - **🔴 审计修订（architect）**：lifespan L263 调 `_kill_all_subprocesses`，不整组搬会引用失败；原方案未说清这个耦合
- main.py 顶部 `app = FastAPI(lifespan=lifespan)` 改 `from .lifecycle import lifespan`
- main.py 留 re-export 别名给 poc 脚本和测试
- **冒烟**：`POST /shutdown` 优雅关闭 + 子进程不残留 + 无 WAL 损坏

### 阶段 3 —— 抽核心 services（~半天）✅ 已完成（2026-07-19，拆分收官）

> **执行记录**：建 services/（`__init__.py` + `order_service.py` place_order_service + `signal_service.py` process_buy_signals/_process_one_signal），**函数内 import 4 处双点**（审计 C1/A 兑现 R7）；main.py 删 3 服务 + 死 import（asyncio/date/datetime/HTTPException/Request）；scheduler:436 + routers/trade×2 改源 main→services。main.py **283 → 93 行（-190）**。
> **验证清单 10/10 项过**：py_compile 5 文件 / import + services identity / routes 44 / main 残留 0 / scheduler+trade from main 0 / main 死 import 0 / **services 函数内双点（R7，4 处审计点全 `..` 前缀）** / **pytest 81 全过**（test_buy_signal_bridge 调 process_buy_signals 间接验函数内 import）。⏳ 项9 **人工冒烟**（启服务 + buy-signal + 手工下单 + 14:50 auto_buy + 14:55 心跳）。
> **R7 兑现**：4 处审计点（order_executor/schemas×2/buy_volume）全改 `..` 双点。grep `from \.[a-z_]` 命中是**注释**（"原 main 为 from .schemas" 描述），代码双点正确，pytest 兜底验证。
> **计划书流程**：双 agent 审计抓 CRITICAL（C1/A 函数内 import 前缀，**和 trade R5 同类盲区第二次犯**）+ 8 处迭代后实施。
> 🎉 **拆分收官**：main.py **1930 → 93 行（-95%）**。40 路由拆 4 个 router + lifespan/生命周期搬 lifecycle.py + 3 核心服务搬 services/。main.py 现在只剩启动骨架（app + middleware + include_router + _resolve + __main__）。

- 抽 `services/order_service.py`（`place_order_service`）/ `services/signal_service.py`（`process_buy_signals` / `_process_one_signal`）
- **🔴 同步改 `scheduler.py:436`** → `from .services.signal_service import process_buy_signals`（生产路径，必改，不能靠 main 别名）
- main.py 留 re-export 别名给测试
- main.py 只剩启动骨架 ~250 行
- **冒烟**：`scripts/poc_dryrun_e2e.py` 端到端 + 全接口手点 + auto_buy 定时任务实测触发一次

> 阶段 1 跑稳一周后再做阶段 2/3，可进一步降风险（"两步走"策略）。

---

## 6. 冒烟验证清单（逐接口，实盘必跑）

> 每条都标了"怎么验 + 预期"。拆分后**逐条对照**，任一条行为变化即视为引入回归。

### 6.0 自动化前置（pytest 全绿，所有手点的前提）

> **审计修订（code-reviewer）**：原清单漏了既有 1399 行 pytest。这些测试必须先全绿，且 `test_buy_signal_bridge.py::TestTokenVerification` 已覆盖 §6.3 #16/#17 的 token 分支，不必重复手点。

| # | 验证项 | 怎么验 | 预期 |
|---|---|---|---|
| 0 | pytest 全套 | `pytest tests/test_live_trader_smoke.py tests/test_live_trader_audit.py tests/test_buy_signal_bridge.py tests/test_manual_order.py -v` | **0 失败**。任一失败先修再继续 |

### 6.1 启动与生命周期

| # | 验证项 | 怎么验 | 预期 |
|---|---|---|---|
| 1 | 服务启动 | `python -m app.live_trader.main` | 日志显示"实盘交易模块启动"、文件锁获取成功、无 ImportError |
| 2 | 端口冲突守卫 | 先起一个占 8081 的进程再启 | 启动失败并报"端口 8081 在监听" |
| 3 | lifespan 装配 | 看 `_state` 是否含全部组件 | store/qmt/config/audit/runtime_state 全部非 None |
| 4 | 优雅关闭 | `POST /shutdown` | 进程干净退出，**无 WAL 损坏**（重启 DuckDB 不报 stale lock） |

### 6.2 行情与查询（market router）

| # | 接口 | 预期 |
|---|---|---|
| 5 | `GET /live/health` | 返回 200 + 状态 JSON |
| 6 | `GET /live/quotes?codes=000001` | 返回现价/昨收/涨跌幅，`source` 非 parquet（优先 QMT） |
| 7 | `GET /live/positions` | 返回当前持仓 + 今日盈亏口径正确 |
| 8 | `GET /live/equity?days=1` | 净值数字合理，单日跳变 <15% |
| 9 | `GET /live/orders` / `/live/deals` | 返回最近订单/成交 |
| 10 | `GET /live/stocklist` / `/live/index/members` | 返回股票池/指数成分 |

### 6.3 交易动作（trade router）—— **最高风险**

| # | 接口 | 预期 |
|---|---|---|
| 11 | `POST /live/order`（dryrun 模式） | 不真实下单，返回模拟成交 |
| 12 | `POST /live/order`（实盘模式） | **真实下单成功**，订单进 QMT，审计有记录 |
| 13 | `place_order_service` 直调 | 与 #12 行为一致（被路由复用） |
| 14 | `POST /live/order/cancel` | 撤单成功 |
| 15 | `POST /live/cancel-by-source?terminal=TDX` | 按来源批量撤 |
| 16 | `POST /live/buy-signal`（带 token） | 信号被处理，`_process_one_signal` 走通 |
| 17 | `POST /live/buy-signal`（错 token） | 401 拒绝 |
| 18 | `POST /live/kill-switch/activate` | 风控激活，后续下单被拦 |
| 19 | `POST /live/kill-switch/deactivate` | 风控解除 |
| 20 | `POST /live/reconcile` | 对账不报错 |
| 21 | `POST /live/exit-scan` | 退出扫描触发 |
| 22 | `GET /live/audit/replay/{order_id}` | 回放审计轨迹 |

### 6.4 配置热加载（config_api router）

| # | 接口 | 预期 |
|---|---|---|
| 23 | `PUT /live/config/scan-interval` | 写入后 `GET` 能读回新值 |
| 24 | `POST /live/config/mode` | dryrun/live 切换生效 |
| 25 | `PUT /live/config/switches` | 各开关切换后行为变化 |
| 26 | `GET /live/config/risk-status` | 风控状态正确反映当前持仓/资金 |

### 6.5 通知与同步（system router）

| # | 接口 | 预期 |
|---|---|---|
| 27 | `POST /live/notifications/test` | 测试通知实际送达（企微/飞书） |
| 28 | `GET /live/notifications/summary` | 返回各级别计数 |
| 29 | `POST /live/positions/sync` | 单只/全量同步，保留 peak_price 等扩展字段 |
| 30 | `POST /live/sync-positions` | 全量同步不丢持仓 |
| 31 | `POST /live/sync/intra` | 分时同步 |
| 32 | `POST /live/sync/index_daily` | 指数日线同步 |

### 6.6 外部脚本端到端

| # | 验证项 | 预期 |
|---|---|---|
| 33 | `python scripts/poc_dryrun_e2e.py` | 全流程跑通，`_takeover_positions` 别名 re-export 生效 |

### 6.7 进程清理

| # | 验证项 | 预期 |
|---|---|---|
| 34 | `_cleanup_zombies` / `_kill_all_subprocesses` | 关闭时子进程不残留 |

---

## 7. 不拆的代价 & 决策建议

### 不拆的代价

- 每加一个路由，文件继续膨胀，可读性进一步恶化
- 多人/多 agent 并行改动冲突概率上升
- 同名 `sync_positions` 这类坏味道继续埋着
- 持续违反自家 800 行红线

### 决策建议

| 你的诉求 | 建议 |
|---|---|
| 近期还要往 live_trader 加新接口 | **建议拆**（越加越难拆） |
| 近期只维护不动结构 | 可缓，但建议至少做阶段 0+1 |
| 想要最小风险落地 | **阶段 1 轻拆**，收益/风险比最优 |
| 追求架构最干净 | 阶段 1 + 2 全做，两步走 |

### 触发"必须拆"的红线信号

- main.py 突破 **2500 行**
- 出现第三次"改 A 接口误伤 B 接口"的回归
- 需要多人同时改 live_trader

---

## 附录 A：风险登记

> **审计修订（2026-07-19）**：经 code-reviewer + architect 双 agent 审计，原表"无 pytest/高"失真，且漏报 scheduler 生产依赖这一最高风险项。已重排。

| 风险 | 等级 | 缓解 |
|---|---|---|
| **scheduler.py 生产路径漏改（L436/555）** | **🔴 致命** | 阶段 0a + 阶段 3 同步改 import 源，不靠 main 别名兜底（§3.2/§5） |
| 测试 monkey-patch `_is_local` 失效 | 高 | 阶段 0b 同步改 `test_manual_order.py:27` 的 patch 路径 |
| `_state` 循环 import | 中 | 抽独立 `_state.py`，子路由 `from ._state import`（§3.1） |
| lifespan 装配顺序 + `_kill_all_subprocesses` 耦合 | 中 | 阶段 2 整组搬 lifecycle.py（§5） |
| `_instrument_name_cache` 跨 router 缓存分裂 | 中 | 阶段 1 明确归属，单点定义（§5） |
| 拆分引入回归 | **中低**（原评"高"） | §6.0 pytest 全绿前置 + 全清单逐条冒烟（**有 1399 行 pytest 兜底**） |
| poc 脚本依赖断裂 | 低 | main.py 留 re-export 别名 |
| 同名 `sync_positions` 误绑定 | 低 | 拆时改名 |

## 附录 B：本次评估与审计的边界

- ❌ 未修改任何业务代码（main.py / scheduler.py / 测试 均未动）
- ❌ 未运行任何服务、未跑 pytest（拆分阶段才开始）
- ✅ 仅做静态阅读 + grep 验证（符合审计"读真实代码"原则）
- ✅ 已据两份 agent 审计修正 3 处事实错误（§3.1/§3.2/§3.4/§5/§6/附录A），审计报告归档于：
  - `docs/AUDIT-main拆分方案_文档质量_2026-07-19.md`（code-reviewer，6 维度）
  - `docs/ARCHREVIEW-main拆分方案_技术可行性_2026-07-19.md`（architect，技术深审）
