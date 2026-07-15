# 审计核查报告 — 对照 docs/AUDIT-work-2026-07-14-full.md 逐项验证

> 核查日期：2026-07-14
> 核查方法：读实际代码，不靠推断

---

## 一、要求 vs 实现比对（审计报告表）

逐条核实：

| 审计报告结论 | 代码实际 | 核查结果 |
|---|---|---|
| 新建 `utils.py` 的 `calc_trading_days()` | `utils.py:24-44` 已实现 | ✅ 正确 |
| `/live/config/risk-status` API | `main.py:1284-1503` 已实现 | ✅ 正确 |
| `risk_params` 提到顶层 | `main.py:1305-1317` 已实现 | ✅ 正确 |
| `last_close` 字段 | `main.py:1486` 已实现 | ✅ 正确 |
| 轮询与 alerts.js 模式对齐 | `live_trader.js:693-701` 已实现 | ✅ 正确 |
| `hold_days<2` 时 HS 显示 safe + T+1 标注 | **代码中不存在此逻辑** | ❌ 审计结论正确 |
| TR 已触发时 status="danger" | **代码写的是 `"warning"`** | ❌ 审计结论正确 |
| TR `remaining` 负数兜底 | **未处理** | ❌ 审计结论正确 |
| 进度条宽度公式 | **前端只有 table，无进度条** | ❌ 审计结论正确 |
| ATR 模式标注"显示与实际触发可能存在偏差" | **代码中不存在此逻辑** | ❌ 审计结论正确 |
| TC 条件逻辑 | **逻辑可疑，需核实** | ⚠️ 审计有道理（见本报告 TC 部分） |
| pytest 测试文件 `test_live_trader_risk_monitor.py` | **不存在** | ❌ 审计结论正确 |
| `current_price` 用实时价（非 last_close） | **API 确实返回 last_close** | ❌ 审计结论正确 |

---

## 二、逐项问题核查

### [CRITICAL-1] TR 已触发状态语义错误

**审计结论：** `main.py:1373` TR 已触发时 `tr_status = "warning"`，应为 `"danger"`

**代码验证：**
```python
# main.py:1371-1374
if tr_triggered:
    tr_remaining = 0.0
    tr_status = "warning"   # ← 确认是 "warning"
    tr_message = f"已触发移动止盈（回撤{drawdown:.1f}% > 阈值{trail_dd_pct:.1f}%）"
```

**PLAN 设计稿（PLAN-live-risk-monitor-2026-07-14.md:50）：**
> `HS(120) > TF(115) > TP(110) > TR(105) > FD(90) > TC(20)`

PLAN 原文"风控状态判定规则"：`danger：HS 已触发 或 TF 已触发（全局退出）`，`warning：TR 已激活 / TP 已触发 / BE 已触发（部分退出）`

**核查结论：** ❌ **审计正确**。TR 已触发写 `"warning"` 是 PLAN 设计意图（TR 是部分退出，非全局退出）。但 `_reason_priority` 中 TR=105 高于 FD=90/TC=20，退出时确实优先于 FD/TC。设计上 TR 是"部分退出"警告，不等于 danger（HS/TF 全卖）。审计报告要求改为 danger 是**与 PLAN 设计稿矛盾**——PLAN 明确说 TR 是 warning 而非 danger。

**实际判断：** 审计报告对代码的描述准确，但对 PLAN 语义的理解有误——TR 触发 = `"warning"` 符合 PLAN 设计稿。

---

### [HIGH-1] `current_price` 字段误用昨收价

**审计结论：** `main.py:1484` 直接写 `last_close`，不是实时价

**代码验证：**
```python
# main.py:1484
"current_price": float(pos.get("last_close") or 0),  # 实时价由前端 applyLiveQuotes 填入
```

**核查结论：** ❌ **审计描述准确**。但注释说"实时价由前端 applyLiveQuotes 填入"——即后端返回 last_close，前端 5 秒轮询的 `applyLiveQuotes()` 会用实时价覆盖 `current_price` 列。这是**设计决策**：后端 API 不知道实时价（QMT 行情走 WebSocket 推送，前端 `applyLiveQuotes` 才是实时价来源）。

审计报告对此的理解有偏差——`current_price` 列在前端显示时会变成实时价，后端只给 last_close 做 fallback。

---

### [HIGH-2] pytest 测试文件缺失

**审计结论：** `tests/test_live_trader_risk_monitor.py` 不存在

**代码验证：** `Grep "test.*risk" tests/` → 无匹配

**核查结论：** ✅ **审计正确**。测试文件确实不存在。PLAN 文档（:269-289）写了测试计划但未实施。

---

### [HIGH-3] TR `remaining` 负数未处理

**审计结论：** `main.py:1376`，当 drawdown > trail_dd 时计算结果为负

**代码验证：**
```python
# main.py:1376
tr_remaining = trail_dd_pct - drawdown if drawdown >= 0 else abs(drawdown)
```

场景：`drawdown=2.6`, `trail_dd_pct=2.0` → `2.0 - 2.6 = -0.6`（负数）

但注意 `main.py:1386`：
```python
"remaining": max(0, tr_remaining),  # ← 输出前做了 max(0, ...)
```
最终输出层做了 `max(0, ...)` 兜底，负数不会流到前端。

**核查结论：** ⚠️ **审计描述部分准确**。代码有 max(0) 兜底，但负数计算过程本身没有在计算点处理，而是留到输出层兜底——这是防御性编程，不算错误但不够干净。

---

### [HIGH-4] TC 条件逻辑存疑

**审计结论：** `main.py:1427` TC 条件 `pnl >= threshold` 与"盈利不足"语义矛盾

**代码验证：**
```python
# main.py:1427
tc_status = "warning" if tc_remaining <= 0 and profit_rate >= tc_profit_threshold else "safe"
```

TC 语义（时间条件退出）：持仓超过 N 天，**且**盈利不足（低于阈值）时应该考虑退出。

**实际含义：**
- `tc_remaining <= 0`：已超过 N 天
- `profit_rate >= tc_profit_threshold`：盈利已经达标 → **不需要退出**，所以 WARNING 语义是对的（超过期限且盈利足够，应该警告是否要卖）

但如果语义是"超过期限且盈利不足"，则应该是 `profit_rate < tc_profit_threshold` → WARNING。

**核查结论：** ⚠️ **审计有一定道理，逻辑需核实**。需要对照 exit_monitor.py 中 TC 的真实逻辑（exit_monitor.py 中无 time_exit 逻辑——grep 无结果）。TC 在 PLAN 中的原始设计语义需确认。

---

### [MEDIUM-1] T+1 保护边界条件缺失

**审计结论：** `main.py:1338-1358` 无 `holding_days < 2` 判断

**代码验证：** 确认整个 HS 块无 `holding_days` 条件判断

**核查结论：** ✅ **审计正确**。T+1 保护确实未实现。

---

### [MEDIUM-2] 进度条未实现，只有表格

**审计结论：** 前端只有 table，无进度条

**代码验证：** `live_trader.js:651-691` `renderRiskMonitor` 只输出 `<table>`，无 `<div>` 进度条

**核查结论：** ✅ **审计正确**。PLAN 设计稿（:23-48）明确有进度条，前端未实现。

---

### [MEDIUM-3] ATR 模式提示缺失

**审计结论：** `use_atr_trail=true` 时应标注"显示与实际触发可能存在偏差"

**代码验证：** `risk_items` 中无 `atr_note` 字段

**核查结论：** ✅ **审计正确**。

---

### [CRITICAL-2] 测试文件不存在

同 [HIGH-2]，合并结论：✅ 审计正确。

---

### [MEDIUM-4] 回归保护缺失

同 [HIGH-2]，合并结论：✅ 审计正确。

---

### [MEDIUM-5] 进度条是 PLAN 核心亮点，完全未实现

同 [MEDIUM-2]，合并结论：✅ 审计正确。

---

### [MEDIUM-6] `current_price` 显示昨收价误导用户

同 [HIGH-1]，合并结论：⚠️ 有一定道理，但实际有前端 `applyLiveQuotes` 覆盖机制。

---

## 三、额外隐患核查

### 隐患1: `lru_cache` 无清理机制

**代码验证：**
```python
# utils.py:9
@lru_cache(maxsize=1)
def _load_trading_calendar() -> set:
```

**核查结论：** ✅ **有道理**。交易日历文件更新后缓存不失效。但实际上进程启动后日历文件几乎不会变，缓存生命周期 = 进程生命周期，实际影响极小。

### 隐患2: TR 回撤方向未校验

**代码验证：**
```python
# main.py:1365
drawdown = peak_pnl_pct - current_pnl_pct  # 回撤百分点
```

**核查：** `drawdown` 负数时（盈利扩大场景），message 会显示"回撤-0.5%"，语义奇怪。

**核查结论：** ✅ **有道理**，但 message 不会影响交易逻辑，属 UX 问题。

### 隐患3: TP tiers 解析脆弱

**代码验证：**
```python
# main.py:1447-1455
try:
    triggered_list = _json.loads(tp_triggered) if isinstance(tp_triggered, str) else (tp_triggered or [])
    tp_triggered_flag = any(...)
except Exception:
    pass  # 静默失败
```

**核查结论：** ✅ **有道理**。解析失败时静默走 `False`，无日志。

### 隐患4: `entry_date` 为 None 时静默 fallback

**代码验证：**
```python
# utils.py:31-32
if entry_date is None:
    return 1
```

**核查结论：** ✅ **有道理**。但 `entry_date` 为 None 在 store 正常流转中几乎不会发生（store.upsert_position 不会写 None）。

### 隐患5: git diff 范围远超本次 scope

**审计描述：** git diff 显示 248 个文件变更

**核查：** 这指的是整个仓库的当前状态差异，不等于本次 commit 的影响范围。实际核查 `303d061` 只改了 6 个文件（+398/-89 行）。

**核查结论：** ❌ **审计报告混淆了"当前未暂存状态"和"本次 commit 实际改动"**。

---

## 四、总结

### 审计报告准确的部分（需修复）

| 问题 | 级别 | 核实 |
|------|------|------|
| TR 已触发 status="warning"（PLAN 设计就是 warning，非 error） | CRITICAL | ⚠️ 审计描述准确但判断有误——按 PLAN 设计 TR="warning" 是对的 |
| T+1 保护缺失 | MEDIUM | ✅ 确实缺失 |
| 进度条未实现 | MEDIUM | ✅ 确实只有 table |
| ATR 提示缺失 | MEDIUM | ✅ 确实缺失 |
| pytest 测试文件缺失 | HIGH | ✅ 确实不存在 |
| TR remaining 负数（输出前有 max(0) 兜底） | HIGH | ⚠️ 有 max(0) 兜底，非严重问题 |
| TP tiers 解析静默失败 | MEDIUM | ✅ 有道理但低风险 |

### 审计报告不准确的部分

| 问题 | 审计判断 | 实际情况 |
|------|----------|----------|
| TR 应为 danger 而非 warning | 错误 | PLAN 设计稿明确 TR="warning"（部分退出），exit_monitor 优先级也低于 HS/TF |
| `current_price` 是 bug | 错误 | 后端返回 last_close 做 fallback，前端 applyLiveQuotes 会用实时价覆盖 |
| git diff 248 个文件 | 错误 | 混淆了工作区状态和 commit 实际范围；本次 commit 只改 6 个文件 |
| TC 逻辑矛盾 | 待核实 | 需对照 exit_monitor 真实 TC 逻辑 |

### 实际需修复清单

| 优先级 | 问题 | 操作 |
|--------|------|------|
| P0 | T+1 保护缺失 | 在 HS 块加 `holding_days < 2` 判断 |
| P0 | pytest 测试文件缺失 | 按 PLAN:269-289 创建测试 |
| P1 | 进度条未实现 | 前端按 PLAN 设计实现进度条 |
| P1 | ATR 提示缺失 | 加 `atr_note` 字段 |
| P2 | TC 逻辑核实 | 对照 exit_monitor.py 确认 TC 真实语义 |
| P2 | TP 解析静默失败 | 加日志 |
| P3 | TR remaining 负数（加 `max(0, ...)` 在计算点而非输出点） | 改善代码清晰度 |

---

## 五、对审计报告的整体评价

| 维度 | 评分 | 说明 |
|------|------|------|
| 代码描述准确性 | 7/10 | 大部分描述准确，但混淆了工作区状态和 commit 范围 |
| PLAN 设计理解 | 5/10 | TR 优先级理解有误，将 PLAN 设计的 warning 判断为错误 |
| 功能完整性判断 | 8/10 | 进度条/测试/T+1 缺失的判断全部正确 |
| 安全性分析 | 7/10 | TP 解析静默失败等隐患发现合理 |

**4 个核心缺失项（审计完全正确）：** T+1 保护、进度条、ATR 提示、pytest 测试。

---

*核查完成时间: 2026-07-14 21:05 (周二)*
