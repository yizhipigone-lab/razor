# 项目成果审计报告 — 2026-07-14

> 审计范围：PR #6 auto_buy 前端UI + 实盘风控监控面板 v1 + batch-1 风控参数统一
> 审计标准：最高标准（架构 + 功能 + 测试 + 交互四维度）
> 审计日期：2026-07-14

---

## 审计对象

- **改动范围**：2个 commit（898d778 auto_buy UI + 303d061 风控监控面板）
- **原始要求**：PLAN-live-risk-monitor-2026-07-14.md 定义的功能
- **审查文件**：`app/live_trader/main.py`、`app/live_trader/utils.py`、`static/index.html`、`static/js/live_trader.js`、`app/config/risk_params.py`

---

## ✅ 要求 vs 实现比对

| 要求 | 实现情况 | 证据 | 结论 |
|---|---|---|---|
| 新建 `utils.py` 的 `calc_trading_days()` | ✅ 已实现 | `utils.py:24-44` | ✅ |
| `/live/config/risk-status` API | ✅ 已实现 | `main.py:1284-1503` | ✅ |
| `risk_params` 提到顶层 | ✅ 已实现 | `main.py:1305-1317` | ✅ |
| `last_close` 字段 | ✅ 已实现 | `main.py:1486` | ✅ |
| 轮询与 alerts.js 模式对齐 | ✅ 已实现 | `live_trader.js:694-701` | ✅ |
| `hold_days<2` 时 HS 显示 safe + T+1 标注 | ❌ **未实现** | `main.py:1338-1358` 无此逻辑 | ❌ |
| TR 已触发时 status="danger" | ❌ **代码写的是 warning** | `main.py:1373` | ❌ |
| TR `remaining` 负数兜底 | ❌ **未处理** | `main.py:1376` 负数不过滤 | ❌ |
| 进度条宽度公式 `min(remaining/budget*100,95)` | ❌ **未实现** | 前端只有表格，无进度条 | ❌ |
| ATR 模式标注"显示与实际触发可能存在偏差" | ❌ **未实现** | `main.py` 无此 message | ❌ |
| TC 条件逻辑 `(remaining<=0 AND pnl>=threshold)` | ⚠️ **逻辑可疑** | `main.py:1427` 待核实 | ⚠️ |
| pytest 测试文件 `test_live_trader_risk_monitor.py` | ❌ **不存在** | `tests/` 无此文件 | ❌ |
| `current_price` 用实时价（非 last_close） | ❌ **用的是 last_close** | `main.py:1484` | ❌ |

---

## 🏛️ 架构分析师发现

### [CRITICAL-1] TR 已触发状态语义错误

- **位置**：`main.py:1373`
- **问题**：TR 已触发时 `tr_status = "warning"`，但 PLAN 设计稿和 exit_monitor 实际优先级（TR 是全局退出）都要求 `danger`
- **影响**：前端渲染颜色错误，用户可能误判移动止盈已触发的严重性
- **证据**：
  ```python
  # main.py:1373
  tr_status = "warning"   # ← 错误，应为 "danger"
  ```
- **修复建议**：将 `main.py:1373` 的 `"warning"` 改为 `"danger"`

---

### [HIGH-1] `current_price` 字段误用昨收价代替实时价

- **位置**：`main.py:1484`
- **问题**：PLAN 要求 `current_price` 为实时价，但代码直接写 `last_close`
- **证据**：
  ```python
  # main.py:1484
  "current_price": float(pos.get("last_close") or 0),  # ← 昨收价，不是实时价
  ```
- **影响**：面板显示的"现价"实际是昨收价，与 PLAN 承诺的语义不符，用户看到的是昨日收盘价

---

### [HIGH-2] pytest 测试文件缺失

- **位置**：`tests/test_live_trader_risk_monitor.py` 不存在
- **问题**：H5 修复声称已加入 pytest，但 `Grep` 在 `tests/` 目录下未找到任何匹配文件
- **影响**：核心风控计算逻辑无自动化测试保护，回归风险高；资金相关逻辑没有测试是严重的安全隐患
- **修复建议**：创建测试文件，覆盖以下场景：
  - `test_risk_status_hs_triggered`：profit_rate=-6.7%，hard_stop=-6% → HS 已触发，remaining=0
  - `test_risk_status_hs_not_triggered`：profit_rate=-5%，hard_stop=-6% → HS 未触发，remaining=1%
  - `test_risk_status_hold_days_lt2`：hold_days=1 时 HS 显示 safe，message 含 T+1 保护
  - `test_risk_status_tr_not_triggered`：drawdown=1.5%，trail_dd=2% → TR 未触发
  - `test_risk_status_avg_cost_zero`：avg_cost<=0 时跳过风控计算

---

## ⚙️ 功能分析师发现

### [HIGH-3] TR `remaining` 负数未处理

- **位置**：`main.py:1376`
- **问题**：
  ```python
  tr_remaining = trail_dd_pct - drawdown if drawdown >= 0 else abs(drawdown)
  ```
  当 `drawdown=2.6`, `trail_dd_pct=2.0` 时 → `2.0 - 2.6 = -0.6`（负数！）
  - 已触发时（line 1371）remaining 已设为 0，但未触发时计算出来可能是负数
  - 负数 remaining 传入前端，进度条会显示异常宽度
- **证据**：实际计算 `2.0 - 2.6 = -0.6`
- **修复建议**：`main.py:1376` 改为 `max(0, trail_dd_pct - drawdown if drawdown >= 0 else 0)`

---

### [HIGH-4] TC 条件逻辑存疑

- **位置**：`main.py:1427`
- **代码**：
  ```python
  tc_status = "warning" if tc_remaining <= 0 and profit_rate >= tc_profit_threshold else "safe"
  ```
- **问题**：TC 语义是"持仓超 N 天且盈利不足"时触发 → 触发条件应为 `remaining<=0 AND pnl < threshold`（不足才触发）
- **当前逻辑**：`pnl >= threshold` 才 WARNING，与"盈利不足"的语义矛盾
- **修复建议**：核实 TC 真实触发逻辑，对照 exit_monitor.py 中 TC 规则的实际实现

---

### [MEDIUM-1] T+1 保护边界条件缺失

- **位置**：`main.py:1338-1358`（HS 计算块）
- **问题**：PLAN 明确要求 `hold_days<2` 时 HS 显示 safe 并标注"T+1保护"，代码中完全缺失此逻辑
- **证据**：整个 HS 块没有 `holding_days` 的条件判断
- **修复建议**：在 HS 块中加入 `if holding_days < 2: hs_status="safe"; hs_message="T+1保护，持仓不足2天不触发硬止损"`

---

### [MEDIUM-2] 进度条未实现，只有表格

- **位置**：`live_trader.js:651-691`
- **问题**：PLAN 设计稿花了大量篇幅描述进度条（越短越危险、颜色编码、宽度公式 `min(remaining/budget*100,95)`），前端实际只渲染了普通 table
- **影响**：视觉效果与 PLAN 原型差异大，用户无法直观感受"距离触发还有多远"的视觉量化信息
- **PLAN 设计**：
  ```
  已触发（remaining ≤ 0）: 100%, var(--red)
  剩余 > 0: min(remaining / budget * 100, 95)%, var(--yellow)
  safe: 0%, var(--green)
  ```

---

### [MEDIUM-3] ATR 模式提示缺失

- **位置**：API 响应和前端渲染
- **问题**：当 `use_atr_trail=true` 时，PLAN 要求标注"显示与实际触发可能存在偏差"，完全未实现
- **修复建议**：在 `risk_items` 的 TR 或单独字段中加入 `atr_note` 提示

---

## 🧪 测试师发现

### [CRITICAL-2] 测试文件不存在，无法验证核心逻辑

- 计划书承诺的 `test_live_trader_risk_monitor.py`（~80行，覆盖 HS 触发/未触发/边界）**不存在**
- 核心风控计算逻辑（HS 触发判断、TR 回撤计算、TF 天数判断）没有任何单元测试
- **这是阻塞项**：无测试就无法保证风控计算正确性

---

### [MEDIUM-4] 回归保护缺失

- 改动涉及 `main.py` 的 `/live/config/risk-status` 端点，但项目中无任何针对此端点的 API 测试
- 建议补充 `pytest` 测试覆盖以下路径：
  - HS 触发边界（profit_rate = -6% exactly）
  - TR 未触发边界（drawdown = 2% exactly）
  - `hold_days=1` 的 T+1 保护
  - `avg_cost <= 0` 的跳过逻辑
  - `peak_price` 为 null 时 TR 状态

---

## 🖱️ 交互响应发现

### [MEDIUM-5] 进度条是 PLAN 核心亮点，完全未实现

- PLAN 设计稿花了大量篇幅描述进度条（越短越危险、颜色编码、宽度公式）
- 前端实际只有一行简单的 table，没有进度条
- 用户看不到"距离触发还有多远"的视觉量化信息

---

### [MEDIUM-6] `current_price` 显示昨收价误导用户

- 持仓表旁边有"现价"列，但实际返回的是 `last_close`
- 在交易时间段，用户看到的是昨收价而非实时价，无法判断是否该处理

---

## 🤔 我额外想到的隐患

### 1. `lru_cache` 无清理机制

- **位置**：`utils.py:9` 的 `@lru_cache(maxsize=1)`
- **问题**：缓存了交易日历，但 `calc_trading_days` 每天只用一次，缓存意义不大
- **隐患**：如果日历文件更新了，缓存不会失效（进程生命周期内）
- **建议**：移除 `@lru_cache`，每次重新加载（或加文件 mtime 检查）

---

### 2. TR 回撤方向未校验

- **位置**：`main.py:1363-1365`
- **问题**：当 `profit_rate > peak_pnl_pct`（罕见行情反弹场景），`drawdown = peak - current` 会是负数
- **当前逻辑**：`tr_remaining = abs(drawdown)` 逻辑正确，但 message 显示"回撤-0.5%"会很奇怪
- **建议**：message 中加判断，负数回撤显示"浮盈扩大"而非"回撤"

---

### 3. TP tiers 解析脆弱

- **位置**：`main.py:1447-1455`
- **代码**：
  ```python
  triggered_list = _json.loads(tp_triggered) if isinstance(tp_triggered, str) else (tp_triggered or [])
  tp_triggered_flag = any(...)
  ```
- **问题**：没有校验 `triggered_list` 的数据结构，万一格式不对会静默失败（走 `except` 分支，`tp_triggered_flag=False`）
- **建议**：加日志记录解析失败的场景

---

### 4. `entry_date` 为 None 时静默 fallback

- **位置**：`utils.py:32` → `return 1`
- **问题**：store 里的 `entry_date` 如果真的是 None，说明这笔持仓没有入场日期记录，是数据质量问题，不应该静默 fallback 为 1
- **建议**：抛出警告或返回特殊值，让调用方知道数据不完整

---

### 5. git diff 范围远超本次 scope

- **问题**：git diff 显示 248 个文件变更、+35035/-8962 行
- **实际混入**：batch-1 风控参数统一、TDX 回测重构、quote_source 重构等
- **影响**：无法独立验证本次提交的真实影响范围，建议拆分为独立 PR

---

## 📊 总评

| 严重级别 | 数量 | 说明 |
|---|---|---|
| CRITICAL | 2 | TR状态错误 + 测试文件缺失 |
| HIGH | 4 | TR负数、T+1缺失、current_price昨收、TC逻辑存疑 |
| MEDIUM | 6 | 进度条未实现、ATR提示缺失、回归测试缺失、tp_triggered脆弱、lru_cache、entry_date None处理 |
| LOW | 3 | TR消息格式、回撤负数显示、日历缓存清理 |

**整体评分：4/10**

**是否可交付：否（阻塞项：2个CRITICAL）**

---

## 🔧 建议修复项（按优先级）

| 优先级 | 问题 | 修复方案 |
|---|---|---|
| P0 | [CRITICAL-1] TR状态错误 | `main.py:1373` `"warning"` → `"danger"` |
| P0 | [CRITICAL-2] 测试文件缺失 | 创建 `tests/test_live_trader_risk_monitor.py`，覆盖核心路径 |
| P1 | [HIGH-3] TR remaining负数 | `main.py:1376` 加 `max(0, ...)` |
| P1 | [HIGH-1] current_price用错 | 改用 store 实时价字段，或字段重命名为 `last_close` 并更新前端标签 |
| P1 | [HIGH-4] T+1保护缺失 | 在 HS 块加 `hold_days < 2` 判断 |
| P2 | [HIGH-5] TC逻辑存疑 | 对照 exit_monitor.py 核实 TC 真实触发逻辑 |
| P2 | [MEDIUM-2] 进度条未实现 | 前端按 PLAN 设计实现进度条 |
| P3 | [MEDIUM-3] ATR提示缺失 | 加条件提示文字 |
| P3 | [MEDIUM-4] 回归测试缺失 | 补充端到端 pytest |
| P3 | [MEDIUM-5] lru_cache清理 | 移除缓存或加 mtime 检查 |

---

*审计完成时间: 2026-07-14 17:32 (周二)*
