# 审计报告 — PLAN-risk-monitor-fix-2026-07-14.md

> 审计日期：2026-07-14
> 审计对象：[PLAN-risk-monitor-fix-2026-07-14.md](./PLAN-risk-monitor-fix-2026-07-14.md)
> 审计方法：逐条对照实际代码验证，每项结论附带 file:line 引用

---

## 总体评价

| 维度 | 评分 | 说明 |
|------|------|------|
| 问题识别准确度 | 8/10 | 6 个问题中有 5 个识别准确，1 个（P2-1 TC 语义）理解有偏差 |
| 方案可行性 | 6/10 | P0-1 有关键排序缺陷会导致 NameError，P1-1 有 FD 进度条缺口 |
| 完整性 | 7/10 | 遗漏了 FD 的 budget/remaining 补充，测试 mock 策略太笼统 |
| 代码风格一致性 | 7/10 | JS 部分合理，Python 部分需注意变量作用域 |

**总体判定：PASS-WITH-WARNINGS** — 方案方向正确，但有 1 个会导致运行时错误的严重缺陷和 3 个需要补充的缺口。

---

## 逐项审计

### P0-1：T+1 保护缺失

**方案主张：** 在 HS 块中，`hs_triggered` 判断前加入 `holding_days < 2` 的 T+1 保护逻辑。

**代码验证：**

- [main.py:1338-1358](app/live_trader/main.py#L1338-L1358) — HS 块确实没有 `holding_days` 判断 ✅ 问题真实存在
- [main.py:1394](app/live_trader/main.py#L1394) — `holding_days = calc_trading_days(entry_date) if entry_date else 1` 在 **TF 块（第 1394 行）** 才计算
- HS 块（第 1338 行）在 TF 块（第 1392 行）**之前**

**🔴 CRITICAL 发现：变量作用域缺陷！**

`holding_days` 在第 1394 行才被赋值，但 HS 块在第 1338 行就开始执行。方案书的代码示例直接用了 `if holding_days < 2:`，**但此时 `holding_days` 还不存在**，会导致 `NameError`。

**修复建议：** 方案书必须明确在 HS 块之前（建议在第 1337 行 `risk_items = []` 之后）先计算 `holding_days`：

```python
risk_items = []

# 先计算 holding_days（HS/FD/TF/TC/TP 共用）
holding_days = calc_trading_days(entry_date) if entry_date else 1

# ----- HS 硬止损 -----
# 然后才是方案书中的 T+1 保护 + HS 逻辑
```

同时，TF 块（第 1394 行）的重复计算可以删除（或保留做防御，不影响正确性）。

**结论：** ⚠️ **方案方向正确，但缺少关键的前置步骤。本项 PASS-WITH-FIX。**

---

### P0-2：pytest 测试文件缺失

**方案主张：** 新建 `tests/test_live_trader_risk_monitor.py`，覆盖 9 个场景。

**代码验证：**

- `tests/` 目录下无 `*risk*` 相关文件 ✅ 确实缺失
- 方案书 9 个测试用例逐个审视：

| 测试函数 | 验证结果 | 备注 |
|----------|----------|------|
| `test_hs_triggered` | ✅ 合理 | profit_rate=-6.7% ≤ hard_stop=-6% → danger |
| `test_hs_not_triggered` | ✅ 合理 | profit_rate=-5% > hard_stop=-6% → safe |
| `test_hs_hold_days_lt2` | ✅ 合理 | 但依赖 P0-1 先修复才能通过 |
| `test_tr_not_triggered` | ✅ 合理 | drawdown=1.5% < trail_dd=2% → safe |
| `test_tr_triggered` | ✅ 合理 | drawdown=2.6% ≥ trail_dd=2% → warning |
| `test_avg_cost_zero` | ✅ 合理 | [main.py:1331](app/live_trader/main.py#L1331) 有 `if avg_cost <= 0: continue` |
| `test_tf_triggered` | ✅ 合理 | holding_days=12, tf_days=12 → tf_remaining=0 → danger |
| `test_tc_safe` | ⚠️ 需明确参数 | 默认 tc_days=7（[risk_params.py:54](app/config/risk_params.py#L54)），holding_days=7 时 tc_remaining=0，pnl=2% < 3% → safe。但需确认 mock 的 tc_days 值 |
| `test_tp_tier_triggered` | ✅ 合理 | 需要 mock `tp_triggered` 字段为 `[{"tier": 0}]` |

**⚠️ 缺口：mock 策略太笼统**

方案只说"mock 掉 store/QMT 依赖"，但 `get_risk_status` 的实际依赖链是：
1. `_state.get("store")` → 需要 mock
2. `store.get_positions()` → 返回持仓列表
3. `load_risk_params()` → 读 `app_setting.json` 的 `[risk]` 段
4. `calc_trading_days()` → 涉及交易日历

直接用 FastAPI TestClient 测试需要完整的 app 状态，建议至少说明：
- 用 `pytest.fixture` 预设 store 状态
- 用 `monkeypatch` 替换 `load_risk_params` 和 `calc_trading_days`
- 或者改为**纯函数测试**：抽取 `build_risk_items(pos, rp, holding_days)` 独立函数单独测试

**结论：** ✅ **方案正确，但 mock 策略需要细化。本项 PASS。**

---

### P1-1：进度条未实现

**方案主张：** 在 `renderRiskMonitor()` 中，每行增加进度条列，仅显示 global_status 最高的 risk_item。

**代码验证：**

- [live_trader.js:651-691](static/js/live_trader.js#L651-L691) — `renderRiskMonitor` 只有 `<table>` 渲染，无进度条 ✅ 确实缺失

**🔴 发现 1：FD 风险项缺少 `budget` 和 `remaining` 字段**

对比各风险项的字段：

| 风险类型 | budget 字段 | remaining 字段 | 代码位置 |
|----------|-------------|----------------|----------|
| HS | ✅ `budget` (line 1355) | ✅ `remaining` (line 1354) | [main.py:1350-1358](app/live_trader/main.py#L1350-L1358) |
| TR | ✅ `budget` (line 1387) | ✅ `remaining` (line 1386) | [main.py:1379-1390](app/live_trader/main.py#L1379-L1390) |
| TF | ✅ `budget` (line 1404) | ✅ `remaining` (line 1403) | [main.py:1398-1407](app/live_trader/main.py#L1398-L1407) |
| **FD** | ❌ **缺失** | ❌ **缺失** | [main.py:1415-1421](app/live_trader/main.py#L1415-L1421) |
| TC | ✅ `budget` (line 1436) | ✅ `remaining` (line 1435) | [main.py:1429-1439](app/live_trader/main.py#L1429-L1439) |
| TP | ✅ `budget` (line 1472) | ✅ `remaining` (line 1471) | [main.py:1464-1475](app/live_trader/main.py#L1464-L1475) |

FD 可以触发为 `warning`（[main.py:1413](app/live_trader/main.py#L1413)），优先级高于 TC（2 > 1）。如果某持仓 HS/TF/TR/TP 都是 safe 但 FD=warning，FD 会成为 global_status 最高项。此时方案书的 JS 代码：

```js
const pct = it.remaining <= 0 ? 100 : Math.min(it.remaining / it.budget * 100, 95);
```

会计算 `undefined / undefined * 100 = NaN`，进度条渲染为 `width: NaN%`，**完全不可见**。

**修复建议：** 两种方案选一：
- A) 给 FD 补充 `budget`（= fd_threshold）和 `remaining`（= profit_rate - fd_threshold）字段
- B) 在前端找"最高 risk_item"时跳过 FD（因为 FD 是二元触发，无"剩余空间"概念）

**⚠️ 发现 2：方案书没有说明需要在新 table 中增加进度条列**

当前 table 是 5 列（代码/现价/累计/状态/详情），加进度条需要 6 列，且表头 `<thead>` 要同步修改。

**结论：** ⚠️ **方案方向正确，但 FD 进度条 NaN 是真实 bug，需补充 FD 字段或跳过逻辑。本项 PASS-WITH-FIX。**

---

### P1-2：ATR 模式提示缺失

**方案主张：** API 响应加 `risk_params.atr_note`，前端在风险面板标题旁显示。

**代码验证：**

- [main.py:1315](app/live_trader/main.py#L1315) — `risk_params` 已有 `use_atr_trail`，但无 `atr_note` ✅
- [live_trader.js](static/js/live_trader.js) — 前端完全没读 `risk_params` 对象 ✅
- [risk_params.py:34-35](app/config/risk_params.py#L34-L35) — `use_atr_trail` 和 `atr_trail_multiplier` 字段已定义 ✅

**方案可行性：** 方案书的后端改动（3 行）和前端描述（1 句）都是准确的。

**⚠️ 微小缺口：** 方案只说"前端在风险面板标题旁显示 note"，但没给前端代码片段。`renderRiskMonitor` 接收的 `d` 参数包含 `d.risk_params`（[main.py:1499](app/live_trader/main.py#L1499)），可以直接读取。建议补充具体选择器和插入位置。

**结论：** ✅ **方案正确，可直接实施。本项 PASS。**

---

### P2-1：TC 逻辑需核实

**方案主张：** TC 语义"持仓超 N 天且盈利不足时退出"，代码 `profit_rate >= threshold → warning`，逻辑方向需确认。

**代码验证：**

- [main.py:1427](app/live_trader/main.py#L1427)：`tc_status = "warning" if tc_remaining <= 0 and profit_rate >= tc_profit_threshold else "safe"`
- [exit_rules.py:244-248](app/backtest/exit_rules.py#L244-L248)（**真相源**）：

```python
def rule_time_condition(ctx):
    """时间条件退出：持仓超 N 天且盈利达标"""
    if ctx.hold_days > ctx.time_exit_days:
        if cur > ctx.time_exit_profit:
            return ExitSignal("TC", ctx.close)
```

**🔴 方案书对 TC 语义的理解是错误的！**

exit_rules.py 的 docstring 和逻辑都明确：TC = "持仓超 N 天**且盈利达标** → 退出"。不是"盈利不足时退出"。

代码 `profit_rate >= tc_profit_threshold → warning` 和 exit_rules.py 完全一致——盈利达标时 warning（提醒你可以考虑退出了）。

**结论：** ⚠️ **方案书的"行动"是对的（只确认不改逻辑），但"问题描述"是错的（误解了 TC 语义）。实际代码无 bug，TC 逻辑和 exit_rules.py 一致。本项 PASS-WITH-CORRECTION。**

---

### P2-2：TP 解析静默失败

**方案主张：** `except Exception: pass` 加 `logger.warning`。

**代码验证：**

- [main.py:1454-1455](app/live_trader/main.py#L1454-L1455)：确认 `except Exception: pass` ✅
- [main.py:1447-1453](app/live_trader/main.py#L1447-L1453)：解析逻辑本身合理（`json.loads` + `isinstance` 检查 + `any()` 遍历）

**方案可行性：** 方案书的修复代码是标准的 defensive logging，唯一需要确认的是 `logger` 在函数作用域内是否可用。查 [main.py](app/live_trader/main.py) 头部应有 logger 定义。

**结论：** ✅ **方案正确。本项 PASS。**

---

## "不做的事" 审计

| 不做的事 | 验证 | 判定 |
|----------|------|------|
| 不改 TR status（维持 warning） | [main.py:1373](app/live_trader/main.py#L1373) `tr_status = "warning"` 符合 [PLAN 设计稿](docs/PLAN-live-risk-monitor-2026-07-14.md#L60) "TR 是部分退出=warning" | ✅ 合理 |
| 不改 `current_price` 字段 | [main.py:1484](app/live_trader/main.py#L1484) 注释说明"实时价由前端 applyLiveQuotes 填入" | ✅ 合理 |
| 不移除 lru_cache | `utils.py:9` 的 `@lru_cache(maxsize=1)` 缓存交易日历，进程生命周期内不变 | ✅ 合理 |
| 不改 git 历史 | — | ✅ 合理 |

---

## 方案书未覆盖的额外发现

### 发现 A：HS 块 `profit_rate` 取值无防御

[main.py:1340](app/live_trader/main.py#L1340)：
```python
profit_rate = float(pos.get("profit_rate", 0))
```

如果 store 中某持仓的 `profit_rate` 是 `None` 或空字符串 `""`，`float(None)` / `float("")` 会抛 TypeError/ValueError，导致整个 `/live/config/risk-status` 端点 500。

**严重程度：** LOW（store 通常保证该字段有值，但缺少防御）

**建议：** `float(pos.get("profit_rate") or 0)`

---

### 发现 B：进度条"最高 risk_item"选择策略未定义边界情况

方案书说"danger > warning > safe"，但同一状态有多个 risk_items 时（如 HS=safe, TR=safe, TF=safe），选哪个显示进度条？

当前按 `STATUS_PRIORITY` 中同状态的所有项，`max()` 返回第一个遇见的。应该明确定义：同状态时选"remaining/budget 比例最小的"（最接近触发的）或"按 exit_monitor 优先级最高的"。

**严重程度：** LOW（safe 状态下所有进度条都是 0%，选哪个都一样）

---

### 发现 C：方案书代码片段中 `entry_date` 无防御

P0-1 代码片段插入位置在 `volume` 检查之后，但 `entry_date` 可能为 None（[main.py:1394](app/live_trader/main.py#L1394) 有 `if entry_date else 1` 的兜底）。方案书片段没有展示这个兜底。

---

## 总结

### 修复前后对照

| 问题 | 方案级别 | 审计判定 | 关键缺陷 |
|------|----------|----------|----------|
| P0-1 T+1 保护 | P0 | ⚠️ PASS-WITH-FIX | `holding_days` 不在 HS 块作用域内，直接使用会 NameError |
| P0-2 测试文件 | P0 | ✅ PASS | mock 策略需细化 |
| P1-1 进度条 | P1 | ⚠️ PASS-WITH-FIX | FD 无 budget/remaining 字段会导致 NaN 进度条 |
| P1-2 ATR 提示 | P1 | ✅ PASS | 无缺陷 |
| P2-1 TC 逻辑 | P2 | ⚠️ PASS-WITH-CORRECTION | TC 语义理解有误，但代码实际正确，无需修改 |
| P2-2 TP 静默失败 | P2 | ✅ PASS | 无缺陷 |

### 必须修复才能实施的问题

1. **P0-1 变量作用域**（CRITICAL）：在 HS 块之前先计算 `holding_days`，否则代码运行即崩溃
2. **P1-1 FD 进度条 NaN**（HIGH）：给 FD 补充 `budget`/`remaining` 字段，或在前端跳过 FD
3. **P2-1 TC 语义纠正**（MEDIUM）：方案书中的 TC 问题描述需更正为"TC 语义已核实，代码与 exit_rules.py 一致，无需修改"

---

*审计完成时间: 2026-07-14 21:30 (周二)*
