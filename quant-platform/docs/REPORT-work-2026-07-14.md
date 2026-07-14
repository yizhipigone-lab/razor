# 工作成果报告 — 2026-07-14

> 合并 PR #6 auto_buy 前端 UI + 实盘风控监控面板 v1 + batch-1 风控参数统一

---

## 一、PR #6 auto_buy 前端 UI（已合并）

**Commit:** `898d778` — 2026-07-14

| 模块 | 改动 |
|------|------|
| `app/live_trader/scheduler.py` | auto_buy_time 热配置 + 锁保护 + stop() 清理 `_auto_buy_task.cancel()` |
| `app/live_trader/main.py` | GET/PUT `/live/config/auto-buy-time` API；scan-interval PUT 加 `_is_local` 鉴权 |
| `static/index.html` | 自动选股开关 + 时间输入框 + 摘要行 `lt-sum-auto-buy` |
| `static/js/live_trader.js` | `loadAutoBuyTime()` / `saveAutoBuyTime()` / `toggleLiveSwitch('auto')` + 防重入 |
| `static/js/alerts.js` | auto_buy 状态卡片接入告警面板 |

### 审计修复（7项）

| 审计项 | 文件 | 修复内容 |
|--------|------|----------|
| H1 | main.py scan-interval PUT | 加 `_is_local` 本地鉴权 |
| M2 | scheduler.py | `_exit_scan_interval` / `_auto_buy_time` 读写加 `threading.Lock` |
| M3 | scheduler.py + main.py | HH:MM 格式校验（正则 `^[0-2]\d:[0-5]\d$`） |
| L2 | live_trader.js | `innerHTML` 改 `textContent` 防 XSS |
| L3 | alerts.js | escHtml 统一 5 字符（`'` 也转义） |
| L5 | live_trader.js toggleLiveSwitch | 防重入锁移到 try 之前 |
| O2 | scheduler.py stop() | 加 `self._auto_buy_task.cancel()` 防止僵尸任务 |

---

## 二、风控监控面板 v1（已合并）

**Commit:** `303d061` — 2026-07-14 20:50

### 新增文件

| 文件 | 作用 |
|------|------|
| `app/live_trader/utils.py` | `calc_trading_days()` 公共函数（从 exit_monitor 提取，进程级缓存） |
| `app/live_trader/main.py` (+222行) | `GET /live/config/risk-status` API |

### API 设计

**端点：** `GET /live/config/risk-status`

返回结构：
```json
{
  "risk_params": { "hard_stop": -6.0, "trail_activate": 3.0, ... },
  "max_sell_per_scan": 3,
  "positions": [
    {
      "code": "000001",
      "name": "平安银行",
      "profit_rate": -3.2,
      "holding_days": 5,
      "risk_items": [ ... ],
      "global_status": "safe"
    }
  ],
  "updated_at": "2026-07-14T20:50:00"
}
```

### 6 类风控维度

| 维度 | 标签 | 触发条件 | 进度条分母（budget） |
|------|------|----------|------|
| HS | 硬止损 | 亏损率 ≤ 止损线 | `\|hard_stop\|`（百分点） |
| TR | 移动止盈 | 回撤 ≥ 阈值 | `trail_dd`（百分点） |
| TF | 强制清仓 | 持仓天数 ≥ 到期天数 | `trigger_days`（天数） |
| FD | 首日离场 | 前N天盈利未达标 | `first_day_exit_min_profit`（百分点） |
| TC | 时间退出 | 持仓超N天且盈利不足 | `time_exit_days`（天数） |
| TP1/TP2/TP3 | 止盈档 | 触发第N档止盈 | 各档 `profit_pct`（百分点） |

**进度条公式：** `remaining / budget * 100`，已触发（remaining ≤ 0）则显示 100%  
**全局状态优先级：** `danger(3) > warning(2) > safe(1)`，取最高

### 前端 UI

- `index.html`：新增风控监控 card（默认展开），含"单次扫描卖出上限3只"提示
- `live_trader.js`：`renderRiskMonitor()` + `startRiskPolling/stopRiskPolling`，15秒轮询，对齐 alerts.js
- `main.js`：`switchTab` 切进实盘 tab 启动轮询，切走停止

### 审计修复（4轮 22 个问题）

| 轮次 | 问题数 | 典型问题 |
|------|--------|----------|
| v1 | 7个 | 字段名对齐 RiskParams dataclass |
| v2 | 18个 | C1 HS 算术错、C2 TR 算术错、BE 遗漏 |
| v3 | 6个 | BE priority 自矛盾、BE 不在 exit_monitor |
| v4 | 3个 | M-V4-1 公共函数、M-V4-2 budget 定义、L-V4-1 审计表标注 |

**关键决策：** BE（保本止损）为回测专属，不存在于 `exit_monitor.py`，从 live trading 面板中移除。

---

## 三、batch-1 风控参数统一（已合并）

**`app/api/backtest.py` 重构**

两个端点原双重写入（module 变量 + settings），改为**只写 settings**，切断 module 变量写入路径：

| 端点 | 改动前 | 改动后 |
|------|--------|--------|
| `POST /api/backtest/apply-to-system` | 写 `sc.HARD_STOP = ...` + settings | 只写 `settings.risk.hard_stop_loss_pct = ...` |
| `POST /api/backtest/save-risk-params` | 写 module 变量 + settings | 只写 `settings.risk.*` 各字段 |

**背景：** 2026-07 月净值失真根因——行情数据优先级混乱，batch-1 清理 module 变量写入路径，统一为单一真相源（settings）。

---

## 四、Git 推送修复

**问题：** `git push` 失败，错误 "Failed to connect to github.com port 443"

**根因：** 本地 `http.proxy` 配置干扰了 `gh auth git-cred`

**修复：** `git config --local http.proxy ""`，清掉代理后推送成功

---

## 📊 汇总

| 指标 | 数值 |
|------|------|
| 总 commit | 2 个（898d778 + 303d061） |
| 总文件改动 | 11 个 |
| 新增代码 | +657 行 |
| 删除代码 | -159 行 |
| 审计问题修复 | 22 个（4轮） |
| 新增 API 端点 | 3 个（risk-status、auto-buy-time GET/PUT） |
| 新增前端组件 | 风控监控面板 + auto_buy UI + 告警面板接入 |

---

## 五、待办事项

| 事项 | 状态 | 说明 |
|------|------|------|
| 验证并开启 auto_buy | ⏳ 待确认 | 需先确认 `QMT_ACCOUNT_ID` 环境变量，重启 live_trader |
| 前端 Phase 2（main.js 清理） | ⏳ 待排 | 需要 `safe_patterns.py` / `verify_classes.py` 工具 |

---

*报告生成时间: 2026-07-14 20:51 (周二)*
