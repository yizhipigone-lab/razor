# 优化器搜索空间卡片 — 前端渲染 + 联动写入

> 状态: 已确认（v2 修订） | 日期: 2026-06-27

## 1. 问题陈述

系统参数配置 tab 下的"🎯 优化器搜索空间"卡片目前是空占位：
- `renderSearchSpace()` 为 stub（不渲染任何内容）
- `saveSearchSpace()` 为 stub（不保存任何修改）
- 用户无法查看或修改 AI 优化器的搜索边界参数
- AI 优化得到最优参数后，无法一键写入止盈止损卡片

后端能力（`core/settings.py` / `config/app_setting.json` / `ai_optimizer.py`）已完整，仅缺前端实现。

## 2. 盘查结论（14 参数，全部在用）

对 `app_setting.json`、`exit_rules.py`、`engine.py`、`ai_optimizer.py`、`llm_advisor.py` 五处交叉对比：

### 2.1 app_setting.json 已有 10 个参数

| JSON Key | exit_rules 规则 | engine.py | ai_optimizer | 状态 |
|---|------|------|------|------|
| `tp1_profit` | `rule_take_profit` (P60) | ✅ | ✅ _lhs_sample | **在用** |
| `tp2_profit` | `rule_take_profit` (P60) | ✅ | ✅ | **在用** |
| `tp1_ratio` | `rule_take_profit` (P60) | ✅ | ✅ | **在用** |
| `tp2_ratio` | `rule_take_profit` (P60) | ✅ | ✅ | **在用** |
| `hard_stop_loss_pct` | `rule_hard_stop` (P100) | ✅ | ✅ | **在用** |
| `trailing_activate_pct` | `rule_trailing_stop` (P40) | ✅ | ✅ | **在用** |
| `trailing_drawdown_pct` | `rule_trailing_stop` (P40) | ✅ | ✅ | **在用** |
| `breakeven_threshold_pct` | `rule_breakeven_stop` (P95) | ✅ | ✅ | **在用** |
| `breakeven_stop_pnl_pct` | `rule_breakeven_stop` (P95) | ✅ | ✅ | **在用** |
| `time_exit_days` | `rule_time_exit` (P20) | ✅ | ✅ | **在用** |

### 2.2 缺口参数（exit_rules/engine 在用，但搜索空间缺失）

| JSON Key | 来源 | 使用位置 |
|---|------|------|
| `tp3_profit` | FALLBACK_SEARCH_SPACE | engine.py `_simulate_trade_v2`, `POST /api/backtest/ai/apply` |
| `tp3_ratio` | FALLBACK_SEARCH_SPACE | engine.py, `POST /api/backtest/ai/apply` |
| `time_exit_force_days` | _default_search_space | exit_rules `rule_time_force` (P80), engine.py |
| `time_exit_profit` | _default_search_space | exit_rules `rule_time_exit` (P20) |
| `first_day_exit_min_profit` | _default_search_space | exit_rules `rule_first_day_exit` (P90), engine.py |
| `first_day_exit_days` | _default_search_space | exit_rules `rule_first_day_exit` (P90), engine.py |

### 2.3 发现的问题

| 问题 | 说明 |
|------|------|
| tp3 未入搜索空间 | FALLBACK 有 tp3_profit/tp3_ratio，但 app_setting 没有 |
| time_exit_force_days 未入搜索空间 | exit_rules `rule_time_force` 在用，但只靠 FALLBACK |
| first_day 系列未入搜索空间 | exit_rules `rule_first_day_exit` 在用，但只靠 `_default` / config |
| breakeven 默认值矛盾 | exit_rules 默认 `0.0`（禁用），app_setting 的 min=2.0（永远启用） |

**本次只处理前端渲染，不在本次修默认值矛盾。**

## 3. 目标

1. **渲染搜索空间**：14 个参数，按 5 组渲染 min / max / step 输入框
2. **缺键优雅降级**：app_setting 中没有的参数，从 FALLBACK 取默认值渲染，保存时同步写入
3. **独立保存**：保存此卡不干扰其他设置卡片
4. **联动写入按钮**：复用已有 `POST /api/backtest/ai/apply` 端点，取整 1 位小数

## 4. 数据模型

### 4.1 参数映射（14 参数 × 5 分组）

```javascript
const SEARCH_SPACE_PARAMS = [
  // ── 阶梯止盈 ──
  { key: 'tp1_profit',             label: '止盈1 盈利%',   group: '阶梯止盈', isInt: false },
  { key: 'tp2_profit',             label: '止盈2 盈利%',   group: '阶梯止盈', isInt: false },
  { key: 'tp3_profit',             label: '止盈3 盈利%',   group: '阶梯止盈', isInt: false },
  { key: 'tp1_ratio',              label: '止盈1 卖出%',   group: '阶梯止盈', isInt: false },
  { key: 'tp2_ratio',              label: '止盈2 卖出%',   group: '阶梯止盈', isInt: false },
  { key: 'tp3_ratio',              label: '止盈3 卖出%',   group: '阶梯止盈', isInt: false },
  // ── 止损 ──
  { key: 'hard_stop_loss_pct',     label: '硬止损%',       group: '止损',     isInt: false },
  { key: 'breakeven_threshold_pct',label: '保本触发%',     group: '止损',     isInt: false },
  { key: 'breakeven_stop_pnl_pct', label: '保本线%',       group: '止损',     isInt: false },
  // ── 移动止盈 ──
  { key: 'trailing_activate_pct',  label: '移动激活%',     group: '移动止盈', isInt: false },
  { key: 'trailing_drawdown_pct',  label: '移动回撤%',     group: '移动止盈', isInt: false },
  // ── 时间 ──
  { key: 'time_exit_days',         label: '退出天数',      group: '时间',     isInt: true  },
  { key: 'time_exit_force_days',   label: '强制退出天',    group: '时间',     isInt: true  },
  // ── 首日弱势 ──
  { key: 'first_day_exit_min_profit', label: '目标涨幅%',  group: '首日弱势', isInt: false },
  { key: 'first_day_exit_days',    label: '有效天数',      group: '首日弱势', isInt: true  },
];
```

### 4.2 缺键降级源

```javascript
const FALLBACK_SEARCH_SPACE = {
  // 从 llm_advisor.py 的 FALLBACK_SEARCH_SPACE 同级同步
  tp3_profit:              { min: 18.0, max: 30.0, step: 1.0 },
  tp3_ratio:               { min: 0.2,  max: 0.4,  step: 0.05 },
  time_exit_force_days:    { min: 3,    max: 12,   step: 1 },
  first_day_exit_min_profit:{ min: 1.0, max: 5.0,   step: 0.5 },
  first_day_exit_days:     { min: 1,    max: 3,    step: 1 },
};
```

### 4.3 存储位置不变

`config/app_setting.json` → `optimizer.search_space`。保存时，缺键从 FALLBACK 取默认值的参数**也一并写入**，实现"首次保存自动补齐"。

## 5. UI 设计

```
┌────────────────────────────────────────────────────────────────────────┐
│ 🎯 优化器搜索空间                                     [💾 保存此卡]    │
│ AI 参数优化的搜索边界，值越小收敛越快。                     ✓ 已保存   │
│ ───────────────────────────────────────────────────────────────────── │
│                                                                        │
│  ── 阶梯止盈 ─────────────────────────────────────────────────────── │
│  参数            min          max         step                        │
│  tp1_profit  止盈1 盈利%  [  2.0 ] ~ [  6.0 ]  步长 [ 0.5 ]          │
│  tp2_profit  止盈2 盈利%  [ 10.0 ] ~ [ 18.0 ]  步长 [ 1.0 ]          │
│  tp3_profit  止盈3 盈利%  [ 18.0 ] ~ [ 30.0 ]  步长 [ 1.0 ]          │
│  tp1_ratio   止盈1 卖出%  [  0.10] ~ [  0.30]  步长 [0.05]           │
│  tp2_ratio   止盈2 卖出%  [  0.40] ~ [  0.70]  步长 [0.05]           │
│  tp3_ratio   止盈3 卖出%  [  0.10] ~ [  0.50]  步长 [0.05]           │
│                                                                        │
│  ── 止损 ───────────────────────────────────────────────────────     │
│  hard_stop   硬止损%       [ -7.0 ] ~ [ -4.5 ]  步长 [ 0.5 ]          │
│  breakeven   保本触发%     [  2.0 ] ~ [  6.0 ]  步长 [ 0.5 ]          │
│  breakeven_stop 保本线%    [  0.5 ] ~ [  2.0 ]  步长 [ 0.5 ]          │
│                                                                        │
│  ── 移动止盈 ─────────────────────────────────────────────────────── │
│  trail_act   移动激活%     [  1.0 ] ~ [  6.0 ]  步长 [ 0.5 ]          │
│  trail_dd    移动回撤%     [  0.5 ] ~ [  2.5 ]  步长 [0.25]           │
│                                                                        │
│  ── 时间 ───────────────────────────────────────────────────────     │
│  time_exit   退出天数      [    2 ] ~ [    8 ]  步长 [ 1   ]          │
│  time_force  强制退出天    [    3 ] ~ [   12 ]  步长 [ 1   ]          │
│                                                                        │
│  ── 首日弱势 ─────────────────────────────────────────────────────── │
│  first_day_profit 目标涨幅% [ 1.0 ] ~ [ 5.0 ]  步长 [ 0.5 ]          │
│  first_day_days   有效天数  [   1 ] ~ [   3 ]  步长 [ 1   ]          │
│                                                                        │
│ ───────────────────────────────────────────────────────────────────── │
│  💡 灰色参数 = 首次保存后自动写入 app_setting.json                     │
│                                                                        │
│  [▶ 应用 AI 最优参数到止盈止损卡片]                                   │
└────────────────────────────────────────────────────────────────────────┘
```

## 6. API 设计

### 6.1 GET /api/settings（已有，不修改）

加载时已返回 `optimizer.search_space`。缺键时该字段不存在，前端用 FALLBACK 补。

### 6.2 POST /api/backtest/ai/apply（已有，不修改，复用）

端点 `POST /api/backtest/ai/apply` 已存在（`app/api/backtest.py:216-290`），前端直接复用。

功能：
- 取 `body.params`（AI 最优参数）→ 写入 risk 字段
- 同时更新 `optimizer.search_space` 基线
- 取整策略：`round(raw_val, 1)`

### 6.3 POST /api/settings/list/optimizer_search_space（新增）

独立原子端点，不通过通用 `POST /api/settings`。

```
请求:  POST /api/settings/list/optimizer_search_space
      Content-Type: application/json
      Body: { "items": { "tp1_profit": {"min": 2.0, "max": 6.0, "step": 0.5}, ... } }

响应:  200 OK  { "status": "ok", "message": "搜索空间已保存" }
```

**实现**: `app/api/system.py` 新增路由，调用 `settings.set("optimizer", "search_space", items, save=True)`。

## 7. 前端设计

### 7.1 renderSearchSpace(data)

输入：`data` 来自 `optimizer.search_space`，可能缺少部分 key。

1. 遍历 `SEARCH_SPACE_PARAMS` 14 个定义
2. 每个参数从 `data` 取值；缺失时从 `FALLBACK_SEARCH_SPACE` 取默认
3. 按 group 分组渲染，每组加 `<div class="ss-group-title">── 组名</div>` 分隔
4. `isInt=true` 的参数 input step=1，否则 step=0.01（允许任意精度输入，展示的 step 字段独立）

### 7.2 saveSearchSpace()

1. 收集 14 行输入值（包括缺键已补的参数）
2. 校验：min < max && step > 0
3. POST `/api/settings/list/optimizer_search_space`
4. 成功 → 绿字 "✓ 已保存"（2s 淡出），失败 → 红字提示

### 7.3 applyAiBestToRisk()

1. GET `/api/backtest/ai/status` → 取 `best_params`
2. 若无 `best_params` → alert "暂无 AI 优化结果"
3. 对 best_params 做 `round(val, 1)` 取整
4. POST `/api/backtest/ai/apply` → body `{ params: roundedParams }`
5. 弹 confirm 显示取整前后对比
6. 用户确认后 → 刷新止盈止损卡片 input → `addLog("ok", ...)`

## 8. 交互流程

```
进入"系统参数配置"tab
  → loadSettings()
  → renderSearchSpace(optimizer.search_space)  // 14 参数，分组渲染

用户修改搜索空间 → 点"保存此卡"
  → saveSearchSpace()
  → POST /api/settings/list/optimizer_search_space
  → 绿字 "✓ 已保存"

用户点"应用 AI 最优参数到止盈止损卡片"
  → applyAiBestToRisk()
  → GET /api/backtest/ai/status → 取 best_params
  → round(val, 1) 取整
  → POST /api/backtest/ai/apply { params: rounded }
  → 弹 confirm 确认
  → 自动更新止盈止损卡片 input
  → addLog("ok", "AI 最优参数已应用")
```

## 9. 错误处理

| 场景 | 处理 |
|---|---|
| 搜索空间为空 | renderSearchSpace 用全部 FALLBACK 默认值渲染 |
| 部分参数缺键 | 缺键的行用 FALLBACK 填充，保存后自动补齐 |
| 输入 min ≥ max 或 step ≤ 0 | 前端校验，alert + 阻止提交 |
| 保存 API 网络错误 | 红字 "✗ 网络错误"，保留输入 |
| AI 无历史优化结果 | best_params 为空，alert 提示 |
| AI 结果缺少部分字段 | 缺失字段跳过不写入 |
| 并发保存冲突 | 最后保存覆盖（与现有行为一致） |

## 10. 向后兼容

- ❌ 不修改 `POST /api/settings`
- ❌ 不修改 `renderRiskTiers` / `saveRiskSettings`
- ❌ 不修改 `ai_optimizer.run()` 的 search_space 读取路径
- ❌ 不修改 `POST /api/backtest/ai/apply`（直接复用）
- ✅ `renderSearchSpace` 替换空 stub，无破坏风险
- ✅ 缺键从 FALLBACK 补默认值，不影响已有数据

## 11. 改动文件清单

| 文件 | 改动 | 行数估计 |
|---|---|---|
| `static/js/main.js` | 实现 `renderSearchSpace()` + `saveSearchSpace()` + `applyAiBestToRisk()` + `SEARCH_SPACE_PARAMS` + `FALLBACK_SEARCH_SPACE` | ~120 行 |
| `app/api/system.py` | 新增 `POST /api/settings/list/optimizer_search_space` | ~15 行 |
| `static/index.html` | 无改动（HTML 已存在） | 0 |
| `config/app_setting.json` | 无改动（保存时自动补齐缺键） | 0 |
| `core/settings.py` | 无改动（property 已存在） | 0 |
| `app/api/backtest.py` | 无改动（复用已有 `POST /api/backtest/ai/apply`） | 0 |

## 12. 未纳入范围

- 参数动态增删（用户选了固定列表）
- breakeven 默认值矛盾修复（exit_rules 默认 0.0 但搜索空间 min=2.0）
- tp1/tp2/tp3 step 与取整策略的统一（本次按 FALLBACK 值渲染，不强制统一）
- 多组搜索空间（只支持 1 组全局搜索空间）
