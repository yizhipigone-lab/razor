# 计划：风控监控面板核查修复（2026-07-14）

> 基于 `docs/AUDIT-VERIFY-work-2026-07-14.md` 核查结论
> 修复 6 个真实问题（剔除审计误判项）

---

## 改动清单

### P0-1：T+1 保护缺失

**位置：** `app/live_trader/main.py` HS 硬止损块（:1338-1358）

**现状：** HS 块无 `holding_days < 2` 判断，持仓第 1 天也正常参与 HS 触发判断。

**改动：** 在 HS 块中，`hs_triggered` 判断前加入 T+1 保护逻辑：
- `holding_days < 2` 时，强制 `hs_status = "safe"`，`hs_message` 标注 "T+1 保护，持仓不足2天不触发硬止损"
- 已触发场景（profit_rate 已 <= 止损线）也走同一条保护路径

```python
# 在 hs_triggered 判断之前插入
if holding_days < 2:
    hs_status = "safe"
    hs_message = "T+1保护，持仓不足2天不触发硬止损"
    risk_items.append({
        "type": "HS", "label": "硬止损",
        "trigger_value": hard_stop_pct,
        "current_pnl": profit_rate,
        "remaining": abs(hard_stop_pct),  # 仍填 budget
        "budget": abs(hard_stop_pct),
        "status": hs_status,
        "message": hs_message,
    })
    # 跳过后续普通 HS 逻辑
else:
    # 原有逻辑
```

**验证：** `holding_days=1` 时 HS 始终 safe；`holding_days>=2` 按原逻辑判断。

---

### P0-2：pytest 测试文件缺失

**位置：** 新建 `tests/test_live_trader_risk_monitor.py`

**覆盖场景（按 PLAN:269-289）：**

| 测试函数 | 场景 | 预期结果 |
|----------|------|----------|
| `test_hs_triggered` | profit_rate=-6.7%, hard_stop=-6% | HS status="danger", remaining=0 |
| `test_hs_not_triggered` | profit_rate=-5%, hard_stop=-6% | HS status="safe", remaining=1% |
| `test_hs_hold_days_lt2` | hold_days=1, profit_rate=-7% | HS status="safe", message 含 "T+1" |
| `test_tr_not_triggered` | drawdown=1.5%, trail_dd=2% | TR status="safe", remaining=0.5% |
| `test_tr_triggered` | drawdown=2.6%, trail_dd=2% | TR status="warning", remaining=0 |
| `test_avg_cost_zero` | avg_cost=0 | 跳过风控计算，不在 positions 结果中 |
| `test_tf_triggered` | holding_days=12, tf_days=12 | TF status="danger", remaining=0 |
| `test_tc_safe` | holding_days=7, pnl=2%, threshold=3% | TC status="safe" |
| `test_tp_tier_triggered` | TP1 已触发 | TP1 status="warning", remaining=0 |

**实现方式：** 直接测试 `get_risk_status` 端点（FastAPI TestClient），mock 掉 store/QMT 依赖。

---

### P1-1：进度条未实现

**位置：** `static/js/live_trader.js` `renderRiskMonitor()`（:651-691）

**PLAN 设计（PLAN:41-48）：**

| 状态 | 宽度 | 颜色 |
|------|------|------|
| 已触发（remaining ≤ 0） | 100% | `var(--red)` |
| 剩余 > 0 | `min(remaining / budget * 100, 95)%` | `var(--yellow)` |
| safe | `0%` | `var(--green)` |

**改动：** 将现有的纯 table 渲染，改为每行内嵌进度条列：

```js
// 每行新增进度条列（td）
const pct = it.remaining <= 0 ? 100 : Math.min(it.remaining / it.budget * 100, 95);
const barColor = it.status === 'danger' ? 'var(--red)'
                : it.status === 'warning' ? 'var(--yellow)'
                : it.status === 'safe' && it.remaining > 0 ? 'var(--yellow)'
                : 'var(--green)';
'<td><div style="background:var(--bg2);border-radius:3px;height:6px;width:100%">'
  + '<div style="width:' + pct + '%;background:' + barColor + ';height:6px;border-radius:3px"></div>'
  + '</div></td>'
```

进度条仅显示 global_status 最高的那个 risk_item（danger > warning > safe）。

---

### P1-2：ATR 模式提示缺失

**位置：** `app/live_trader/main.py` risk_status API 响应 + `static/js/live_trader.js` 渲染

**现状：** `use_atr_trail=true` 时无任何提示。

**改动：**
1. API 响应顶层加 `risk_params.use_atr_trail`（已有）和 `risk_params.atr_note` 字段：
   ```python
   if rp.use_atr_trail:
       risk_params["atr_note"] = "移动止盈基于ATR计算，显示与实际触发可能存在偏差"
   ```
2. 前端在风险面板标题旁显示 note（当 atr_note 存在时）。

---

### P2-1：TC 逻辑需核实

**位置：** `app/live_trader/main.py` TC 块（:1423-1439）

**问题：** TC 语义"持仓超 N 天且盈利不足时退出"，但代码逻辑 `profit_rate >= tc_profit_threshold → warning`，逻辑方向需确认。

**行动：** 先 grep `exit_monitor.py` 和 `exit_rules.py` 确认 TC 真实触发条件，再决定是否修改。**本次只做确认+文档记录，不盲目改逻辑。**

---

### P2-2：TP 解析静默失败

**位置：** `app/live_trader/main.py` TP tiers 解析块（:1447-1455）

**现状：** `except Exception: pass` 静默吞掉解析错误。

**改动：** 解析失败时加日志：
```python
except Exception as e:
    logger.warning(f"TP tiers 解析失败 tp_triggered={tp_triggered!r}: {e}")
```

---

## 不做的事

- 不改 TR status（维持 warning，按 PLAN 设计）
- 不改 `current_price` 字段（后端返回 last_close 是设计决策，前端 applyLiveQuotes 覆盖）
- 不移除 lru_cache（进程级缓存影响极小）
- 不改 git 历史

---

## 验证方法

1. 启动 live_trader，进实盘 tab
2. 有持仓时确认进度条出现，safe 时宽度 0%
3. `holding_days=1` 的持仓 HS 显示 T+1 保护
4. pytest `tests/test_live_trader_risk_monitor.py` 全部通过
5. `use_atr_trail=true` 时面板有 ATR 提示
6. TP tiers 解析失败时 server log 有 warning
