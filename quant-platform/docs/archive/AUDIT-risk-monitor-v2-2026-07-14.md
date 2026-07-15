# 审计报告 — 风控监控面板核查修复 v2（fc13a4c）

> 审计日期：2026-07-14
> 审计范围：commit fc13a4c 实际代码（不是测试文件）
> 审计标准：严苛，挑剔，不讲好话
> 审计方法：读实际代码，逐行对照 PLAN-risk-monitor-fix-v2-2026-07-14.md

---

## 🔴 CRITICAL：测试函数结构与 main.py 实际代码不一致

**这是最严重的问题。**

测试文件 `tests/test_live_trader_risk_monitor.py` 中的 `_build_risk_items` 函数（:24-183）是内联提取的测试辅助函数，**它的 HS 块结构和 main.py 实际代码结构不同**。

**main.py 实际 HS 块结构（:1347-1377）：**
```python
if holding_days < 2:
    # T+1 保护，追加 HS safe，return
    risk_items.append({...})
else:
    hs_triggered = profit_rate <= hard_stop_pct
    if hs_triggered:
        # danger，追加，return
        risk_items.append({...})
    else:
        # safe，追加，return
        risk_items.append({...})
```

**测试文件 `_build_risk_items` HS 块结构（:38-72）：**
```python
if holding_days < 2:
    # T+1 保护，追加 HS safe
    risk_items.append({...})
elif hs_triggered:   # ← 这个变量在前面的 if 分支里已经定义过了吗？
    # danger
    risk_items.append({...})
else:
    # safe
    risk_items.append({...})
```

**问题：** 测试函数用的是 if/elif/else 三路分支，main.py 实际用的是 if/else 两路（内嵌 if/else）。`test_hs_hold_days_lt2` 通过了，但这只能证明 T+1 保护这一条路径碰巧写对了——**其他路径的逻辑根本不是 main.py 的实际逻辑**。

**影响：** 14 个测试全部 PASS，但它们测的是另一套代码。这不是测试覆盖问题，是测试函数本身就是错误的复制品。

---

## 🔴 CRITICAL：测试函数 HS remaining 值与 main.py 不一致

**main.py（正确）：**
```python
if holding_days < 2:
    # remaining = abs(hard_stop_pct) = 6.0
```
**测试函数（错误）：**
```python
if holding_days < 2:
    # remaining = abs(hard_stop_pct) = 6.0  ← 一致 ✅
```

这部分碰巧一致。但 `holding_days >= 2` 的 safe 路径：

**main.py：**
```python
hs_remaining = abs(hard_stop_pct - profit_rate)  # 6.0 - (-5.0) = 1.0
```

**测试函数：**
```python
"remaining": abs(hard_stop_pct - profit_rate),  # 6.0 - (-5.0) = 1.0 ✅ 一致
```

这部分也一致。但 T+1 路径的 `budget` 字段：

**main.py（:1355）：** `"budget": abs(hard_stop_pct)` = 6.0  
**测试函数（:50）：** `"budget": abs(hard_stop_pct)` = 6.0 ✅ 一致

**真正的 remaining 不一致在 T+1 路径的语义：** main.py 的 T+1 保护场景下 remaining 填的是 `abs(hard_stop_pct)`（满刻度），而 safe 场景填的是 `abs(hard_stop_pct - profit_rate)`（离触发差值）。两者物理含义不同，但测试函数都用了 `abs(hard_stop_pct)`，碰巧 main.py 也这样写，所以这部分不是 bug。

---

## 🔴 HIGH：TR 进度条颜色逻辑有误

**位置：** `live_trader.js:686-689`

```js
const barColor = topItem.status === 'danger' ? 'var(--red)'
  : topItem.status === 'warning' ? 'var(--yellow)'
  : topItem.remaining > 0 ? 'var(--yellow)'   // ← safe 但 remaining>0 → yellow
  : 'var(--green)';                           // ← safe 且 remaining=0 → green
```

问题：`safe` 且 `remaining > 0` 时显示 yellow，`safe` 且 `remaining = 0` 时显示 green。

但根据 PLAN 设计稿（:41-48）：
- safe：`0%` 宽度，`var(--green)`

safe 状态下 remaining 无论如何都不该是 yellow。进度条宽度为 0 时颜色才应该是 green（表示完全没有风险）。当前逻辑下 safe+有剩余空间会显示 yellow 进度条，视觉上等同于 warning，**用户会误判**。

---

## 🟡 MEDIUM：TR 未触发时 remaining 计算有隐蔽缺陷

**位置：** `main.py:1395` + `main.py:1405`

```python
tr_remaining = trail_dd_pct - drawdown if drawdown >= 0 else abs(drawdown)
...
"remaining": max(0, tr_remaining),
```

当 `drawdown < 0`（盈利扩大）时，`tr_remaining = abs(drawdown)`。例如 `drawdown=-1.0`，`tr_remaining=1.0`。

此时 `max(0, 1.0) = 1.0`，进度条会显示 1.0/2.0*100 = 50% 黄色进度条——但实际盈利在扩大，没有任何危险。这不是 bug，但是**视觉效果令人困惑**（盈利扩大却显示"还有一半距离触发移动止盈"）。

 PLAN 设计稿没有提到这种边界情况。

---

## 🟡 MEDIUM：进度条 topItem 选择策略在同优先级时随机

**位置：** `live_trader.js:676-681`

```js
const topItem = (pos.risk_items || [])
  .filter(it => it.type !== 'FD')
  .sort((a, b) => {
    const p = { danger: 3, warning: 2, safe: 1 };
    return (p[b.status] || 0) - (p[a.status] || 0);
  })[0];
```

当存在多个同 status 的 risk_item（如 HS=safe，TR=safe，TF=safe），排序后 `max` 返回数组中第一个遇到的（JS 稳定排序）。这意味着 topItem 的选择是随机的——取决于 risk_items 列表的顺序（由 main.py 添加顺序决定）。HS→TR→TF→FD→TC→TP 的添加顺序决定同 status 时永远优先选 HS。这**不是 bug**（HS 确实最优先），但没有显式文档化。

---

## 🟡 MEDIUM：测试没有覆盖的边界场景

| 边界场景 | 是否有测试 | 备注 |
|----------|-----------|------|
| `holding_days=2` 时走 T+1 还是普通 HS | ❌ 无 | 边界值 2 应走普通 HS，但无测试 |
| `profit_rate == hard_stop_pct`（刚好等于止损线） | ❌ 无 | `hs_triggered = profit_rate <= hard_stop_pct`，等于时触发，但无测试 |
| `avg_cost < 0`（负成本） | ❌ 无 | `avg_cost <= 0` 时跳过，但 `avg_cost = -1` 是否也跳过？代码是 `<= 0` |
| `drawdown == trail_dd_pct`（刚好等于阈值） | ❌ 无 | 边界值触发，但无测试 |

---

## 🟢 LOW：FD risk_items 缺少 budget/remaining

FD 是二元触发，没有 budget/remaining 字段。这是设计决策（跳过 FD），已在 PLAN 中说明。

---

## 🟢 LOW：TP 解析失败静默 fallback 不够明确

TP 解析失败时，`tp_triggered_flag = False` 静默 fallback。这意味着解析失败等同于"未触发"。虽然加了 logger.warning，但程序行为上无法区分"真的没触发"和"解析失败了"。

---

## 📊 严重问题汇总

| 级别 | 数量 | 说明 |
|------|------|------|
| 🔴 CRITICAL | 1 | 测试函数结构与 main.py 不一致，测试测的不是真实代码 |
| 🔴 HIGH | 1 | safe+remaining>0 时进度条显示 yellow，误导用户 |
| 🟡 MEDIUM | 3 | TR 盈利扩大进度条语义混淆、topItem 选择策略无文档、边界测试缺失 |
| 🟢 LOW | 2 | FD 无 budget/remaining、TP 静默 fallback |

**整体评分：3/10**

**核心缺陷：** 测试函数不是 main.py 的镜像——14 个测试 PASS 不代表 main.py 正确，这是自欺。进度条颜色逻辑错误（HIGH）会让用户在 safe 状态下看到黄色进度条。

---

*审计完成时间: 2026-07-14 22:05 (周二)*
