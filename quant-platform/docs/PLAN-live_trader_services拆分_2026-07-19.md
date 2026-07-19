# 计划书：live_trader/main.py 阶段 3 — services/ 拆分（收官，最高风险）

> 日期：2026-07-19
> 性质：**交易核心服务搬迁 + scheduler 生产路径联动**（直接动交易逻辑 + auto_buy 生产）
> 前置：阶段 0-2 已完成（main.py 283 行）
> 状态：**待双 agent 审计 → 迭代 → 用户批准后才动手**

---

## TL;DR

抽 `services/order_service.py`（place_order_service）+ `services/signal_service.py`（process_buy_signals + _process_one_signal），同步改 **scheduler.py:436 生产路径** + routers/trade.py 2 处。main.py 283 → ~100 行（收官）。**外部零依赖**（grep 确认 scripts/tests 不引用这 3 个），故 main 不留 re-export，直接删。

**为什么这步风险最高**：
1. scheduler.py:436 是 auto_buy 生产代码（14:50 真实下单），改错 = 实盘 auto_buy 崩
2. process_buy_signals 极复杂（asyncio 并发 + 心跳防 14:55 看门狗误报 + 幂等 + 去重）
3. _process_one_signal → place_order_service **跨 services 调用**（新增的 import 维度）
4. 直接动交易逻辑（资金安全核心）

---

## 1. 范围（3 服务搬 services/）

| 服务 | main.py 行号 | 搬到 | 依赖 |
|---|---|---|---|
| `place_order_service` | L88-105（18行）| `services/order_service.py` | `_state`（executor）+ `from .order_executor import OrderExecutor`（函数内）|
| `process_buy_signals` | L108-204（97行）| `services/signal_service.py` | asyncio + datetime + logger + `_state` + 函数内(schemas/xtquant_compat) + 调 `_process_one_signal` |
| `_process_one_signal` | L209-273（65行）| `services/signal_service.py` | asyncio.to_thread + date + `_state` + 函数内(schemas/buy_volume/xtquant_compat/hashlib/settings) + **调 `place_order_service`（L270，跨 services）** |

## 2. services/ 设计

### services/__init__.py（新建，package 标记）

### services/order_service.py
```python
"""下单核心服务(阶段3, 2026-07-19 从 main.py 抽离)。"""
from core.logger import get_logger
from .._state import state as _state

logger = get_logger("live_trader.main")  # 沿用 main 名(审计 R6 教训)

def place_order_service(intent, source: str = "WEB", lock_wait_sec: int = 30) -> dict:
    ...原 main.py L88-105 原样搬...
```

### services/signal_service.py
```python
"""买入信号处理服务(阶段3, 2026-07-19 从 main.py 抽离)。

含 process_buy_signals(并发内核+心跳+去重+幂等) + _process_one_signal(单信号处理)。
"""
import asyncio
from datetime import date, datetime

from core.logger import get_logger

from .._state import state as _state
from .order_service import place_order_service  # 跨 services:_process_one_signal 调

logger = get_logger("live_trader.main")  # 沿用 main 名

async def process_buy_signals(...):
    ...原 main.py L108-204 原样搬...

async def _process_one_signal(...):
    ...原 main.py L209-273 原样搬...
```

### 跨 services 调用（核心难点）
- `_process_one_signal`（signal_service）L270 调 `place_order_service`（order_service）
- signal_service **顶部** `from .order_service import place_order_service`
- order_service **不** import signal_service → **单向，无循环** ✅
- 验证：order_service 依赖 _state + OrderExecutor；signal_service 依赖 _state + order_service + asyncio。都不 import main。

### 🔴 函数内相对 import 路径调整（审计 C1/A，**CRITICAL — 最易踩坑**）

> services/ 比 main.py 深一级，函数内 `from .xxx` 必须改 `from ..xxx`，否则 ModuleNotFoundError。§2 代码示例的"原样搬"指**逻辑原样搬**，函数内相对 import 前缀必须按下表改。**py_compile 抓不到**（只查语法），必须靠验证 #3 真跑。

| main.py 行 | 当前 | 搬后（services/ 下）|
|---|---|---|
| L101 | `from .order_executor import OrderExecutor`（place_order_service 内）| `from ..order_executor import OrderExecutor` |
| L134 | `from .schemas import BuySignalResult, SignalResult`（process_buy_signals 内）| `from ..schemas import ...` |
| L211 | `from .schemas import OrderIntent`（_process_one_signal 内）| `from ..schemas import OrderIntent` |
| L212 | `from .buy_volume import _calc_buy_volume`（_process_one_signal 内）| `from ..buy_volume import _calc_buy_volume` |

绝对路径不变：L135/213/253 `from app.utils.xtquant_compat`、L233 `from core.settings`、`import hashlib`。

## 3. import 改动清单（机械工作重点）

### 3.1 scheduler.py:436（🔴 生产路径）
```python
# 当前(在 _do_auto_buy 方法体内,函数内 import):
from .main import process_buy_signals
# 改后:
from .services.signal_service import process_buy_signals
```

### 3.2 routers/trade.py（2 处函数内 import）
| 行 | 当前 | 改后 |
|---|---|---|
| L176 | `from ..main import place_order_service` | `from ..services.order_service import place_order_service` |
| L258 | `from ..main import process_buy_signals` | `from ..services.signal_service import process_buy_signals` |

### 3.3 main.py
- 删 3 服务定义（L88-273，约 186 行）
- **不留 re-export**（grep 确认 scripts/tests 零外部依赖，routers/scheduler 改源后 main 内部也不用）

## 4. main.py 死 import 清理（删 3 服务后变死）

| import | 原使用者 | 搬后 |
|---|---|---|
| `import asyncio` | process_buy_signals(Semaphore/gather) + _process_one_signal(to_thread) | **死** → 删 |
| `from datetime import date, datetime` | process_buy_signals(datetime.now) + _process_one_signal(date.today) | **死** → 删 |
| `HTTPException, Request`（fastapi 行）| 原 main 路由（阶段1搬走），_resolve 不用 | **死** → 删（code-reviewer 阶段1 NOTE 遗留，顺手清）|

留：`FastAPI`（app）+ `CORSMiddleware` + `get_logger` + `_state`/`auth`/`routers`/`lifecycle` import。

## 5. 风险点（按严重级）

### 🔴 R1 [CRITICAL] scheduler.py:436 生产路径 —— auto_buy 真实下单
- `_do_auto_buy` 的 `from .main import process_buy_signals` 是 auto_buy 14:50 触发的生产代码。
- 改错（拼写/路径）= auto_buy ImportError，14:50 崩，错过下单。
- **缓解**：改完 grep 确认 scheduler 无 `from .main import process_buy_signals` + py_compile + pytest（test 含 scheduler 相关）+ **人工 auto_buy 冒烟（用户做）**。

### 🔴 R2 [CRITICAL] _process_one_signal → place_order_service 跨 services
- _process_one_signal（signal_service）调 place_order_service（order_service）。
- signal_service 顶部 import 若失败 = 信号处理崩，buy-signal 端点 500。
- **缓解**：signal_service 顶部 `from .order_service import place_order_service`（单向无循环，已分析）+ py_compile + identity 验证 + pytest test_buy_signal_bridge。

### 🟡 R3 [HIGH] process_buy_signals 心跳逻辑
- L199 `store.record_heartbeat("docker_tdx", ...)` 防 scheduler 14:55 看门狗误报。
- 搬迁**绝不能漏**这行（漏 = 14:55 看门狗误报"无信号心跳"）。
- **审计 C 补充**：L197-198 的 scan_status 三值 `"ok"/"no_signal"/"all_rejected"` 与 scheduler.py:403 看门狗判读配套，**字符串字面量不可改**。
- **缓解**：逻辑原样搬（函数体不动，仅函数内相对 import 前缀按 §2 改）+ pytest + 人工观察心跳。

### 🟡 R4 [MEDIUM] logger name 沿用 live_trader.main（R6 教训）
- process_buy_signals/_process_one_signal 有 logger 日志。
- services 用 `get_logger("live_trader.main")`（和 lifecycle 同理），保持日志源标签。

### 🟢 R5 [LOW] main 死 import 清理
- asyncio/date/datetime/HTTPException/Request 删。py_compile + grep 兜底。

### 🟢 R6 [LOW] services/__init__.py 新建 + 外部零依赖
- services/__init__.py 空 package 标记（审计 D：空文件或仅 docstring，**不 re-export**，保外部零依赖边界）。
- 外部（scripts/tests）零依赖（grep 确认），main 不留 re-export。

### 🔴 R7 [HIGH] 函数内相对 import 前缀 `.`→`..`（审计 C1/A，**最易踩坑**）
- services/ 比 main.py 深一级，函数内 `from .schemas`/`.buy_volume`/`.order_executor` 必须改 `from ..xxx`，否则 ModuleNotFoundError。
- **py_compile 抓不到**（只查语法不解析 import），必须靠验证 #3 真跑。
- **缓解**：按 §2 函数内 import 清单改 4 处 + grep `from \.schemas\|from \.buy_volume\|from \.order_executor` in services/*.py 应 0 + 验证 #3 真跑 import。

## 6. 验证清单（实施后必跑）

| # | 验证项 | 方法 | 预期 |
|---|---|---|---|
| 1 | py_compile（**仅语法层,抓不到 import 错 — 审计 B**） | `python -m py_compile services/*.py main.py scheduler.py routers/trade.py` | 通过 |
| 2 | import main | `from app.live_trader.main import app` | 不报错 |
| 3 | 服务来自 services | `from app.live_trader.services.order_service import place_order_service; from app.live_trader.services.signal_service import process_buy_signals` | 可 import |
| 4 | 44 路由不变 | `len(app.routes)` | 44 |
| 5 | main.py 残留 | grep `^def place_order_service\|^async def process_buy_signals\|^async def _process_one_signal` in main.py | 0 |
| 6 | import 源全改 | grep `from \.\.main import place_order_service\|from \.\.main import process_buy_signals\|from \.main import process_buy_signals` in routers/trade.py + scheduler.py | 0 |
| 7 | main 死 import | grep `^import asyncio\|^from datetime\|HTTPException\|Request` in main.py | 0（已删）|
| 8 | **pytest 全套** | 4 文件 81 用例 | 全过 |
| 9 | **人工冒烟** | 用户实盘：① 启服务（lifespan）② buy-signal 触发一次（TDX 路径）**④ 手工下一单测试单（WEB 路径,审计 M1）** ③ 观察 14:50 auto_buy + 14:55 心跳 | 全正常 |
| 10 | **services 函数内 import 双点**（审计 R7） | grep services/*.py 确认无 `from .schemas` / `from .buy_volume` / `from .order_executor` | **0 命中**（必须 `..` 双点）|

## 7. 行数预估

- 搬走 place_order_service(18) + process_buy_signals(97) + _process_one_signal(65) = **~180 行**（净代码；含函数间空行/注释的删除区间 L88-273 共 186 行，审计 M2）
- main.py **283 → ~100 行**（收官，原 EVAL 估 ~250，因阶段 2 已多搬）
- main.py 剩：docstring + import + app/middleware/include + _resolve + __main__

## 8. 回滚预案

- 改动未 commit，`git diff` 可见。
- 若冒烟失败：`git checkout app/live_trader/main.py app/live_trader/scheduler.py app/live_trader/routers/trade.py`（services/ 目录删除即可，新文件）。
- 回滚不影响阶段 0-2（阶段 1 已 commit 358a1c6，阶段 2 待 commit）。

## 9. 不做的事（YAGNI）

- ❌ 不改 process_buy_signals/_process_one_signal 的**逻辑**（逻辑原样搬，含心跳/幂等/去重/cutoff/尾盘定价；**仅函数内相对 import 前缀 `.`→`..`**，审计 C1/A）
- ❌ 不重构 place_order_service（只搬位置 + 函数内 import 前缀）
- ❌ main 不留 re-export（外部零依赖）
- ❌ 不动 _resolve_instrument_name（market/config_api 还用，留 main，未来单独处理）
- ❌ 不动 signal_picker.py / scheduler.py:435 `from .signal_picker import`（审计 L1：signal_picker 本就在 app/live_trader/，`from .schemas` 正确，与 services 拆分无关）

---

## 审计修订记录（2026-07-19，Step 3 迭代）

经 code-reviewer + architect 双 agent 审计（报告：`AUDIT-services计划书_文档质量_2026-07-19.md` + `ARCHREVIEW-services计划书_技术可行性_2026-07-19.md`），逐条处理：

| Finding | 来源 | 处理 |
|---|---|---|
| **C1/A** 函数内相对 import 前缀 `.`→`..` 遗漏（4 处）| **双方共识 CRITICAL** | ✅ §2 加"函数内 import 路径调整"小节 + §9 改"逻辑原样搬" |
| **H1** R-list 漏函数内 import 风险 | code-reviewer | ✅ §5 加 R7 [HIGH] |
| B 验证 #1 py_compile 虚假安全感 | architect | ✅ §6 #1 标"仅语法层" |
| C R3 补 scan_status 三值不可改 | architect | ✅ §5 R3 补 |
| M1 项9 漏 WEB 手工下单路径 | code-reviewer | ✅ §6 项9 加 ④ |
| M2 行数 186 vs 180 注解 | code-reviewer | ✅ §7 加注 |
| L1 signal_picker 受影响说明 | code-reviewer | ✅ §9 补 |
| D services/__init__.py 空文件明示 | architect | ✅ §5 R6 补 |
| 验证加 grep services 双点 | 双方 | ✅ §6 加项10 |

**两份审计共识**：架构思路站得住（跨 services 无循环、to_thread 函数对象、心跳 store 同对象、scheduler 改源无循环、死 import 准、logger 一致、routers 无循环、外部零依赖）。**1 CRITICAL 漏洞**（函数内 import 前缀，和 trade R5 同类盲区）已修入 §2 + R7。计划书达"可施工"标准。

---

## 待办

- [x] Step 2：code-reviewer + architect 双 agent 审计（完成）
- [x] Step 3：据审计迭代（完成，见上表）
- [ ] Step 4：用户批准后实施
