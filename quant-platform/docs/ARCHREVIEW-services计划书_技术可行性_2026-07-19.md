# 审计报告：services.py 计划书 — 技术可行性

> 审计对象：`docs/PLAN-live_trader_services拆分_2026-07-19.md`
> 审计员：architect agent（Read/Grep 实证，禁止派子 agent）
> 日期：2026-07-19
> 结论：**WARNING — 思路对能落地，但 §2 设计示例漏写"函数内相对 import 前缀必须改"，按"原样搬"实施会 CRITICAL 翻车**

---

## 一、7 个核心技术问题裁决

### Q1 跨 services 顶部 import 是否循环 — ✅ 认同（无循环）
- order_service.py 顶部：`from .._state import` + get_logger（叶子）
- signal_service.py 顶部：`from .._state import` + `from .order_service import place_order_service` + get_logger
- main.py 顶部不 import services（不留 re-export）
- scheduler.py:436 / routers/trade.py:176,258 函数内 import
- 依赖方向 services → _state + order_service（单向），无回环

### Q2 to_thread(place_order_service) 拿到正确函数对象 — ✅ 认同
signal_service 顶部 `from .order_service import place_order_service` 后，名字绑定到 order_service.place_order_service 函数对象本身（Python import 是引用绑定）。搬迁后行为与 main.py L269-271 一致。

### Q3 心跳 store 一致性 — ✅ 认同（同一对象）
lifecycle.py:224 `_state.update({"store": store})` → scheduler.py:44 `self.store = store` → signal_service `_state.get("store")` 同一 dict。signal_service 写 record_heartbeat，scheduler:362 _check_signal_heartbeat 读同一对象，14:55 看门狗不受影响。

### Q4 scheduler.py:436 改源后是否循环 — ✅ 认同（无循环）
函数内 import（_do_auto_buy 体内），运行时解析。scheduler 顶部不 import services，signal_service 不 import scheduler。单向。⚠️ 但运行时 import → signal_service 顶部错误要 14:50 才暴露，验证 #2/#3 必须真跑。

### Q5 main.py 死 import 清单 — ✅ 认同（4 项全死）
asyncio / date / datetime / HTTPException / Request 删后 main 剩余代码（_resolve + app + __main__）零使用。**实施细节**：L8 `from fastapi import FastAPI, HTTPException, Request` 整行改 `from fastapi import FastAPI`（不能留逗号）。

### Q6 services/logger 沿用 live_trader.main — ✅ 认同（与 lifecycle 同理）
core/logger.py:64-68 get_logger(module) → loguru bind module。日志 cyan 列读 module extra。services 用 live_trader.main 与原一致。

### Q7 routers/trade 改源后是否循环 — ✅ 认同（无循环）
trade.py:176/258 函数内 import（端点体内），运行时解析。trade 顶部不 import services，单向。阶段 1 的函数内 import 模式继续生效。

---

## 二、额外发现（计划书未提）

### 🔴 A [CRITICAL] 函数内相对 import 前缀必须改 `.`→`..`（计划书最大遗漏）
三个服务函数内的相对 import（main.py 原文）：

| main.py 行 | 代码 | 搬到 services/ 后 |
|---|---|---|
| L101 | `from .order_executor import OrderExecutor` | `from ..order_executor import`（order_service 内）|
| L134 | `from .schemas import BuySignalResult, SignalResult` | `from ..schemas import`（signal_service 内）|
| L211 | `from .schemas import OrderIntent` | `from ..schemas import` |
| L212 | `from .buy_volume import _calc_buy_volume` | `from ..buy_volume import` |
| L135/213/253 | `from app.utils.xtquant_compat import` | 绝对路径不变 |
| L233 | `from core.settings import` | 绝对路径不变 |

计划书 §2 写"原样搬"，§9 写"不改逻辑（原样搬）"。按字面实施：
- order_service.py 加载 → `from .order_executor` 解析为 `live_trader.services.order_executor` → **ModuleNotFoundError**
- signal_service.py 加载 → L134/L211/L212 同样 ImportError
- 爆炸路径：buy-signal 请求 500 + 14:50 auto_buy 崩 + 验证 #3 立即暴露

**修复成本极低**：4 处 `.` → `..`。但计划书必须明示，不能让实施者按"原样搬"踩雷。

**py_compile 抓不到**：只查语法不解析 import。验证 #1 通过 ≠ 运行时能 import。必须真跑 #2/#3。

### 🟡 B [MEDIUM] 验证 #1 py_compile 给虚假安全感
建议标注"仅语法层"，#2/#3 标"import 层验证，必须真跑"。

### 🟡 C [MEDIUM] R3 补 scan_status 三值不可改
scheduler.py:403 `hb.get("scan_status")` 读打日志，process_buy_signals L197-198 的 `"ok"/"no_signal"/"all_rejected"` 三值是和看门狗配套的。搬迁若改字符串字面量会出问题。R3 补"scan_status 三值不可改"。

### 🟢 D [LOW] services/__init__.py 空文件明示
明示"空文件或仅 docstring，不 re-export"，避免画蛇添足破坏"外部零依赖"边界。

### 🟢 E [LOW] main.py TODO 注释块保留
L207-278 TODO + __main__ 块留 main.py，不是搬迁范围。

---

## 三、认同/存疑清单

| 计划书判断 | 裁决 |
|---|---|
| §2 跨 services 顶部 import 无循环 | ✅ 认同 |
| §2 signal_service 顶部 import place_order_service | ✅ 认同（但配套函数内 `..` 没写，见 A）|
| §2 "原样搬" | 🔴 **反对**（函数内相对 import 前缀必须改）|
| §3.1 scheduler:436 改源 | ✅ 认同 |
| §3.2 routers/trade 改源 | ✅ 认同 |
| §3.3 不留 re-export | ✅ 认同（外部零依赖）|
| §4 死 import 清单 | ✅ 认同（要改 fastapi 行）|
| §5 R1-R6 | ✅ 认同（R3 补 scan_status 三值）|
| §6 验证 #1 py_compile | 🟡 存疑（抓不到 import 错）|
| §6 验证 #2/#3 | ✅ 认同（真能抓 A）|
| §9 "原样搬" | 🔴 **反对**（与 A 冲突）|

---

## 四、总体结论：WARNING（可落地，需修计划书后动手）

- 7 核心技术问题 6 个判断正确（跨 services 无循环、to_thread 函数对象、心跳 store 同对象、scheduler 改源无循环、死 import 准、logger 一致、routers 无循环）
- 1 CRITICAL 漏洞：§2 + §9 "原样搬" 遗漏函数内相对 import 前缀 `.`→`..`，按字面实施 = signal_service/order_service 加载即 ImportError，buy-signal 500 + 14:50 auto_buy 崩
- 修复成本极低（4 处前缀 + 措辞修正）

**埋雷风险**：
| 维度 | 埋雷 | 说明 |
|---|---|---|
| scheduler 14:50 auto_buy 生产 | 🔴 若不修 A 则埋雷 | 运行时 import 失败 → auto_buy 崩 |
| 心跳 14:55 看门狗 | 🟢 不埋雷 | store 同对象，R3 强调 |
| 跨 services 调用 | 🟢 不埋雷 | 顶部 import 单向无循环 |
| main 死 import | 🟢 不埋雷 | 4 项全死 |

**建议**：架构思路 PASS，**必须先迭代修 A 项**（§2 明示函数内 import 前缀 + §9 改"原样搬"措辞），修完再实施。
