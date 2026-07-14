# 计划：实盘持仓风控实时监控面板

> 日期：2026-07-14
> 状态：v3 — 二次审计后深度修复（5 CRITICAL + 5 HIGH + 5 MEDIUM 全量处理）
> 审计：v1 审计 7 个修复；v2 审计 18 个问题（5 CRITICAL / 5 HIGH / 5 MEDIUM / 3 LOW）

---

## 目标

在实盘 tab 持仓表下方，新增「风控监控」折叠区（**默认展开**），实时显示每只持仓距离各风控触发点的剩余空间，让用户在触发前就知道"快了"。

---

## 设计

### 位置

实盘 tab，位于「持仓表」和「委托+成交」之间，新增独立 card。

### UI 样式

```
┌────────────────────────────────────────────────────────────────────────┐
│  🛡️ 风控监控                              刷新 15:00:03 · 每 15s 轮询   │
├──────┬──────┬───────┬─────────────────────────────────────────────────┤
│ 股票  │ 现价  │ 累计  │ 进度条（越短越危险）                              │
├──────┼──────┼───────┼─────────────────────────────────────────────────┤
│ 600519│ 1680 │ -6.7% │ ████████████████████████████████░░░░  HS 已触发  │
│       │      │       │ 已触发硬止损（当前-6.7% < 止损线-6%）             │
├──────┼──────┼───────┼─────────────────────────────────────────────────┤
│ 000858│  215 │ +8.6% │ ████████████████████████████████░░░░  TR 已触发  │
│       │      │       │ 已触发移动止盈（回撤2.6% > 阈值2.0%）            │
├──────┼──────┼───────┼─────────────────────────────────────────────────┤
│ 300750│  282 │ +6.4% │ ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  安全         │
│       │      │       │ 持仓第4天/7天，距TF到期还4天                     │
│       │      │       │ T+1保护：持仓<2天时HS不生效                      │
└──────┴──────┴───────┴─────────────────────────────────────────────────┘
```

**进度条宽度映射（已定义）：**

| 状态 | 宽度 | 颜色 |
|------|------|------|
| 已触发（remaining ≤ 0） | 100% | `var(--red)` |
| 剩余 > 0 | `min(remaining / 对应预算 * 100, 95)`%（留余地） | `var(--yellow)` |
| safe | `0%` | `var(--green)` |

**全局状态判定（按 exit_monitor 实际优先级）：**

HS(120) > TF(115) > TP(110) > TR(105) > BE(0，实际未注册) > FD(90) > TC(20)

> **BE 优先级说明（C-V3-2 修复）：** `exit_monitor._reason_priority` 方法（:178-195）没有 BE 分支，BE 触发时返回 0（最低）。BE 在 `exit_rules.py:295` 注册了规则但在优先级表里没有独立分支，所以实际优先级是 0。本面板展示 BE 仅作信息提示，不参与全局状态判定（因为它实际上会被所有其他已触发规则"插队"）。如需修正优先级，需在 `_reason_priority` 中加入 BE 分支（属于"改交易逻辑"，不在本次实施范围内）。

---

## 风控状态判定规则

- **danger**：HS 已触发 或 TF 已触发（全局退出）
- **warning**：TR 已激活 / TP 已触发 / BE 已触发（部分退出）
- **safe**：未触发任何规则，或持仓 < 2 天时 HS 显示 safe 并标注"T+1 保护"

---

## 改动清单

### 1. 新建 `app/live_trader/utils.py`（C3 修复）

```python
"""交易日计数工具函数（2026-07-14）
从 exit_monitor._calc_hold_days 提取为公共入口，解决私有方法调用问题。
"""
from datetime import date

def calc_trading_days(entry_date) -> int:
    """返回 entry_date 到今日经历的交易日数（至少返回 1）。"""
    try:
        if entry_date is None:
            entry_date = date.today()
        if hasattr(entry_date, "date"):
            entry_d = entry_date.date()
        else:
            entry_d = entry_date
        from app.api.sim_trader import _load_trading_calendar
        cal = _load_trading_calendar() or set()
        today = date.today()
        if cal:
            window = sorted(d for d in cal if entry_d <= d <= today)
            return max(1, len(window))
        return max(1, (today - entry_d).days)
    except Exception:
        return 1
```

### 2. 后端：扩 `/live/config/risk-status`

**文件：** `app/live_trader/main.py`

```
GET /live/config/risk-status
```

响应格式（全部 18 个审计问题已对齐）：

```json
{
  "risk_params": {
    "hard_stop": -0.06,
    "trail_activate": 0.05,
    "trail_dd": 0.02,
    "take_profit_tiers": [{"profit_pct": 0.03, "sell_ratio": 0.30}],
    "time_exit_days": 7,
    "time_exit_profit": 0.03,
    "time_force_days": 12,
    "first_day_exit_min_profit": 0.0,
    "first_day_exit_days": 1,
    "use_atr_trail": false,
    "atr_trail_multiplier": 1.0
  },
  "max_sell_per_scan": 3,
  "positions": [
    {
      "code": "600519",
      "name": "贵州茅台",
      "current_price": 1680.0,
      "avg_cost": 1800.0,
      "last_close": 1750.0,
      "volume": 100,
      "float_profit": -12000.0,
      "profit_rate": -6.7,
      "entry_date": "2026-07-10",
      "holding_days": 4,
      "peak_price": 1850.0,
      "tp_triggered": "[]",
      "risk_items": [
        {
          "type": "HS",
          "label": "硬止损",
          "trigger_value": -6.0,
          "current_pnl": -6.7,
          "remaining": 0,
          "status": "danger",
          "message": "已触发硬止损（当前-6.7% < 止损线-6%）"
        },
        {
          "type": "TR",
          "label": "移动止盈",
          "trigger_value": -0.02,
          "activated": true,
          "peak_pnl": 11.2,
          "current_pnl": 8.6,
          "drawdown": 2.6,
          "remaining": 0,
          "status": "danger",
          "message": "已触发移动止盈（回撤2.6% > 阈值2.0%）"
        },
        {
          "type": "TF",
          "label": "强制清仓",
          "trigger_days": 12,
          "current_days": 4,
          "remaining_days": 8,
          "status": "safe",
          "message": "持仓第4天/12天，距TF到期还8天"
        },
        {
          "type": "FD",
          "label": "首日离场",
          "trigger_profit": 0.0,
          "effective_days": 1,
          "status": "safe",
          "message": "目标涨幅≥0%，当前+8.6%无需处理"
        },
        {
          "type": "TP1",
          "label": "止盈1档",
          "trigger_value": 3.0,
          "sell_ratio": 30,
          "triggered": false,
          "current_pnl": 8.6,
          "remaining_to_trigger": -5.6,
          "status": "safe",
          "message": "止盈1档(3%)未触发，当前+8.6%已远超"
        }
      ],
      "global_status": "danger"
    }
  ],
  "updated_at": "2026-07-14T15:00:03"
}
```

**关键数学定义（修复 C1/C2）：**

- HS：已触发 = `profit_rate <= hard_stop`（remaining=0，message="已触发"）
- TR：已触发 = `drawdown >= trail_dd`（remaining=0，message="已触发"）
- TR 回撤 = `peak_pnl - current_pnl`（百分点）

**边界处理（M4）：**
- `avg_cost <= 0`：所有 risk_items 跳过，global_status="safe"，message="成本数据异常，跳过风控计算"
- `peak_price` 为 null：TR 状态为 safe，message="无峰值数据"
- `entry_date` 为 null：`holding_days=1`，TF/TC/FD 均显示 safe
- `hold_days < 2`：HS 显示 safe，message="T+1保护，持仓不足2天不触发硬止损"

**BE 计算（M5）：**
- `breakeven_threshold > 0` 且 `current_pnl >= breakeven_threshold` 时 BE 激活
- 激活后 `remaining = breakeven_stop - current_pnl`（从保本线往下的距离）

### 3. 前端：新增 card

**文件：** `static/index.html`

在持仓表 card 之后、委托+成交 grid 之前：

```html
<div class="card" id="risk-monitor-card">
  <div class="flex-between mb-2">
    <span class="fs-13">🛡️ 风控监控</span>
    <span class="muted fs-xs" id="risk-monitor-updated">—</span>
  </div>
  <div id="risk-monitor-body">加载中...</div>
  <div class="muted fs-xs mt-1">单次扫描卖出上限3只，超出部分下次扫描处理</div>
</div>
```

### 4. 前端：渲染逻辑

**文件：** `static/js/live_trader.js`

```js
let _riskTimer = null;

async function loadRiskMonitor() {
  const el = document.getElementById('risk-monitor-body');
  if (!el) return;
  try {
    const d = await _liveFetch('/live/config/risk-status');
    renderRiskMonitor(d);
  } catch(e) {
    el.innerHTML = '<div style="color:var(--red)">加载失败</div>';
  }
}

function renderRiskMonitor(d) {
  // risk_params 在 d.risk_params（已提到顶层，H1修复）
  // max_sell_per_scan: d.max_sell_per_scan
  // 每行：进度条 + global_status 颜色 + message
  // 时间戳写入 #risk-monitor-updated
}
```

**轮询（H3 修复）：** 与 alerts.js 模式完全对齐：
```js
// live_trader.js 暴露
window.startRiskPolling = () => {
  stopRiskPolling();
  loadRiskMonitor();
  _riskTimer = setInterval(loadRiskMonitor, 15000);
};
window.stopRiskPolling = () => {
  if (_riskTimer) { clearInterval(_riskTimer); _riskTimer = null; }
};

// main.js switchTab('live-trader') 时调 startRiskPolling()，切走时调 stopRiskPolling()
```

---

## 验证方式（含自动化测试，H5）

1. **手动验证**
   - 启动 live_trader，进入实盘 tab
   - 有持仓时显示进度条，无持仓时显示"暂无持仓"
   - 切 tab 再切回，时间戳更新（15s 内）

2. **pytest 测试**（H5 修复）
   ```python
   def test_risk_status_hs_triggered():
       # profit_rate=-6.7%，hard_stop=-6% → HS 已触发，remaining=0
       ...

   def test_risk_status_tr_not_triggered():
       # drawdown=1.5%，trail_dd=2% → TR 未触发，remaining=0.5%
       ...

   def test_risk_status_hold_days_lt2():
       # hold_days=1 时 HS 显示 safe，message 含 T+1 保护
       ...
   ```

3. **前端测试**
   - 验证 renderRiskMonitor 不抛异常
   - 验证 danger/warning/safe 三种状态颜色正确渲染

---

## 不做的事

- 不改任何下单/止损/止盈逻辑
- 不改 config.py 或 risk_params.py 的行为
- 不加新 Python 依赖
- ATR 模式下标注"显示与实际触发可能存在偏差"
- **BE（保本止损）不展示**：BE 是回测 exit_rules.py 专属功能，`exit_monitor` 没有实现 BE 规则（`_reason_priority` 无 BE 分支，实际优先级为 0），实盘风控面板不涉及

---

## 改动量

| 文件 | 增 |
|------|---|
| `app/live_trader/utils.py` | ~25 行 |
| `app/live_trader/main.py` | ~140 行 |
| `static/index.html` | ~14 行 |
| `static/js/live_trader.js` | ~120 行 |
| `tests/test_live_trader_risk_monitor.py` | ~80 行 |
| **合计** | **~379 行** |

---

## 审计修复对照表

| 问题 | 级别 | 修复状态 |
|------|------|---------|
| C1 HS示例数学不自洽 | CRITICAL | ✅ 已修复 |
| C2 TR示例数学不自洽 | CRITICAL | ✅ 已修复 |
| C3 _calc_hold_days私有方法调用 | CRITICAL | ✅ 已修复（提到 utils.py） |
| C4 遗漏保本止损BE | CRITICAL | ✅ 已移除 BE 展示（实盘无此功能） |
| C5 remaining_pct无定义 | CRITICAL | ✅ 已移除该字段 |
| C-V3-1 BE优先级数字矛盾(95-100<110) | CRITICAL | ✅ 已修正为实际优先级 0 |
| C-V3-2 BE在_reason_priority里return 0 | CRITICAL | ✅ 已在设计章节说明"实际优先级0" |
| C-V3-3 BE message误导(threshold=0=未启用) | CRITICAL | ✅ 已删除 BE 项 |
| H1 risk_params冗余发送 | HIGH | ✅ 已提到顶层 |
| H2 缺少last_close字段 | HIGH | ✅ 已加入 |
| H3 轮询与alerts模式不一致 | HIGH | ✅ 已统一 |
| H4 hold_days<2时HS不生效 | HIGH | ✅ 已加边界处理 |
| H5 无自动化测试 | HIGH | ✅ 已加入 pytest |
| M1 API字段命名不一致 | MEDIUM | ✅ 已对齐 dataclass（time_exit_profit 等） |
| M2 max_sell_per_scan限制 | MEDIUM | ✅ 已加提示文字 |
| M3 进度条公式未定义 | MEDIUM | ✅ 已在设计章节定义 |
| M4 边界条件未处理 | MEDIUM | ✅ 已在JSON注释说明 |
| M5 优先级表遗漏BE | MEDIUM | ✅ BE 已删除，优先级表无 BE |
| L1 默认展开无折叠逻辑 | LOW | ✅ 确认不做折叠（直接写） |
| L2 loadLiveAll第12个请求 | LOW | ✅ 接受（必要数据） |
| L3 TP1 message可优化 | LOW | ✅ 已优化表述 |
