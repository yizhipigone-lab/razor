# 计划：风控监控面板核查修复（v2 — 审计修复版）

> 基于 `docs/AUDIT-VERIFY-work-2026-07-14.md` + `docs/AUDIT-PLAN-risk-monitor-fix-2026-07-14.md` 迭代
> 修复 6 个真实问题，消化审计发现的 3 个方案缺陷

---

## 改动清单

### P0-1：T+1 保护缺失 + 变量作用域修复

**位置：** `app/live_trader/main.py` risk_status API 内

**关键修复（审计发现）：** `holding_days` 在原代码第 1394 行（TF 块内）才计算，但 HS 块在第 1338 行就开始执行。**必须在 HS 块之前先计算 `holding_days`**，否则 NameError。

**改动步骤：**

**步骤 1：** 在 `risk_items = []` 之后、各 risk_item 块之前，统一计算 `holding_days`：

```python
# main.py:1336 附近
risk_items = []

# 统一计算 holding_days（HS/FD/TF/TC/TP 共用）
holding_days = calc_trading_days(entry_date) if entry_date else 1

# ----- HS 硬止损 -----
hard_stop_pct = rp.hard_stop * 100  # 如 -6.0
profit_rate = float(pos.get("profit_rate", 0) or 0)  # 防御 None/"" → 0

# T+1 保护：持仓不足2天不触发硬止损
if holding_days < 2:
    hs_status = "safe"
    hs_message = "T+1保护，持仓不足2天不触发硬止损"
    risk_items.append({
        "type": "HS", "label": "硬止损",
        "trigger_value": hard_stop_pct,
        "current_pnl": profit_rate,
        "remaining": abs(hard_stop_pct),  # 进度条用满（safe=0%）
        "budget": abs(hard_stop_pct),
        "status": hs_status,
        "message": hs_message,
    })
else:
    hs_triggered = profit_rate <= hard_stop_pct
    if hs_triggered:
        hs_remaining = 0.0
        hs_status = "danger"
        hs_message = f"已触发硬止损（当前{profit_rate:.1f}% < 止损线{hard_stop_pct:.1f}%）"
    else:
        hs_remaining = abs(hard_stop_pct - profit_rate)
        hs_status = "safe"
        hs_message = f"距硬止损 {hard_stop_pct:.1f}% 还差 {hs_remaining:.1f}%"
    risk_items.append({
        "type": "HS", "label": "硬止损",
        "trigger_value": hard_stop_pct,
        "current_pnl": profit_rate,
        "remaining": hs_remaining,
        "budget": abs(hard_stop_pct),
        "status": hs_status,
        "message": hs_message,
    })
```

**步骤 2：** TF 块（原 1394 行）的 `holding_days` 重复计算可删除（已在步骤 1 统一计算）。

**防御改进：** `profit_rate = float(pos.get("profit_rate", 0) or 0)` 防止 `None`/空字符串导致 TypeError（审计发现 A）。

---

### P0-2：pytest 测试文件

**位置：** 新建 `tests/test_live_trader_risk_monitor.py`

**mock 策略（细化后）：**

采用**纯函数测试**路径，不走完整 FastAPI 依赖链：
- 直接 import `build_risk_items`（从 main.py 抽取为独立函数）或 **直接 mock `load_risk_params` + `calc_trading_days`**
- 用 `unittest.mock.patch` 替换这两个函数，返回固定值
- 用 `pytest.fixture` 定义标准持仓字典 fixture

```python
import pytest
from unittest.mock import patch, MagicMock
from datetime import date

# 标准持仓 fixture
@pytest.fixture
def sample_pos():
    return {
        "code": "000001",
        "avg_cost": 10.0,
        "volume": 1000.0,
        "last_close": 9.5,
        "profit_rate": -6.7,
        "entry_date": date.today(),
        "peak_price": 11.0,
        "tp_triggered": "[]",
    }

# mock load_risk_params（返回固定 RiskParams）
def mock_rp():
    rp = MagicMock()
    rp.hard_stop = -0.06
    rp.trail_activate = 0.03
    rp.trail_dd = 0.02
    rp.take_profit_tiers = [{"profit_pct": 0.03, "sell_ratio": 0.3}]
    rp.time_exit_days = 7
    rp.time_exit_profit = 0.03
    rp.time_force_days = 12
    rp.first_day_exit_min_profit = 0.0
    rp.first_day_exit_days = 1
    rp.use_atr_trail = False
    rp.atr_trail_multiplier = 1.0
    return rp

@patch("app.live_trader.main.load_risk_params", return_value=mock_rp())
@patch("app.live_trader.main.calc_trading_days", return_value=4)
def test_hs_triggered(mock_calc, mock_rp, sample_pos):
    # profit_rate=-6.7%, hard_stop=-6% → HS danger, remaining=0
    items = build_risk_items(sample_pos, mock_rp(), 4)
    hs = next(i for i in items if i["type"] == "HS")
    assert hs["status"] == "danger"
    assert hs["remaining"] == 0.0
```

**测试场景（9个）：**

| 测试函数 | 场景 | 断言 |
|----------|------|------|
| `test_hs_triggered` | profit_rate=-6.7%, hard_stop=-6% | HS status="danger", remaining=0 |
| `test_hs_not_triggered` | profit_rate=-5%, hard_stop=-6% | HS status="safe", remaining=1 |
| `test_hs_hold_days_lt2` | hold_days=1, profit_rate=-7% | HS status="safe", message 含 "T+1" |
| `test_tr_not_triggered` | drawdown=1.5%, trail_dd=2% | TR status="safe", remaining=0.5% |
| `test_tr_triggered` | drawdown=2.6%, trail_dd=2% | TR status="warning", remaining=0 |
| `test_avg_cost_zero` | avg_cost=0 | 该持仓被跳过（结果为空） |
| `test_tf_triggered` | holding_days=12, tf_days=12 | TF status="danger", remaining=0 |
| `test_tc_warning` | holding_days=7, pnl=4%, threshold=3% | TC status="warning" |
| `test_tp_tier_triggered` | tp_triggered=[{"tier": 0}] | TP1 status="warning", remaining=0 |

> **注意：** `build_risk_items` 函数需从 main.py 抽取为独立纯函数（接受 pos/rp/holding_days，返回 risk_items 列表），这样才能在不启动 FastAPI 的情况下测试。否则改用 `@patch` 整个 `/live/config/risk-status` 端点的内部逻辑路径。

---

### P1-1：进度条实现 + FD NaN 修复

**位置：** `static/js/live_trader.js` `renderRiskMonitor()`（:651-691）

**FD NaN 问题（审计发现）：** FD（首日离场）是二元触发（已触发/未触发），无"剩余空间"概念，进度条分子分母无意义。选 global_status 最高的 risk_item 显示进度条时，**跳过 FD**。

**改动：** 在 table 每行中插入进度条列，JS 逻辑：

```js
// 进度条列：选 global_status 最高的那个 risk_item（跳过 FD）
const topItem = (pos.risk_items || [])
  .filter(it => it.type !== 'FD')  // FD 无进度条概念，跳过
  .sort((a, b) => {
    const p = { danger: 3, warning: 2, safe: 1 };
    return (p[b.status] || 0) - (p[a.status] || 0);
  })[0];

let barHtml = '';
if (topItem) {
  const pct = topItem.remaining <= 0 ? 100
    : Math.min(topItem.remaining / topItem.budget * 100, 95);
  const barColor = topItem.status === 'danger' ? 'var(--red)'
    : topItem.status === 'warning' ? 'var(--yellow)'
    : topItem.remaining > 0 ? 'var(--yellow)'
    : 'var(--green)';
  barHtml = '<div style="background:var(--bg2);border-radius:3px;height:6px;width:100%">' +
    '<div style="width:' + pct + '%;background:' + barColor + ';height:6px;border-radius:3px"></div>' +
    '</div>';
}
```

同时更新 table 表头增加进度条列：

```js
// live_trader.js:665 附近
let html = '<table class="data-table" style="font-size:12px"><thead><tr>' +
  '<th>代码</th><th>现价</th><th>累计</th><th>进度</th><th>状态</th><th>详情</th></tr></thead><tbody>';
```

---

### P1-2：ATR 模式提示

**位置：** `app/live_trader/main.py` risk_status API + `static/js/live_trader.js` 渲染

**后端改动：** 在 `risk_params` 构建处加 `atr_note`：

```python
# main.py:1317 附近，risk_params 字典构建末尾
if rp.use_atr_trail:
    risk_params["atr_note"] = "移动止盈基于ATR计算，显示与实际触发可能存在偏差"
```

**前端改动：** 在 `renderRiskMonitor()` 顶部加 note 提示：

```js
function renderRiskMonitor(d) {
  const el = document.getElementById('risk-monitor-body');
  if (!el) return;
  // ATR note
  const atrNote = d.risk_params && d.risk_params.atr_note;
  const noteHtml = atrNote
    ? '<div class="muted fs-xs mb-2" style="color:var(--yellow)">' + escHtml(atrNote) + '</div>'
    : '';
```

---

### P2-1：TC 逻辑核实（结论：代码正确，无需修改）

**核实结论（审计发现）：** `exit_rules.py:244-248` 中 TC 真实语义：

```python
def rule_time_condition(ctx):
    """时间条件退出：持仓超 N 天且盈利达标"""
    if ctx.hold_days > ctx.time_exit_days:
        if cur > ctx.time_exit_profit:
            return ExitSignal("TC", ctx.close)
```

TC = "持仓超 N 天**且盈利达标** → 退出"。**不是**"盈利不足时退出"。

`main.py:1427` 的代码 `profit_rate >= tc_profit_threshold → warning` 与 `exit_rules.py` 完全一致，代码无 bug。

**本次行动：只确认，不改代码。** 将结论记入本计划书，不修改任何逻辑。

---

### P2-2：TP 解析静默失败

**位置：** `app/live_trader/main.py` TP tiers 解析块（:1447-1455）

**改动：** `logger` 在 main.py:22 已定义，直接使用：

```python
except Exception as e:
    logger.warning(f"TP tiers 解析失败 code={code} tp_triggered={tp_triggered!r}: {e}")
```

---

## 不做的事

- 不改 TR status（维持 warning，符合 PLAN 设计稿）
- 不改 `current_price` 字段（后端 last_close + 前端 applyLiveQuotes 覆盖是设计决策）
- 不移除 lru_cache（进程级缓存影响极小）
- 不改 git 历史
- 不改 TC 逻辑（已核实与 exit_rules.py 一致）

---

## 审计修复对照

| 审计问题 | 修复方式 |
|----------|----------|
| P0-1 变量作用域（NameError） | 在 `risk_items = []` 后统一计算 `holding_days` |
| P0-2 mock 策略笼统 | 明确 `@patch load_risk_params` + `@patch calc_trading_days` 策略 |
| P1-1 FD NaN | 前端选进度条 item 时过滤掉 FD |
| P1-2 atr_note 前端无代码 | 补充 `noteHtml` + 插入位置 |
| P2-1 TC 语义误解 | 纠正：TC 代码正确，只确认不做改动 |
| 发现 A profit_rate 防御 | `float(pos.get("profit_rate", 0) or 0)` |

---

## 验证方法

1. `python -c "from app.live_trader.main import app"` 无 import 错误
2. pytest `tests/test_live_trader_risk_monitor.py` 全部通过
3. 启动 live_trader，进实盘 tab，有持仓时进度条出现
4. `holding_days=1` 的持仓 HS 显示 T+1 保护
5. `use_atr_trail=true` 时面板有 ATR note
6. TP tiers 解析失败时 server log 有 warning
7. FD 持仓时进度条不显示 NaN（跳过 FD）
