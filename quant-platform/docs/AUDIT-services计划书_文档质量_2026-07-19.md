# 审计报告：services.py 计划书 — 文档质量

> 审计对象：`docs/PLAN-live_trader_services拆分_2026-07-19.md`
> 审计员：code-reviewer agent（Read/Grep 实证，禁止派子 agent）
> 日期：2026-07-19
> 结论：**WARNING — 1 CRITICAL + 1 HIGH 应在实施前补齐**

---

## CRITICAL

### C1 [完整性+可行性] 函数内相对 import 路径调整完全遗漏 — "原样搬"会直接 ImportError
**证据**：
- `main.py:101` `from .order_executor import OrderExecutor`（place_order_service 内）
- `main.py:134` `from .schemas import BuySignalResult, SignalResult`（process_buy_signals 内）
- `main.py:211` `from .schemas import OrderIntent`（_process_one_signal 内）
- `main.py:212` `from .buy_volume import _calc_buy_volume`（_process_one_signal 内）

计划书 §2 写"原样搬"，§9 写"不改逻辑（原样搬）"。

**问题**：services/ 是 `app/live_trader/services/`，比 main.py 深一级。函数内 `from .schemas` 在 services/ 下会解析到不存在的 `app/live_trader/services/schemas`，**4 处相对 import 都会 ImportError**，buy-signal 端点 500、auto_buy 14:50 崩。绝对 import（`from app.utils.xtquant_compat`、`from core.settings`、`import hashlib`）不受影响。

**建议**：§2 或 §3 增加"函数内 import 路径调整清单"：
| main.py 行 | 当前 | 搬后（services/ 下）|
|---|---|---|
| L101 | `from .order_executor import OrderExecutor` | `from ..order_executor import OrderExecutor` |
| L134 | `from .schemas import BuySignalResult, SignalResult` | `from ..schemas import BuySignalResult, SignalResult` |
| L211 | `from .schemas import OrderIntent` | `from ..schemas import OrderIntent` |
| L212 | `from .buy_volume import _calc_buy_volume` | `from ..buy_volume import _calc_buy_volume` |

§9 "原样搬"改为"逻辑原样搬，仅函数内相对 import 一级（`.` → `..`）"。§6 验证清单加：grep services/*.py 确认无 `from \.schemas\|from \.buy_volume\|from \.order_executor`（应 0）。

## HIGH

### H1 [风险识别] R1-R6 漏最大实操风险（函数内 import 路径）
R2 提到"跨 services 顶部 import"（signal_service→order_service），但**完全没提** services/ 内函数体相对 import 要跟着深一级调整。这是本次搬迁**最易踩的坑**（IDE 不提示，py_compile 能过但运行时才炸）。
**建议**：新增 R7 [HIGH]「services/ 函数内相对 import 路径调整」，缓解含 grep 校验 + import 冒烟。

## MEDIUM

### M1 [可测试性] 项9 人工冒烟只覆盖 TDX 路径，未显式覆盖 WEB 手工下单
§6 项9 ② buy-signal 走 routers/trade.py:258 → process_buy_signals（source=TDX）。但 routers/trade.py:176 的 place_order_service（source=WEB, lock_wait=30s）是独立 import 改动点，source 影响定价。
**缓解**：pytest test_manual_order 覆盖 /live/order（TestClient 加载即验 import），非阻塞。建议项9 加"④ 手工下单一笔（验 WEB 路径）"。

### M2 [一致性] §3.3 删 186 行 vs §7 搬 180 行需注解
186 = 含函数间空行/注释的删除区间（273-88+1）；180 = 三函数净行数和（18+97+65）。都对但易误读。
**建议**：§7 加"186 含空行/注释，180 为净代码行"。

## LOW

### L1 [完整性] signal_picker 受影响与否未明确（已验证不受影响）
`signal_picker.py:16` `from .schemas import SignalItem`（signal_picker 本就在 app/live_trader/，`from .schemas` 正确，与 services 拆分无关）；无 `from .main import`。scheduler.py:435 `from .signal_picker import SignalPicker` 也不受影响。
**建议**：§9 补"signal_picker.py / scheduler.py:435 不动（无 main 依赖）"。

### L2 [完整性] cutoff(L150) / 尾盘定价 PRICE_TYPE_PEER_FIRST(L260) 未进 R-list
属"原样搬"范畴，不构成独立风险。建议 R3 心跳条目后加"cutoff/尾盘定价同样原样搬，无逻辑改动"以示覆盖。

---

## 已验证正确（无需改）

| 计划书结论 | 验证结果 |
|---|---|
| scheduler.py:436 `from .main import process_buy_signals` 属实 | ✅ 函数内 import（_do_auto_buy 体内）|
| routers/trade.py:176,258 两处函数内 import | ✅ |
| 外部零依赖（scripts/tests 不引用 3 服务）| ✅ grep 确认 |
| 3 服务行号/行数（18+97+65=180）| ✅ L88-105/L108-204/L209-273 全对 |
| main.py 283 行 | ✅ wc -l = 283 |
| 6 步逻辑归属 signal_service | ✅ 全在 L108-204 |
| _process_one_signal 幂等键/买入量/尾盘定价 | ✅ 全在 L209-273 |
| 跨 services 单向无循环 | ✅ order_service 不 import signal_service；都不 import main |
| 心跳 L199 store.record_heartbeat | ✅ |
| 死 import 清理（asyncio/date/datetime/HTTPException/Request）| ✅ grep 确认残余零使用 |

---

## Review Summary

| Severity | Count | Status |
|---|---|---|
| CRITICAL | 1 | block（C1 函数内相对 import 遗漏）|
| HIGH | 1 | warn（H1 R-list 缺项）|
| MEDIUM | 2 | info |
| LOW | 2 | note |

**总体结论**：作为施工蓝图**基本可用，但不能直接照搬实施**。C1 是硬伤——"原样搬"措辞与 services/ 深一级现实冲突，4 处函数内相对 import 必须显式调整 `..` 前缀，否则搬后第一次 import 就崩。**建议补 C1（含验证清单新增项）+ H1 后迭代一版再实施**。

**Verdict: WARNING — 1 CRITICAL + 1 HIGH 应在实施前补齐**。
