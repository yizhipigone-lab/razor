# 文档质量审计：live_trader/main.py 拆分评估方案

> 审计对象：`docs/EVAL-live_trader_main拆分评估_2026-07-19.md`
> 审计员：code-reviewer agent（Read/Grep 实际代码逐条核对，无子 agent，无推断）
> 日期：2026-07-19
> 总体结论：**WARNING — 战略结论可信，但有 2 处 FAIL 级事实错误必须先修正再据以执行**

---

## 关键 Findings（按严重级）

### [FAIL] F1 — §3.2 "外部依赖只有一处" 事实错误（完整性/准确性）

文档原称"全仓库只有 `scripts/poc_dryrun_e2e.py` import main 内部函数，影响面 1 行"。实际 grep `from app\.live_trader\.main import|from \.main import` 全仓，共 **6 处导入点**：

| 文件:行 | 导入符号 | 性质 | 文档是否提及 |
|---|---|---|---|
| `scripts/poc_dryrun_e2e.py:94` | `_takeover_positions` | 外部脚本 | 提及 |
| `tests/test_live_trader_smoke.py:614` | `_takeover_positions` | 测试 | **漏** |
| `tests/test_live_trader_audit.py:112, 159` | `_state`, `app` | 测试 | **漏** |
| `tests/test_buy_signal_bridge.py:240, 246, 252, 259` | `_verify_token` ×4 | 测试 | **漏** |
| `app/live_trader/scheduler.py:436` | `process_buy_signals` | **生产代码** | **漏（最严重）** |
| `app/live_trader/scheduler.py:555` | `_state` | **生产代码** | **漏（最严重）** |

**最致命的漏报**：`scheduler.py:436`（在 `_do_auto_buy` 方法体内）和 `:555`（在 `_cleanup_notifications` 方法体内）是**实盘在跑的生产代码**（`main.py:74` lifespan 装配 `LiveScheduler`，`main.py:247` 写入 `_state["scheduler"]`）。这两处运行时反向导入 `main.process_buy_signals` 和 `main._state`。若拆分到 `services/signal_service.py` 和 `_state.py` 后只补 poc 的 1 行别名、漏改 scheduler.py 这两处，**下次定时任务触发即 ImportError，线上事故**。

修正后影响面：需 4 个 re-export 别名 + `app` 必须仍可从 main 导入，或同步改 scheduler.py 两处导入路径（额外工作量，文档原未列）。

### [FAIL] F2 — §3.4 / §6 / 附录 A "无 pytest 覆盖" 事实错误（风险识别失真）

文档原称"实盘风险 ⚠️ 中等 —— 无 pytest 覆盖"、"拆分引入回归（无 pytest）| 高"。实际：

```
tests/test_live_trader_smoke.py    767 行
tests/test_live_trader_audit.py    207 行
tests/test_buy_signal_bridge.py    425 行
                              共 1399 行
```

且这些测试**直接 import main 内部符号**（见 F1 表）。双向失真：
- 高估实盘风险（实际有 1399 行测试网兜底，应中低）
- 低估可测试性（验收应含 `pytest tests/test_live_trader_*.py` 全绿）
- `test_buy_signal_bridge.py::TestTokenVerification` 已覆盖文档 §6.3 #16/#17 的 4 个 token 分支，手点是重复劳动

### [WARNING] F3 — scheduler.py 耦合维度整章缺失（完整性）

证据见 F1。main.py ↔ scheduler.py 是**双向耦合**：正向 `main.py:74/247` 装配 + 注入，反向 `scheduler.py:436/555` 运行时导入。文档 §2 第 2 条只把"高频同时改动"当拆分理由，没识别为拆分约束。

### [WARNING] F4 — §6 冒烟清单 34 条未利用既有 pytest（可测试性）

§6 列 34 条手点 + 1 条 poc（#33），完全没把 pytest 纳入验收。建议加 §6.0 自动化前置：`pytest tests/test_live_trader_smoke.py tests/test_live_trader_audit.py tests/test_buy_signal_bridge.py -v` 全绿作为所有手点的前提。

### [WARNING] F5 — 路由行号系统性 off-by-one（一致性）

grep `^@app` 核对，大部分路由文档引用的是**函数 def 行**（装饰器+1），少数是装饰器行，两套基准混用：

| 路由 | 装饰器实际 | 文档引用 | 偏差 |
|---|---|---|---|
| `POST /live/positions/sync` | 556 | 557 | +1 |
| `POST /live/order` | 710 | 711 | +1 |
| `POST /live/buy-signal` | 1043 | 1044 | +1 |
| `POST /live/sync-positions` | 1132 | 1133 | +1 |
| `GET /live/equity` | 1732 | 1733 | +1 |
| `GET /live/status` | 478 | 478 | 0 |

（共抽检 11 条，10 条 +1、1 条对齐）。建议全表统一基准（注：def 行也是有效定位，只是基准需统一）。

### [NOTE] F6 — §4.1 `_state.py` 抽离动机描述含混（可行性）

文档说"routers `from .main import _state` 会引发循环依赖"。实际 `scheduler.py:555` 早就在运行时这样导入且工作正常，关键区别是"模块顶层导入 vs 函数内导入"，文档未区分。漏掉的正向理由：抽 `_state.py` 后 `scheduler.py:555` 也应改成 `from ._state import state`，与 F3 呼应。

---

## PASS 项（文档说对的，已逐条验证）

- 总行数 1930（`wc -l` 实证）✓
- 文件 80KB（80884 字节）✓
- 路由总数 40（grep `^@app` 实数）✓
- 分组 9+10+12+9=40（逐组核对归属全对）✓
- `_state` 是模块级 dict（`main.py:26` `_state: dict = {}`）✓
- 两个同名 `sync_positions`（L557 带参数、L1133 无参数）✓
- §3.3 同名函数"装饰器先注册、函数名后绑定覆盖"机制分析 Python 语义正确 ✓
- `place_order_service` L801、`_verify_token` L961、`lifespan` L58 ✓
- 目录结构与现有 25 个 live_trader/*.py 文件无重名冲突 ✓
- §3.1 "`_state` dict 好拆"核心可行性判断成立 ✓

---

## 6 维度汇总

| 维度 | 结论 | 关键 |
|---|---|---|
| 1. 完整性 | WARNING | F1 漏 5 处依赖、F3 漏 scheduler 耦合维度 |
| 2. 一致性 | WARNING | F5 行号 off-by-one 系统性偏差 |
| 3. 可行性 | PASS（带修订） | 核心技术判断成立；但工作量评估偏低 |
| 4. 风险识别 | **FAIL** | F1 漏生产代码、F2 误判无 pytest |
| 5. 优先级 | PASS | 阶段 0→1→2 顺序合理、trade 最后抽判断正确 |
| 6. 可测试性 | WARNING | F4 未利用既有 pytest |

---

## 总体结论：能否作为拍板依据

**WARNING — 战略层可作为拍板依据，战术层不可直接照搬执行。**

- **战略结论可信**（可拍板"拆不拆"+"怎么拆方向"）：该拆（1930×2.4 倍超标 + `_state` dict 易拆）、两阶段顺序合理、4 分组设计合理、同名坏味道真实。
- **战术数据必须先修正 3 处才能执行**：
  1. F1：§3.2 补全 6 处依赖，把 `scheduler.py:436/555` 标为**协同改造点**（不是"1 行别名"）
  2. F2：§3.4 / 附录 A 风险等级下调到"中低"，承认有 1399 行 pytest
  3. §5 阶段 2 加一步：抽 `services/signal_service.py` / `_state.py` 时**同步改 scheduler.py 两处反向导入**，否则线上炸

修正后可升级为"可执行拆分方案"。
