# 审计报告：实盘持仓风控监控面板 — v2 二次审计

> 被审对象：`docs/PLAN-live-risk-monitor-2026-07-14.md`（v2，声称"已按审计意见修复"）
> 审计日期：2026-07-14
> 审计类型：计划书深度审计 — 逐字段对照实际代码验证
> 方法：Read exit_rules.py / exit_monitor.py / main.py / store.py / risk_params.py / live_trader.js / index.html，逐行验证计划中每个数字和字段

---

## 总体结论

**Verdict: FAIL — 3 项 CRITICAL 必须修复后再进入实施。**

v1 审计的 7 个修复基本到位（H1-H5、M1-M3 已处理），但本次深度审计发现了**上一轮审计遗漏的重大问题**：
- 示例数据中的 **HS 和 TR 数学计算全部不自洽**（数字与 exit_rules 实际逻辑矛盾）
- 遗漏了 **保本止损(BE)** 这一独立风控维度
- `_calc_hold_days()` 私有方法调用破坏了封装

---

## CRITICAL — 阻塞级（必须修复）

### C1 — HS 示例数学不自洽：已触发止损却说"还差 0.3%"

**位置：** 计划书第 29-30 行，risk_items[0]（HS 硬止损）

**问题：** 600519 的 `profit_rate=-6.7%`，`trigger_value=-6%`。因为 `-6.7% <= -6%`，**HS 已经触发**。但 `remaining=-0.3` 且 message 写"距硬止损 -6% 还差 0.3%"——这与数学事实矛盾。

**证据：**
- `exit_rules.py:117-119`：HS 触发条件为 `cur <= ctx.hard_stop`，即 `close/entry_price - 1 <= -0.06`
- `profit_rate = (1680-1800)/1800 = -6.67%`，确实 `<= -6%`，HS 已触发
- `remaining` 若是 `trigger_value - current_pnl = -6.0 - (-6.7) = 0.7`（正数，说明已越过）
- `remaining` 若是 `current_pnl - trigger_value = -6.7 - (-6.0) = -0.7`（负数，说明已越过）
- 无论哪种定义，`-0.3` 都对不上，且状态不应是"还差"

**正确应该是：**
```json
{
  "type": "HS",
  "status": "danger",
  "remaining": 0,
  "message": "已触发硬止损（当前-6.7% < 止损线-6%），HS 触发时 TR 即使满足也不执行"
}
```

---

### C2 — TR 示例数学不自洽：回撤已越线却说"距 TR 还差 1.4%"

**位置：** 计划书第 32-33 行，risk_items[1]（TR 移动止盈）

**问题：** 000858 的 `drawdown=2.6%`，`trail_dd=2%`（`trail_dd` 来自 `risk_params` 第 91 行）。`exit_rules.py:236-238`：TR 触发条件为 `close/peak_price - 1 <= -trail_dd`（兜底 close），即回撤 >= 2%。

验算：`close/peak_price - 1 = 1.086/1.112 - 1 = -2.34% <= -2%` → **TR 已触发！**

但 `remaining=1.4`、`status="warning"`、message 说"距 TR 还差 1.4%"。这只有在 `trail_dd=4%` 时才成立（2.6% + 1.4% = 4%），但与 `risk_params` 中 `trail_dd=0.02` 矛盾。

**同样**：`drawdown` 的定义是 `peak_pnl - current_pnl = 11.2% - 8.6% = 2.6%`，而实际 TR 判断用的是 `close/peak_price - 1`（价格回撤），两者数学上有细微差别但结论相同——都已触发。

**根本没有"1.4%"这个数字的出处。**

**正确应该是：**
```json
{
  "type": "TR",
  "status": "danger",
  "drawdown": 2.6,
  "remaining": 0,
  "message": "已触发移动止盈（回撤2.6% > 阈值2.0%）"
}
```

---

### C3 — `_calc_hold_days()` 是私有方法，API 端点不应直接调用

**位置：** 计划书第 161 行

**问题：**
- `_calc_hold_days` 是 `ExitMonitor` 的**私有方法**（下划线前缀），见 `exit_monitor.py:197`
- 它内部依赖 `from app.api.sim_trader import _load_trading_calendar()`——跨模块导入私有函数
- API 端直接 `exit_monitor._calc_hold_days(entry_date)` 破坏了封装，未来 ExitMonitor 内部重构会静默破坏 API

**修复建议：** 将交易日计数逻辑提取为独立工具函数（如 `app/live_trader/utils.py` 的 `calc_trading_days(entry_date)`），ExitMonitor 和 API 端点都调用它。

---

### C4 — 遗漏保本止损(BE)风控维度

**位置：** 计划书 risk_items 数组

**问题：** `exit_rules.py:295` 定义了 `rule_breakeven_stop`（保本止损），在 `ALL_RULES` 和 `ALL_RULES_TRAILING` 注册表中都存在（priority 95-100）。

这意味着：持仓盈亏从盈利回撤到保本线时，BE 规则会触发卖出。它是一个独立的风控维度，优先级介于 TP(110) 和 FD(90) 之间。

**计划书完全没有提到 BE。**

**修复建议：** risk_items 中增加 BE 类型，或至少在"不做的事"中说明为什么忽略 BE（例如 BE 与 TR 重叠度高，合并展示）。

---

### C5 — `remaining_pct` 无定义且数字不自洽

**位置：** 计划书第 108 行

**问题：** HS 的 `remaining_pct: 4.8`——没有文档说明这个百分比的分母是什么。
- 若分母是 `|trigger_value| = 6`：`remaining_pct = (0.3/6)*100 = 5%`，不是 4.8
- 若分母是 `|current_pnl| = 6.7`：`remaining_pct = (0.3/6.7)*100 = 4.48%`，不是 4.8
- 若分母是 `|entry_price| = 1800`：毫无意义

`remaining_pct` 的计算公式必须在计划书中明确定义。

---

## HIGH — 高优先级（应修复）

### H1 — `risk_params` 冗余发送

**位置：** 计划书 JSON 响应第 88-100 行

**问题：** 每只持仓的响应都包含完整的 `risk_params` 对象（12 个字段，~300 bytes）。若有 10 只持仓，就是 ~3KB 冗余数据。`risk_params` 对所有持仓相同，应放在响应顶层。

**修复建议：** 将 `risk_params` 提升到 JSON 响应的顶层，positions 数组中只保留 `code`/`name`/持仓数据/`risk_items`。

---

### H2 — 缺少 `last_close` 字段

**位置：** 计划书 JSON 响应

**问题：** `live_positions` 表有 `last_close` 列（`store.py:90`），用于过夜持仓的今日盈亏计算（口径：现价 - 昨收）。前端持仓表已经用 `data-lastclose` 属性传递此值。

风控面板若需要展示"今日盈亏"或区分 T+0/T+1 持仓，`last_close` 是必要字段。

---

### H3 — 轮询管理方式与 alerts 模式不一致

**位置：** 计划书第 207-209 行

**问题：** 计划用 `clearInterval(_riskTimer)` / `setInterval(loadRiskMonitor, 15000)` 在 `live_trader.js` 内部管理。

但 `alerts.js` 用的是**暴露全局函数 + switchTab 中央管理**模式：
```js
// main.js:1834-1838
if (name === 'alerts') {
    if (typeof startAlertsPolling === 'function') startAlertsPolling();
} else {
    if (typeof stopAlertsPolling === 'function') stopAlertsPolling();
}
```

两套机制并存会让代码库中出现两种轮询管理模式，增加维护负担。统一用 alerts 模式（暴露 `startRiskPolling()`/`stopRiskPolling()`）。

---

### H4 — `hold_days < 2` 时 HS 不生效未处理

**位置：** 计划书风控状态判定规则（第 52-57 行）

**问题：** `rule_hard_stop:104-105`——`hold_days < 2` 时 HS 直接返回 None（T+1 保护：首日不触发止损，防止买入当日即触发止损的误判）。

但计划书没有讨论持仓 < 2 天时的 HS 显示逻辑——应该显示"safe"并标注"T+1 保护，持仓不足 2 天不触发"。

---

### H5 — 无自动化测试计划

**位置：** 计划书验证方式（第 222-228 行）

**问题：** 全部 5 项验证都是手动操作。对于涉及数学计算的核心功能（风控状态判定、remaining 计算），应至少包含：
- 后端：pytest 测试 `/live/config/risk-status` 端点的 risk_items 计算正确性（用已知参数的 mock 持仓）
- 前端：至少验证 renderRiskMonitor 不抛异常、正确渲染 3 种状态颜色

---

## MEDIUM — 中优先级（建议修复）

### M1 — API 字段命名与 risk_params dataclass 不一致

**位置：** 计划书 JSON 第 93-98 行

| API 字段 (计划) | risk_params.py 字段 | 
|---|---|
| `time_exit_min_profit_pct` | `time_exit_profit` |
| `time_exit_force_days` | `time_force_days` |

命名风格不统一会增加后续维护混淆。建议 API 端用 dataclass 的字段名或统一加 `_pct` 后缀。

---

### M2 — `max_sell_per_scan` 限制未在面板体现

**位置：** 整个风控面板设计

**问题：** `exit_monitor.py:83-87`——单次扫描最多卖出 3 只。如果 5 只持仓同时触发 HS，实际上只会卖出优先级最高的 3 只。风控面板若把所有 5 只都标红 danger，用户会以为全部会卖出，但实际只有 3 只会被处理。

**修复建议：** 至少加一条提示文字或 tooltip："单次扫描卖出上限 3 只，超出部分下次扫描处理"。

---

### M3 — 进度条宽度计算公式未定义

**位置：** 计划书 UI 草图

**问题：** 进度条展示了视觉化的"剩余空间"，但没有定义 `width` 百分比如何从 `remaining` 值映射。例如：
- HS：remaining=-0.3（已越过）→ 宽度 0% 还是 100%？
- TR：何时用剩余空间算宽度，何时用激活状态算？

应在计划中定义映射公式，避免前后端各自拍脑袋实现。

---

### M4 — 未讨论 `avg_cost=0` / `peak_price=null` / `entry_date=null` 边界

**位置：** 整个后端实现

**问题：** `exit_monitor.py:133-137` 已处理 `avg_cost <= 0 or last_price <= 0` 的跳过逻辑。但计划书的 risk_items 生成逻辑没有说明这些边界情况下的行为：
- `avg_cost=0` → profit_rate 无法计算 → 所有 risk_items 如何展示？
- `peak_price=0` → peak_pnl 无法计算 → TR 如何展示？
- `entry_date=null` → holding_days 无法计算 → TF/TC/FD 如何展示？

---

### M5 — `exit_monitor._reason_priority` 与计划书优先级表有差异

**位置：** 计划书第 52 行 vs `exit_monitor.py:178-195`

**计划书：** HS(120) > TF(115) > TP(110) > TR(105) > FD(90) > TC(20)
**实际代码：** HS(120) > TF(115) > TP(110) > TR(105) > FD(90) > TC(20)

优先级一致 ✓。但计划书遗漏了 `exit_rules.py:295-296` 中的 `rule_breakeven_stop`（BE），其优先级为 95-100（介于 TP 和 FD 之间）。

---

## LOW — 提示级

### L1 — 计划书声称"默认展开"，但 HTML 片段无折叠逻辑

**位置：** 计划书第 11 行 vs 第 172-180 行

计划说"默认展开"，但 HTML 片段只是普通 card，没有任何折叠/展开的 HTML 结构。如果以后要加折叠，需要额外改动。不过既然明确说"不做折叠"，这在当前版本没问题。

---

### L2 — `loadLiveAll()` 一次性调用 vs 渐进式风险计算

**位置：** 计划书第 208 行

`loadLiveAll()` 已调用 **11 个异步函数**（`loadLiveStatus`, `loadLiveAsset`, `loadLivePositions`, ...）。再加 `loadRiskMonitor()` = 第 12 个。所有函数并发发起 HTTP 请求，后端可能同时收到 12 个请求。风控 API 若计算量大（每只持仓遍历 6 条规则），应考虑是否有必要在 `loadLiveAll()` 首次加载时也触发，还是只靠 15s 轮询。

---

### L3 — 示例中 600519 的 `global_status` 正确但 `risk_items[4]` TP1 的 message 表述可优化

**位置：** 计划书第 148-150 行

TP1 message："3%触发，已触发时卖出30%，剩余仓位由TR保护"——这句话读起来像文档描述而非实时状态。如果 TP1 未触发，应更直白："止盈1档(3%)未触发，当前距触发还差 X%"。如果已触发，应标注"已触发，已卖出30%仓位"。

---

## 审计摘要

| 严重级别 | 数量 | 说明 |
|---------|------|------|
| CRITICAL | 5 | C1-C5：示例数学不自洽 + 私有方法调用 + 遗漏 BE 规则 |
| HIGH | 5 | H1-H5：冗余数据、缺字段、模式不一致、边界未处理、无测试 |
| MEDIUM | 5 | M1-M5：命名不一致、限制未体现、进度条公式缺失 |
| LOW | 3 | L1-L3：表述优化 |
| **合计** | **18** | |

v1 审计修复了 7 个问题，但本轮的 **C1/C2（示例不自洽）和 C4（遗漏 BE）** 是上一轮审计完全漏掉的，属于计划书质量的核心缺陷。

**建议：** 修复全部 5 个 CRITICAL 后再进入实施。特别是 C1/C2——如果连示例数据都算不对，实现出 bug 的概率极高。
