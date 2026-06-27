# 优化器搜索空间卡片 — 前端渲染 + 联动写入

> 状态: 已确认 | 日期: 2026-06-27

## 1. 问题陈述

系统参数配置 tab 下的"🎯 优化器搜索空间"卡片目前是空占位：
- `renderSearchSpace()` 为 stub（不渲染任何内容）
- `saveSearchSpace()` 为 stub（不保存任何修改）
- 用户无法查看或修改 AI 优化器的搜索边界参数
- AI 优化得到最优参数后，无法一键写入止盈止损卡片

后端能力（`core/settings.py` / `config/app_setting.json` / `ai_optimizer.py`）已完整，仅缺前端实现。

## 2. 目标

1. **渲染搜索空间**：9 个固定参数的 min / max / step 输入框
2. **独立保存**：保存此卡不干扰其他设置卡片
3. **联动写入按钮**：将 AI 最优参数（取整 1 位小数）一键写入止盈止损卡片

## 3. 数据模型

### 3.1 存储位置不变

`config/app_setting.json` → `optimizer.search_space`，9 个固定参数：

```json
{
  "optimizer": {
    "search_space": {
      "tp1_profit":         {"min": 2.0,  "max": 6.0,  "step": 0.5},
      "tp2_profit":         {"min": 10.0, "max": 18.0, "step": 1.0},
      "tp1_ratio":          {"min": 0.1,  "max": 0.3,  "step": 0.05},
      "tp2_ratio":          {"min": 0.2,  "max": 0.5,  "step": 0.05},
      "hard_stop_loss_pct":  {"min": -7.0, "max": -4.5, "step": 0.5},
      "trailing_activate_pct": {"min": 1.0, "max": 6.0, "step": 0.5},
      "trailing_drawdown_pct": {"min": 1.5, "max": 6.0, "step": 0.5},
      "time_exit_days":      {"min": 5,   "max": 20,  "step": 1},
      "time_exit_force_days": {"min": 8,   "max": 25,  "step": 1}
    }
  }
}
```

### 3.2 参数名 → 中文标签映射

| JSON Key | 中文标签 |
|---|---|
| `tp1_profit` | 止盈1 盈利% |
| `tp2_profit` | 止盈2 盈利% |
| `tp1_ratio` | 止盈1 卖出% |
| `tp2_ratio` | 止盈2 卖出% |
| `hard_stop_loss_pct` | 硬止损% |
| `trailing_activate_pct` | 移动激活% |
| `trailing_drawdown_pct` | 移动回撤% |
| `time_exit_days` | 退出天数 |
| `time_exit_force_days` | 强制退出天 |

## 4. API 设计

### 4.1 GET /api/settings（已有，不修改）

加载时已返回 `optimizer.search_space`，无需改动。

### 4.2 POST /api/settings/list/optimizer_search_space（新增）

**独立原子端点**，不通过通用 `POST /api/settings`。

```
请求:  POST /api/settings/list/optimizer_search_space
      Content-Type: application/json
      Body: {
        "items": {
          "tp1_profit": {"min": 2.0, "max": 6.0, "step": 0.5},
          ...
        }
      }

响应:  200 OK
      {"status": "ok", "message": "搜索空间已保存"}
```

**后端实现**：`app/api/system.py` 新增路由，调用 `settings.set("optimizer", "search_space", items, save=True)`。

### 4.3 POST /api/backtest/ai/apply-best（新增）

取最近一次 AI 优化的最佳参数 → 取整 → 写入 risk 字段。

```
请求:  POST /api/backtest/ai/apply-best
      Body: {}  （空，默认最近一次 AI 结果）

响应:  200 OK
      {
        "status": "ok",
        "message": "最优参数已应用到止盈止损",
        "raw": {"tp1_profit": 3.72, "hard_stop": -5.83, ...},
        "applied": {"tp1_profit": 3.7, "hard_stop": -5.8, ...}
      }

错误:  404
      {"status": "error", "message": "暂无 AI 优化结果"}
```

**后端实现**：`app/api/backtest.py` 新增路由。
- 数据源优先级：`ai_optimizer._last_best_params`（内存）→ `output/backtest_results/` 最新 JSON（文件）
- 取整：`round(raw_val, 1)`
- 写入：调用 `settings.set("risk", <field>, <value>, save=True)` 对每个可映射参数
- 参数映射表（后端维护）：`tp1_profit` → `risk.take_profit_tiers[0].profit_pct`、`hard_stop` → `risk.hard_stop_loss_pct` 等

## 5. 前端设计

### 5.1 renderSearchSpace(data)

输入：`data` 为 `{key: {min, max, step}, ...}` 字典。

渲染逻辑：
```
若 data 为空或键数 < 1 → 显示灰色提示 "暂无搜索空间配置"
否则 → 渲染表头行 + 9 行输入框，每行 3 个 input[type=number]
     → 行左侧显示中文标签
     → step 字段按参数自身 step 设置 input 的 step 属性
```

HTML 结构约定：
- 容器 `#search-space-list`
- 每行 class `ss-row`，3 个 input class `ss-min` / `ss-max` / `ss-step`，通过 `data-key` 标识参数名

### 5.2 saveSearchSpace()

从 9 行收集输入值 → 构造 `{items: {key: {min, max, step}, ...}}` → POST → 显示保存结果。

**前端校验**：
- min 必须 < max（否则 alert + 阻止提交）
- step 必须 > 0（否则 alert + 阻止提交）
- 空值跳过该参数（不保存）

### 5.3 applyAiBestToRisk()

1. 先 POST `/api/backtest/ai/apply-best`
2. 若返回 error → alert 提示
3. 若返回 ok → 弹 confirm 显示 raw vs applied 对比表
4. 用户确认 → 遍历 applied 字段，更新止盈止损卡片对应 input 的 value
5. 调用 `saveRiskSettings()` 自动持久化
6. `addLog('ok', ...)` 输出成功日志

**前置保护**：如果止盈止损卡片有未保存修改 → 默认不阻止（用户点应用按钮后自动保存，不做额外确认）。

### 5.4 空状态

若 `optimizer.search_space` 为空对象或字段数 < 2：
- 渲染灰色文字 "暂无搜索空间配置，请先运行 AI 优化或手动配置"
- "保存此卡"按钮正常可用（允许首次创建）
- "应用 AI 最优参数"按钮置灰 + tooltip "需先运行 AI 优化"

## 6. 交互流程

```
进入"系统参数配置"tab
  → loadSettings()
  → renderSearchSpace(optimizer.search_space)

用户修改搜索空间
  → 点"保存此卡"
  → saveSearchSpace()
  → 绿字 "✓ 已保存"

用户点"应用 AI 最优参数到止盈止损卡片"
  → applyAiBestToRisk()
  → POST /api/backtest/ai/apply-best
  → 弹 confirm 确认
  → 自动更新止盈止损卡片 input
  → addLog("ok", "AI 最优参数已应用")
```

## 7. 错误处理

| 场景 | 处理 |
|---|---|
| 搜索空间为空 | renderSearchSpace 显示空状态提示 |
| 输入 min ≥ max 或 step ≤ 0 | 前端校验，alert + 阻止提交 |
| 保存 API 网络错误 | 红字 "✗ 网络错误"，保留输入 |
| AI 无历史优化结果 | 后端返回 error，前端 alert |
| AI 结果缺少部分字段 | 缺失字段跳过不写入 |
| 并发保存冲突 | 最后保存覆盖（与现有行为一致） |

## 8. 向后兼容

- ❌ 不修改 `POST /api/settings`
- ❌ 不修改 `renderRiskTiers` / `saveRiskSettings`
- ❌ 不修改 `ai_optimizer.run()` 的 search_space 读取路径
- ✅ `renderSearchSpace` 替换空 stub，无破坏风险
- ✅ `app_setting.json` 数据不变

## 9. 改动文件清单

| 文件 | 改动 | 行数估计 |
|---|---|---|
| `static/js/main.js` | 实现 `renderSearchSpace()` + `saveSearchSpace()` + `applyAiBestToRisk()` | ~80 行 |
| `app/api/system.py` | 新增 `POST /api/settings/list/optimizer_search_space` | ~15 行 |
| `app/api/backtest.py` | 新增 `POST /api/backtest/ai/apply-best` | ~60 行 |
| `static/index.html` | 无改动（HTML 已存在） | 0 |
| `config/app_setting.json` | 无改动（数据已存在） | 0 |
| `core/settings.py` | 无改动（property 已存在） | 0 |

## 10. 未纳入范围

- 参数动态增删（用户选了固定列表）
- 搜索空间参数与风险参数的自动双向同步（只做单向：AI → 风险）
- 多组搜索空间（只支持 1 组全局搜索空间）
