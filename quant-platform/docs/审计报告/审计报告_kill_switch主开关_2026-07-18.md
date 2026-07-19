# 审计报告：Kill Switch 主开关功能

> 日期：2026-07-18（改动日期 2026-07-16）
> 范围：仅今天新增的 `kill_switch_enabled` 主开关功能（7 个源文件 + 1 个测试文件）
> 方法：单 code-reviewer agent 快速审，结论逐条 Read/Grep 实际代码验证（file:line），无派子 agent
> 迭代：审计发现 2 个 MEDIUM 已修复并加回归测试

## 背景

2026-07-16 14:52:32 尾盘选股时，闸门5a fail-safe（开盘资产基准缺失）导致 11 只买单全拒，
5 次 risk 拒绝堆满 → 闸门7 熔断 → kill switch 激活。用户要求做一个"可运行时禁用整个急停机制"的主开关。

实现方式：照搬 `auto_buy_enabled` 模式（config 种子 + runtime_state + 持久化 + HTTP + 前端）。

## 验收标准逐条核对（8 条）

| # | 标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 关时 `activate()` 空操作，返回 False | ✅ PASS | kill_switch.py:55-57，短路在写内存/文件/DB/通知之前 |
| 2 | 关时 `is_active()` 恒 False（含残留） | ✅ PASS | kill_switch.py 短路在三重状态检查之前，全部 12 个调用点自动继承 |
| 3 | `status()` 含 `enabled` 字段 | ✅ PASS | kill_switch.py status() |
| 4 | 闸门7 关时不熔断不拦单 | ✅ PASS | risk_gate.py 双重防护（拦单判断 + _record_rejection 激活前再查） |
| 5 | 关闭时清理残留（内存+文件+DB） | ✅ PASS（修复后） | main.py set_switches 直接调 deactivate()；kill_switch.deactivate() 已改 |
| 6 | 关时 /live/kill-switch/activate 返 409 | ✅ PASS | main.py activate 端点 |
| 7 | 前端"急停功能"开关完整 | ✅ PASS | index.html 开关行+摘要；live_trader.js badge/映射/二次确认；缓存 v18→v19 |
| 8 | 向后兼容（不传 enabled_check 默认启用） | ✅ PASS | enabled_check=None→True；旧 app_setting.json 无字段时 fallback True |

## 审计发现 → 修复状态

| 严重度 | 问题 | 状态 |
|---|---|---|
| MEDIUM-1 | **幽灵通知**：3 处在 activate 空操作后仍发"急停已激活"通知（scheduler 非交易日 / main.py QMT连接失败 / main.py live残留检查）。主开关禁用时每周末必现假告警 | ✅ **已修复**：3 处改为依 `activate()` 返回值决定是否通知 |
| MEDIUM-2 | **DB-only 残留清不掉**：deactivate() 内存 flag 为 False 时提前 return，跳过文件/DB 清理 → 重新启用时幽灵复活 | ✅ **已修复**：deactivate() 不再提前 return，文件/DB 照常清理（幂等） |
| LOW-1 | 测试缺口：无端点 409 测试、enabled_check 异常分支无测试、DB-only 残留路径无测试 | ⚠️ 部分已补：新增 DB-only 残留 + scheduler 幽灵通知 2 个回归测试；端点 409 测试未加（HTTP 层，当前 smoke 套件不测端点） |
| LOW-2 | 文件残留启动时无条件加载，禁用期间保留、重新启用即复活（fail-safe 方向保守，可接受） | ⚠️ 不改：建议在前端确认文案写明，已在前端 hint 注明 |

## 其他维度

- **安全性**：关急停只中和闸门8 + 闸门7；闸门6（QMT未连接）、闸门5a（fail-safe）、闸门1/2/10 全部仍在，无保护真空。PUT /live/config/switches 有 `_require_admin` + 前端确认对话框双层防误触。
- **线程/锁安全**：三把锁（runtime_state._lock / KillSwitch._lock / risk_gate._rejection_lock）无反向嵌套，无死锁。enabled_check 异常 fail-safe 返回 True。
- **死代码/硬编码**：无未用 import；前端颜色全走 CSS var；`lt-badge--warn` 类存在于 main.css。

## 修复改动清单（本轮迭代）

1. `kill_switch.py` — `deactivate()` 移除提前 return，DB/文件清理幂等执行（MEDIUM-2）
2. `scheduler.py` — `_handle_non_trading_day` 通知改依 activate 返回值（MEDIUM-1①）
3. `main.py` — QMT连接失败 activate 返回值门控通知（MEDIUM-1②）
4. `main.py` — live残留检查 activate 返回值门控通知 + 文案修正（MEDIUM-1③）
5. `tests/test_live_trader_smoke.py` — +2 回归测试（DB-only 残留清理、scheduler 幽灵通知）

## 测试

- `test_live_trader_smoke.py`：**25 passed**（含 6 个 kill_switch 相关测试）
- py_compile 全过、node -c 全过
- ⚠️ `test_critical_fixes.py` 有 2 个 **pre-existing** 失败（callback_handler.py:463 `filled_price` 未定义，与本功能无关，stash 验证过）

## 总评

**可上线。** 主干 8 条验收标准全部满足，2 个 MEDIUM 已修复并加回归测试。核心机制（短路位置、闸门7 双保险、向后兼容、锁安全、前后端字段一致性）实现质量高。

**遗留建议**（非阻塞）：
- 端点级 409 测试可后续补（需 FastAPI test client）。
- 治本项：今天真正的根因是"开盘资产基准缺失"导致闸门5a 全天 fail-safe。建议另修"QMT 开盘未连上时用 live_capital 兜底基准 / 开盘补采资产"，避免 kill switch 禁用只是绕过症状。
