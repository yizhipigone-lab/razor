# 审计报告：实盘持仓风控监控面板 — v3 三次审计

> 被审对象：`docs/PLAN-live-risk-monitor-2026-07-14.md`（v3，声称"全部 18 个审计问题已对齐"）
> 审计日期：2026-07-14
> 审计类型：v3 自验审计 — 验证 18 个修复项是否真实落地，并对照代码检查是否引入新问题

---

## 总体结论

**Verdict: 部分通过 — 17 项修复真实落地，但 v3 修复 BE 遗漏问题时引入了新的 CRITICAL 错误。**

v2 → v3 的整体改动方向正确：示例数据数学自洽、私有方法提取、字段补全、模式对齐等都到位。但 BE 的引入暴露了两个严重问题：
1. **BE 的优先级排序与代码现状矛盾**（计划书说 BE(95-100)，但代码里 BE 没有显式优先级，被 `_reason_priority` 默认返回 0）
2. **优先级表里 BE 的位置数字逻辑错误**（把 BE 排在 TP 之前，但 BE=95-100 < TP=110）

---

## v2 → v3 修复验证（18 项）

| v2 问题 | v3 修复 | 验证结果 |
|---------|---------|---------|
| C1 HS 示例数学不自洽 | 已修正：remaining=0, message="已触发" | ✅ **真实修复** |
| C2 TR 示例数学不自洽 | 已修正：remaining=0, message="已触发" | ✅ **真实修复** |
| C3 私有方法 `_calc_hold_days` 调用 | 提取到 `app/live_trader/utils.py` 的 `calc_trading_days()` | ⚠️ **部分修复**（仍调 `_load_trading_calendar` 私有函数） |
| C4 遗漏 BE 风控维度 | risk_items 数组新增 BE 项 | ✅ **修复**但引入新问题（见下文 CRITICAL） |
| C5 remaining_pct 无定义 | 已移除该字段 | ✅ **真实修复** |
| H1 risk_params 冗余发送 | 已移到 JSON 顶层 | ✅ **真实修复** |
| H2 缺少 last_close | 已加入 | ✅ **真实修复** |
| H3 轮询与 alerts 模式不一致 | 改为 `window.startRiskPolling`/`stopRiskPolling` | ✅ **真实修复** |
| H4 hold_days < 2 处理 | 已在边界处理中说明 | ✅ **真实修复** |
| H5 无自动化测试 | 已加入 pytest 用例 | ✅ **真实修复** |
| M1 API 字段命名不一致 | 标记为"已对齐 dataclass" | ❌ **未真正修复**（见下文） |
| M2 max_sell_per_scan 限制 | 已加提示文字 + JSON 字段 | ✅ **真实修复** |
| M3 进度条公式未定义 | 已定义 width 映射公式 | ⚠️ **部分修复**（"对应预算"未明确定义） |
| M4 边界条件未处理 | 已加 avg_cost/peak_price/entry_date/hold_days 边界 | ✅ **真实修复** |
| M5 优先级表遗漏 BE | 已加 BE 到优先级表 | ❌ **修复但引入 CRITICAL 新错误**（见下文） |
| L1 默认展开无折叠逻辑 | 明确"不做折叠" | ✅ **真实修复** |
| L2 loadLiveAll 第 12 个请求 | 接受（必要数据） | ✅ **真实修复** |
| L3 TP1 message 表述 | 已优化 | ✅ **真实修复** |

---

## v3 新发现的问题

### CRITICAL — 阻塞级（必须修复）

#### C-V3-1 — BE 优先级排序数字逻辑自相矛盾

**位置：** 计划书第 51 行

```
HS(120) > TF(115) > BE(95-100) > TP(110) > TR(105) > FD(90) > TC(20)
```

**问题：** `>` 表示"优先级更高"，但按数值排序：
- HS=120 > TF=115 ✓
- TF=115 > BE=95-100 ✓（115 > 100）
- **BE=95-100 > TP=110 ✗**（100 < 110，BE 实际上比 TP 低）

数学上，TP(110) > BE(95-100)，所以正确顺序应该是：
```
HS(120) > TF(115) > TP(110) > TR(105) > BE(95-100) > FD(90) > TC(20)
```

**风险：** 风控面板的"全局状态判定"会按这个错误顺序取最高优先级。如果 BE 在面板上排在 TP 前，但实际 exit_monitor 处理时 TP 排在 BE 前，两者行为不一致。

---

#### C-V3-2 — 实际 exit_monitor._reason_priority 没有 BE

**位置：** `exit_monitor.py:178-195` vs 计划书第 51 行

**证据：** `_reason_priority` 的实际实现：

```python
if reason.startswith("HS"): return 120
if reason.startswith("TF"): return 115
if reason.startswith("TP"): return 110
if reason.startswith("TR"): return 105
if reason.startswith("FD"): return 90
if reason.startswith("TC"): return 20
return 0   # ← BE 落到这里，返回 0
```

**问题：** 计划书"不改任何下单/止损/止盈逻辑"（第 309 行），但 BE 在实际代码中没有显式优先级。如果 BE 触发，`_reason_priority` 会返回 0，**BE action 会被排到所有其他 action 之后**（在 `actions.sort(reverse=True)` 中）。

也就是说，计划书的优先级表说 BE=95-100，但代码实际会给 BE=0。这两套不一致。

**风险：** 用户看面板以为 BE 是中等优先级，但实际触发后被推迟处理。

**修复建议（任选其一）：**
1. 允许修改 `_reason_priority`，加上 BE 分支（推荐：`return 100`）
2. 或在"不做的事"里明确写"BE 优先级与代码一致（0，排在最末）"

---

#### C-V3-3 — BE 示例 message 文字误导用户

**位置：** 计划书第 174 行

```json
{
  "type": "BE",
  "activated": false,
  "remaining": -8.6,
  "message": "保本止损未激活（盈利8.6% > 阈值0%，需回撤至0%才触发）"
}
```

**问题：** message 中"盈利8.6% > 阈值0%" 这部分文字，从用户视角读起来是"我满足了条件"。但实际规则要求 **breakeven_threshold > 0** 才可能激活（见第 216 行定义）。threshold=0 意味着 BE 功能根本没启用。

**用户困惑：** "我已经满足条件了，为什么还没激活？"

**更准确的 message：** "BE 未启用（breakeven_threshold=0，需要配置大于 0 才生效）"

---

### HIGH — 高优先级（应修复）

#### H-V3-1 — M1 字段命名未真正对齐 dataclass

**位置：** 计划书第 111-112 行

```json
"time_exit_min_profit_pct": 0.03,
"time_exit_force_days": 12,
```

vs `app/config/risk_params.py` 第 30-31 行：

```python
time_exit_profit: float
time_force_days: int
```

**验证：** 计划书声称"M1 已对齐 dataclass"，但 API 字段名仍与 dataclass 字段名不一致：
- `time_exit_min_profit_pct` ≠ `time_exit_profit`
- `time_exit_force_days` ≠ `time_force_days`

dataclass 是更稳定的契约（frozen），API 字段应直接复用 dataclass 的 field 名。

---

#### H-V3-2 — `breakeven_threshold` / `breakeven_stop` 数据来源不明

**位置：** 计划书第 117-118 行

```json
"breakeven_threshold": 0.0,
"breakeven_stop": 0.0
```

**问题：** `risk_params.py` 的 `RiskParams` dataclass 里**没有** `breakeven_threshold` 和 `breakeven_stop` 字段。这两个值从哪里读？

- 是新的 settings key（未文档化）？
- 是新加到 dataclass 的字段（"不做的事"里没提）？
- 还是 `exit_rules.py` 里的 RuleContext 字段（需要在 response 里重新解析）？

**修复建议：** 明确字段来源，要么扩展 RiskParams dataclass，要么从 settings 新增 key。

---

#### H-V3-3 — `_load_trading_calendar` 仍是私有函数

**位置：** 计划书第 82 行（utils.py 中）

```python
from app.api.sim_trader import _load_trading_calendar
```

**问题：** 提取 `calc_trading_days()` 是为了"解决私有方法调用问题"，但函数内部依然调用 `_load_trading_calendar`（下划线前缀的私有函数）。封装问题没有真正解决，只是迁移了一个层级。

**修复建议：** 继续把交易日历加载函数也提取到 `app/live_trader/utils.py` 或独立的 calendar 模块。

---

### MEDIUM — 中优先级（建议修复）

#### M-V3-1 — 进度条"对应预算"未定义

**位置：** 计划书第 46 行

```
剩余 > 0：min(remaining / 对应预算 * 100, 95)%
```

**问题：** "对应预算"对每种规则是什么？
- HS：`|hard_stop|`？`|avg_cost|`？
- TR：`trail_dd`？`|peak_pnl|`？
- TF：`time_force_days`？某个时间窗口？

不同规则的预算基数差异很大，没有定义就实现不出来。

**修复建议：** 在公式里写清每个规则的 divisor 来源（如 HS=`abs(trigger_value)*avg_cost`、TR=`trail_dd`、TF=`trigger_days`）。

---

#### M-V3-2 — TC 风险项从示例消失

**位置：** v2 → v3 的 risk_items 数组对比

v2 示例包含 TC（时间条件退出），v3 示例不再列出。

**问题：** v2 审计没有把 TC 列为独立问题（因为 TF 已覆盖大部分场景），但 v3 完全移除 TC 后，需要明确说明：
- TC 是什么？（`rule_time_condition` 在 `exit_rules.py`，要求持仓超 N 天且盈利达标）
- 为什么从面板移除？（优先级太低 20，从未被触发？）
- 还是合并到了 TF？

---

### LOW — 提示级

#### L-V3-1 — v3 改动量统计微调不准确

**位置：** 计划书第 318-326 行

```
app/live_trader/utils.py    ~25 行
app/live_trader/main.py     ~140 行
static/index.html           ~14 行
static/js/live_trader.js    ~120 行
tests/test_live_trader_risk_monitor.py  ~80 行
合计 ~379 行
```

新增的 utils.py 实际包含 25 行（计划书中代码片段就是 25 行），但 API 端 risk-status 的实现会比 140 行多，因为有 6 类规则的计算函数（HS/TR/TF/BE/FD/TP1），每类 10-20 行 = 60-120 行，加上 JSON 组装 + 边界处理，总量约 200 行。改动量可能被低估。

---

## 审计摘要

| 类别 | 数量 | 详情 |
|------|------|------|
| v2 18 项修复中真实落地 | 14 | ✅ |
| 部分修复 | 2 | C3（仍调私有函数）、M3（"对应预算"未定义） |
| 未真实修复 | 2 | M1（字段名仍不一致）、M5（修复引入新错误） |
| v3 新引入问题 | 6 | 3 CRITICAL + 3 HIGH + 2 MEDIUM + 1 LOW |

**核心结论：** v3 修了大部分表面问题，但 BE 的引入暴露了**优先级表与代码现状的双重不一致**。修复 BE 的同时，应该对齐 exit_monitor 的优先级定义。

**建议下一步：**
1. 修复 C-V3-1/2：BE 优先级表与代码对齐（要么改 _reason_priority，要么改计划书）
2. 修复 C-V3-3：BE message 文字改为"未启用（阈值=0）"
3. 修复 H-V3-1：API 字段名严格对齐 dataclass（time_exit_profit 而非 time_exit_min_profit_pct）
4. 修复 H-V3-2：明确 breakeven_threshold/stop 字段数据来源

---

**与 v2 审计的对比：** v2 审计漏掉了 BE 维度的存在，v3 修复 BE 时未对照 `_reason_priority` 实际代码，导致引入了新的不一致。教训：**修复一个被遗漏的维度时，必须同时检查该维度在所有相关代码中的实际行为**。