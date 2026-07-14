# 审计报告：实盘持仓风控实时监控面板

> 被审对象：`docs/PLAN-live-risk-monitor-2026-07-14.md`
> 审计日期：2026-07-14
> 审计类型：计划书质量审计（非代码审计）
> 方法：Read/Grep 实际代码验证 API 路径、函数名、字段、轮询机制、数据流

---

## 总体结论

**Verdict: FAIL — 5 项 HIGH 必须修复后再进入实施。**

方向正确，UI 草图清晰；但在实现细节与现有代码的对齐上存在多处断点：
- 引用了**不存在的函数**（`initLive()` / `startPolling()`）
- API 字段需求未与 `live_positions` 真实表结构对齐
- API 路径设计未与现有命名规范对齐
- ATR 止盈 / 多档止盈 / FD 等**已存在的风控维度被遗漏**
- 示例数据中 TC 和 TF 概念混淆（数学不自洽）

---

## HIGH — 阻塞级（须修复）

### H1 — 引用不存在的函数 `initLive()` 与 `startPolling()`

**位置：** `docs/PLAN-live-risk-monitor-2026-07-14.md:147`

**问题原文：**
> "在 `initLive()` 里加上 `loadRiskMonitor()`，并在 `startPolling()` 里每 15s 刷新。"

**证据：**
- `static/js/live_trader.js` 全文**没有** `initLive` 或 `startPolling` 函数定义
- 实际入口是 `async function loadLiveAll()`（`live_trader.js:542`），由 `main.js:1831` 的 `switchTab('live-trader')` 触发一次性加载
- 实盘 tab **没有自动 setInterval**：
  - 行情：main.js `setInterval(pollLiveQuotes, 5000)`（5s）
  - 持仓：仅切 tab 时刷新
- 15s 节奏仅存在于 `alerts.js:19-22`（告警 tab）

**风险：** 创建无人调用的函数，沦为"孤儿接口无人调"同类问题。

**修复建议：**
- 在 `loadLiveAll()` 里追加 `loadRiskMonitor()`
- 15s 轮询：在 `live_trader.js` 加 `_riskTimer = setInterval(loadRiskMonitor, 15000)`，由 main.js 的 `switchTab` 启停（对齐 alerts.js 模式）

---

### H2 — API 字段需求未对齐 `live_positions` 真实表结构

**位置：** `docs/PLAN-live-risk-monitor-2026-07-14.md:65-110`（响应 JSON）

**问题：**
- `live_positions` 表（`store.py:82-85`）实际列：`code, avg_cost, volume, can_use_volume, frozen_volume, pending_buy_volume, market_value, float_profit, profit_rate, peak_price, sell_count, entry_date, managed, strategy_name, tp_triggered, last_close`
- `holding_days` 不能简单 `(today - entry_date).days`（会多算周末/节假日），exit_monitor 已有 `_calc_hold_days` 用**交易日**计数
- `peak_pnl` 不是列名，需 API 端从 `peak_price - avg_cost` 算

**修复建议：**
- 复用 `_calc_hold_days` 逻辑
- `peak_pnl` 在 API 端计算
- 扩 `/live/positions` 直接补字段，避免新建独立 API

---

### H3 — API 路径风格与现有命名不一致

**位置：** `docs/PLAN-live-risk-monitor-2026-07-14.md:58`

**问题：** `GET /live/positions/risk-status` 二级嵌套，与现有 `/live/*` 风格不符。

**修复建议：** 改为 `GET /live/config/risk-status`（与 `/live/config/risk-params` 同命名空间）

---

### H4 — ATR 移动止盈模式未覆盖（已存在功能被遗漏）

**位置：** `docs/PLAN-live-risk-monitor-2026-07-14.md:86-96`

**问题：** `app/config/risk_params.py:34-35` 有 `use_atr_trail` / `atr_trail_multiplier`，当 ATR 模式启用时触发逻辑是 `(peak_price - current_price) >= ATR * multiplier`，不是固定百分比回撤。

**修复建议：**
- API 端按 `use_atr_trail` 分支返回不同的 trigger_value
- 或至少在面板标注"ATR 模式下显示可能与实际触发有偏差"

---

### H5 — 风控状态分类未对齐退出优先级表

**位置：** `docs/PLAN-live-risk-monitor-2026-07-14.md:82`

**问题：** exit_monitor `_reason_priority` 是持仓级别优先级（HS=120 → TF=115 → TP=110 → TR=105 → FD=90 → TC=20），当某持仓同时触发 HS 和 TR 时只执行 HS。面板若把 HS/TR 都标红会让用户误以为两个都会触发。

**修复建议：**
- 新增"风控状态判定规则"章节
- 每只持仓取其触发最高优先级的 status
- 文字说明"HS 触发时 TR 即使满足也不执行"

---

## MEDIUM — 警告级（应修复）

### M1 — 多档止盈（TP1=3%）未列入风险项

**位置：** risk_items 数组示例只有 HS/TR/TF

store 已有 `tp_triggered` 字段，`exit_monitor.py:147-149` 读此字段判断已触发 tier。持仓 +6% 时若 TP1=3% 早已触发，面板无任何提示。

**修复建议：** risk_items 新增 TP 数组（按 tier 数量 1~N），API 从 `tp_triggered` JSON 数组解析。

### M2 — FD（首日弱势离场）风险项未列入

**位置：** risk_items 示例

`risk_params.py:32-33` 有 `first_day_exit_min_profit` / `first_day_exit_days`，优先级 90。需列入。

### M3 — CSS 落点与 Token 命名未指明

**位置：** 进度条实现

- 项目只有 `static/css/main.css`
- 已有 token：`--red` / `--yellow` / `--green` / `--text2`
- 进度条用 inline `style="width: 70%; background: var(--red)"`，沿用现有 token

---

## LOW — 提示级

### L1 — 示例数据 TC 与 TF 概念混淆（数学不自洽）

**位置：** UI 示例

TF=强制清仓12天，持仓第4天，"距TF到期还4天"是合理的，但 TC 与 TF 在示例中同时出现容易混淆。

---

## 修复决策

| 问题 | 决策 | 原因 |
|------|------|------|
| H1 | 修复 | 现有代码无此函数，必须对齐实际入口 |
| H2 | 修复 | 数据字段不对齐实现会报错 |
| H3 | 修复 | API 路径风格须一致 |
| H4 | 记录偏差 | ATR 模式数据后端无存储，标注偏差即可 |
| H5 | 修复 | 错误的状态分类会导致用户误判 |
| M1 | 修复 | 已触发止盈用户必须可见 |
| M2 | 修复 | FD 是独立风控维度 |
| M3 | 修复 | 须明确 CSS 落在哪个文件 |
| L1 | 记录 | 示例问题不影响实现 |

---

## 审计摘要

- 发现：9 个（5 HIGH / 3 MEDIUM / 1 LOW）
- 修复：7 个
- 遗留（记录偏差不阻塞）：2 个（H4 ATR偏差 / L1 示例混淆）
