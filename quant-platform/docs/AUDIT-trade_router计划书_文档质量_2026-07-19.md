# 审计报告：trade router 计划书 — 文档质量

> 审计对象：`docs/PLAN-live_trader_trade_router拆分_2026-07-19.md`
> 审计员：code-reviewer agent（Read/Grep 实际验证，禁止派子 agent）
> 日期：2026-07-19
> 范围：计划书作为施工蓝图的文档质量（不审代码本身）
> 总体结论：**WARNING — 可作施工蓝图，动手前需补 3 处**

---

## 总体结论

| 维度 | 结论 | 一句话 |
|---|---|---|
| 完整性 | WARNING | 10 路由全列，但路由6 漏 dry-run 403 检查时机 + date/time 顶层 import |
| 一致性 | **FAIL** | batch1 标题"5 个"vs 正文"6 个"；asyncio"≥4 处"实际只 3 处 |
| 可行性 | PASS | 函数内 import 成立；scheduler:436 不动可行 |
| 风险识别 | WARNING | 漏了 source 参数语义 + dry-run 检查归属 |
| 优先级 | PASS | 简单→复杂、核心服务最后，合理 |
| 可测试性 | WARNING | 只 2/10 路由有专项测试归宿 |

**判定**：主干扎实（行号/依赖矩阵/风险分级/回滚/YAGNI 全到位），但 1 个一致性 FAIL + 4 个 WARNING。修完 F2.1/F2.2 + 补 W1.1/W4.1 即可动手。

## 验证基础（全部 Read/Grep 核对过）
- 路由行号 10/10 全对：513/525/535/544/554/563/674/881/923/964
- 核心服务行号 3/3 全对：place_order_service:654 / process_buy_signals:713 / _process_one_signal:814
- scheduler.py:436 共用关系属实
- auth.py 设计合理，且 system.py/config_api.py 已用同款 `from ..auth import` 模式跑通

---

## Finding 清单（按严重级）

### FAIL（必修，动手前）

**F2.1 一致性 — batch1 标题"5 个"vs 正文"6 个"自相矛盾**
证据：计划书 L94 标题"5 个简单路由"，紧接下一行"这 6 个（pending 是空端点）"。实际 activate+deactivate+reconcile+exit-scan+audit/replay+pending = 6 个。batch1(6)+batch2(2)+batch3(2)=10 对，仅标题数字错。
建议：标题"5 个"改"6 个"。

**F2.2 一致性 — asyncio 阈值"≥4 处"实际只 3 处**
证据：grep `asyncio\.` in main.py 命中 L776 Semaphore + L779 gather + L874 to_thread = 3 处。计划书验证项 4 预期"≥4 处"错。"asyncio 非死 import"结论成立（3 处都在留 main 的服务内），但阈值数字错。
建议：改"≥3 处（Semaphore/gather/to_thread）"。

### WARNING（建议修，提升蓝图精度）

**W1.1 完整性 — 路由6 dry-run 403 检查时机未列**（用户硬约束点名）
证据：main.py:580-582 的 `runtime_state.is_live()` 失败 → 403 是**路由层**检查，在 place_order_service 调用之前。place_order_service 本身（654-672）不做 dry-run 检查。计划书 R1 只提 import 链失败风险，完全没提这 3 行归属。漏搬或挪到 service 之后 = dry-run 模式误下真单（实盘事故）。
建议：R1 加 "L580-582 dry-run 检查必须随路由6 搬、保留在 place_order_service 调用之前、顺序不可改"。

**W4.1 风险 — place_order_service 的 source 参数语义差异未提**（用户硬约束点名）
证据：两路调用签名不同 ——
- 路由6 (main.py:648): `place_order_service(intent, source="WEB", lock_wait_sec=30)`
- _process_one_signal (main.py:874-876): `asyncio.to_thread(place_order_service, intent, "TDX", lock_wait_sec)` 默认 5s

source="WEB" vs "TDX" 决定 OrderExecutor 价格策略 + terminal 标记；lock_wait_sec 30s vs 5s 决定清仓锁等待。计划书 R1 只说"两路共用"，没列参数差异。搬路由6 时若误改参数 = 手工下单行为悄悄变化。
建议：R1 加 "搬路由6 后核对 main.py:648 调用签名 source='WEB'/lock_wait_sec=30 一字不动"。

**W1.2 完整性 — 路由6 顶层 import 漏列 date/time**（与 architect 漏点 3 重叠）
证据：main.py:629-633 用 `date.today()` + `time.time()`，来自顶部 `from datetime import date, datetime`(L11) + `import time`(L9)。计划书矩阵"其他"列漏 date/time。trade.py 顶部不补 → NameError。
建议：矩阵"其他"补 `date/time（顶层 import 需迁移）`。

**W4.2 风险 — cancel-by-source 的 seq>0 守卫未提**
证据：main.py:947-949 `seq = order.get("seq", 0); if seq > 0 and qmt.connected: qmt.cancel_order(seq)`。计划书 R5 只提 ORDER_STATUS_INFLIGHT/ORDER_TYPE_SELL 的 import，没提 seq 守卫。漏守卫 → seq=0 触发 qmt.cancel_order(0) 未定义行为。
建议：R5 加 "L947-949 seq>0 守卫必须保留"。

**W2.3 一致性 — 行数预估算术表述误导**
证据：计划书写"搬走 ~285 / 留 main ~250 / 1007→~720"，公式 `1007-285=722` 对，但"留 ~250"易被读成"新增 250"。
建议：改为 "720 = 1007 - 285；这 ~250 行原本就在 main 内不搬所以保留"。

**W6.1 可测试 — 8/10 路由无专项测试归宿**
证据：验证项 5 只列 test_manual_order(路由6) + test_buy_signal_bridge(路由8)。其余 8 路由搬迁正确性无明确验证手段。
建议：验证项 5 补 "smoke 测试需覆盖 10 路由注册 + 基本响应；cancel_order/cancel_by_source 至少加 import 级冒烟"。

### MEDIUM
**W6.2 可测试 — 真实下单无法测未提示**
证据：路由6 调真实 QMT 下单，但 main.py:580-582 dry-run 硬拒 403，测试走不进 live 链路。计划书 R1 缓解"pytest test_manual_order 全过"未说明此测试是 mock 还是真实链路。
建议：R1 加 "test_manual_order 走 mock executor，真实 QMT 下单链路不在测试覆盖内，搬完需人工 dry-run 切 live 冒烟"。

### LOW
**W1.3 完整性 — buy-signal 路由 HTTP 层 4 步未列**
证据：main.py:881-920 除调 process_buy_signals 外，还有 runtime_state.buy_enabled 检查(L894-898) + _verify_token 鉴权(L901) + BuySignalRequest 校验(L905) + kill_switch 检查(L911)。R2 只点名服务内副作用，没列这 4 步。
建议：R2 加 "buy-signal 路由整段(L881-920)原样搬，含 4 步 HTTP 层检查"。

---

## 给主控的决策清单
- 优点：路由/服务行号 100% 准确（零推断）、scheduler:436 共用属实、函数内 import 方案无循环、auth 共享依赖已被 system/config_api 验证同款
- 必修 2 条：F2.1（batch1 数字）+ F2.2（asyncio 阈值）— 对内不自洽硬伤
- 强烈建议补 2 条（用户硬约束点名）：W1.1（dry-run 403 时机）+ W4.1（source 参数语义）
- 其余 WARNING 可在实施时顺手补
- 修完上述 4 条即达"可施工"标准
