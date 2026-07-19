# 代码质量审查：main.py 拆分（阶段 0a/0b/1.1）

> 审计员：code-reviewer agent（Read/Grep 实际验证，禁止派子 agent）
> 日期：2026-07-19
> 范围：本次拆分改动（_state.py / auth.py / routers/__init__.py / routers/system.py 新建 + main.py 删 9 路由 + scheduler.py:555 + test_manual_order patch）
> **排除安全维度**（用户指示不审）

---

## 总体结论：WARNING（可合并，建议先清理 2 个死 import）

| 严重级 | 数量 | 状态 |
|---|---|---|
| CRITICAL | 0 | PASS |
| HIGH | 0 | PASS |
| MEDIUM | 2 | WARN（死 import，应清理） |
| LOW | 3 | INFO |

拆分边界清晰（dict 引用机制、循环 import 规避、模块级状态归属、测试 patch 同步全部正确），9 路由 + 3 鉴权函数 + `_last_test_notify` + `_state` 从 main.py 删除干净无残留，同名函数治理到位，docstring 质量高。**必须改的质量问题：无 CRITICAL/HIGH**。

---

## FINDINGS

### [MEDIUM-1] 死 import：`import signal`（本次拆分导致）
- **文件**：`app/live_trader/main.py:8`
- **证据**：原 `/shutdown` 用 `os.kill(os.getpid(), signal.SIGINT)`，该路由本次搬到 `routers/system.py`。grep `signal\.SIG` 在 main.py 全文无匹配；line 951 `code = signal.code` 与 line 969 `price = signal.price` 中的 `signal` 是 `_process_one_signal(signal, ...)` 的**函数参数变量名 shadow**，非 stdlib 模块属性。stdlib `signal` 在 main.py 已无真实引用者。
- **修复**：删除 `app/live_trader/main.py:8` 的 `import signal`。

### [MEDIUM-2] 死 import：`import threading`（本次拆分导致）
- **文件**：`app/live_trader/main.py:10`
- **证据**：原 `_last_test_notify = {"ts": 0.0, "_lock": threading.Lock()}` 用顶部 `threading`，该状态本次搬到 `routers/system.py`。main.py 顶部 `threading.` 已无使用者；line 1596 `_spawned_lock = _threading.Lock()` 用的是 line 1593 `import threading as _threading` 的别名，独立于顶部 import。
- **修复**：删除 `app/live_trader/main.py:10` 的 `import threading`（line 1593 的 `import threading as _threading` 保留）。

### [LOW-1] `_is_local` re-export 纯冗余
- **文件**：`app/live_trader/main.py:29`
- **证据**：grep `_is_local` 在 main.py 只有 line 29 的 import，**main.py 内部零使用**（`_require_admin` 已在 auth.py 内部闭环调 auth._is_local）。`_verify_token`（buy_signal 用）和 `_require_admin`（6 处路由用）的 re-export 必要，但 `_is_local` 不必要。测试侧已改 patch `auth_mod._is_local`，不再依赖 main re-export。
- **修复**：改为 `from .auth import _verify_token, _require_admin`。

### [LOW-2] `__file__` 四层 dirname 脆弱
- **文件**：`routers/system.py`（sync/intra + sync/index_daily 两处）
- **证据**：`os.path.dirname(...)` 套 4 层，纯靠数层数维护。两处完全重复。
- **修复**：提模块级常量 `_PROJECT_ROOT = Path(__file__).resolve().parents[3]`，两处改 `str(_PROJECT_ROOT / "qmt_sync_job.py")`。非阻塞。

### [LOW-3] 函数内 `from ..main import ...` 为已知技术债
- **文件**：`routers/system.py`（`_takeover_positions` / `_cleanup_zombies` / `_spawned_*`）
- **证据**：过渡策略，docstring 写明"阶段 2 搬 lifecycle.py 后改 import 源"。Python module 有 sys.modules 缓存，性能可忽略；但若阶段 2 不执行则成永久欠债。
- **修复**：阶段 2 搬 lifecycle 时一并清理。

---

## 正向确认（拆分干净，无遗漏）

| 检查项 | 结论 |
|---|---|
| 9 路由删干净 | PASS（main.py 全 no matches） |
| 3 鉴权函数删干净 | PASS（仅在 auth.py 定义） |
| `_last_test_notify` 删干净 | PASS（搬到 system.py，SRP 归属正确） |
| `_state` dict 引用机制 | PASS（lifespan update 对所有 import 方可见） |
| 循环 import 规避 | PASS（routers 顶层不绕回 main） |
| scheduler.py 改动 | PASS（相对 import 正确） |
| 测试 patch 同步 | PASS（全 tests 无遗漏 main._is_local patch） |
| test_buy_signal_bridge 兼容 | PASS（靠 re-export 工作，无需改） |
| 同名函数治理 | PASS（admin/full 改名到位，main 无残留） |
| logger 命名一致性 | PASS（`live_trader.auth` / `live_trader.routers.system`） |
| docstring 质量 | PASS（历史行号/依赖约定/阶段计划齐全） |
| `@app`→`@router` 转换彻底 | PASS |
| import 分组（system.py） | PASS（stdlib→third-party→local） |

---

## 不在本次范围的发现（仅记录）

- `main.py:14` `from typing import Optional` 与 `main.py:18` `from pydantic import BaseModel` 在 main.py 内部均无使用，但 git diff 显示这两个 import **非本次拆分引入**（属会话前已存在的未提交改动），不在本次审查范围。

---

## 修复记录（2026-07-19，主控据本报告迭代）

二次 grep 确认 signal/threading 死 import 结论属实后，逐条处理：

| Finding | 处理 | 验证 |
|---|---|---|
| MEDIUM-1 死 `import signal` | ✅ 删 main.py:8 | py_compile + pytest 81 过 |
| MEDIUM-2 死 `import threading` | ✅ 删 main.py:10 | 同上 |
| LOW-1 `_is_local` 冗余 re-export | ✅ main.py:29 去掉 `_is_local`，改 `from .auth import _verify_token, _require_admin` | grep 确认 main 仅 L27 注释残留 |
| LOW-2 `__file__` 四层 dirname 脆弱 | ✅ system.py 提常量 `_PROJECT_ROOT = Path(__file__).resolve().parents[3]`，sync/intra + sync/index_daily 两处复用 | `_PROJECT_ROOT` 路径验证通过（qmt_sync_job.py / qmt_sync_index_job.py 都找到）|
| LOW-3 函数内 `from ..main import` 技术债 | ⏳ 留阶段 2（搬 lifecycle.py 时清理）| 已登记，docstring 有注明 |

修复后：py_compile 通过 / `_PROJECT_ROOT` 路径正确 / pytest 4 文件 81 用例全过。**无回归**。
