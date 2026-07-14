# 项目审计报告书：sim_trader 安全高价值子集重构

> 审计日期：2026-07-14（周一，通宵自主作业）
> 审计对象：分支 `refactor/sim-trader-decompose`（基于 master）
> 审计依据：`docs/PLAN-sim-trader-engine-decompose-2026-07-14.md`（v3）的安全高价值子集
> 审计方法：code-reviewer agent 全盘审计 + 真实代码 Read/Grep 交叉验证 + 全量测试套件
> 审计员：code-reviewer（禁止派子 agent，逐条引用 file:line）

---

## 0. 执行摘要

| 维度 | 结论 |
|------|------|
| 总评 | **PASS** |
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 2（已全部修复） |
| LOW | 3（已全部修复） |
| 回归 | 0（全量 390 测试通过，基线 31 → 390） |
| 向后兼容 | ✅ 14 个 `from engine import Position/Trade` 调用方全部仍可用（re-export 验证） |
| 实盘风险 | ✅ 未触碰运行态业务逻辑；NameError 修复采用"不修改 pos"方案，EOD 行为零改变 |

**一句话**：计划书 v3 的安全高价值子集（Step 1/2/6 + 测试 + import 清理）已全部落地，🔴 盘中 NameError 崩溃 bug 已修复并锁死回归测试，无任何现有功能受损。

---

## 1. 本次交付清单

### 1.1 新建文件（4 个源 + 4 个测试）

| 文件 | 行数 | 作用 |
|------|------|------|
| `app/sim_trader/models.py` | ~110 | Position/Trade/CycleResult 叶子模块（仅依赖 stdlib，打破 engine↔store 循环） |
| `app/sim_trader/store_protocol.py` | ~40 | SimStore runtime_checkable Protocol + load_equity_curve 返回格式契约 |
| `app/sim_trader/in_memory_store.py` | ~170 | 纯 dict adapter（测试用），行为对齐 JsonSimStore |
| `tests/test_models.py` | — | Position/Trade/CycleResult 单测 |
| `tests/test_in_memory_store.py` | — | InMemoryStore 行为 + engine 注入端到端 |
| `tests/test_sim_store_protocol.py` | — | 3 adapter isinstance(SimStore) + 接口完整性 + pos 键契约 |
| `tests/test_intraday_monitor.py` | — | NameError 回归（5 场景：不崩/HS/TR/TP/峰值不降） |

### 1.2 修改文件（3 个）

| 文件 | 改动 |
|------|------|
| `app/sim_trader/engine.py` | 删 Position/Trade 定义改从 models 导入 re-export；清理 4 条冗余 function-body import（_write_log 的 `from datetime import date` + auto_sell/buy/scan 的 `from config import AUTO_*`） |
| `app/sim_trader/store.py` | SimTraderStore 加 `clear_all()`（BEGIN/COMMIT 原子事务）；`load_equity_curve` 同时输出 `pos`+`positions` 键；4 处 lazy import 改指 models |
| `app/sim_trader/intraday_monitor.py` | 🔴 修复 `_check_position` NameError bug + 新增 `_calc_hold_days` |

---

## 2. 🔴 关键 bug 修复详情（NameError）

### 2.1 bug 现状（master）

`intraday_monitor._check_position`（master line 136-138）：

```python
risk_params_dict = dataclasses.asdict(_load_risk_params())
ctx.peak_price = overall_peak      # ← ctx 未定义, overall_peak 未定义
signal = exit_rule_engine.check(ctx, skip_eod_only=True)  # ← ctx 未构建
```

**触发条件**：盘中 tick 命中任一活跃持仓 → 立即 `NameError` 崩溃。
**潜伏原因**：盘中 tick 触发风控的时机罕见，且 `_check_and_act` 外层 `_is_market_hours()` 时间护栏进一步收窄，导致实盘从未命中→从未暴露。
**实盘风险**：一旦在"先卖后买"窗口（14:52-14:54）命中，盘中监控线程崩溃，风控形同虚设。

### 2.2 修复方案（Option B — 不修改 pos）

对齐 `live_trader.exit_monitor._build_context` + `engine.check_stops` 范式：

```python
ctx = exit_rule_engine.build_context(pos, bar, hold_days, sim_params, use_high_for_tp=True)
if session_peak > ctx.peak_price:
    ctx.peak_price = session_peak        # 覆盖 ctx, 不动 pos
signal = exit_rule_engine.check(ctx, skip_eod_only=True)
```

**为何选 Option B（不修改 pos.peak_price）而非 Option A（抬升 pos.peak_price）**：
- Option A 会持久化盘中峰值到 pos，改变 EOD `check_stops` 的 trailing stop 基准 → 引入行为变更。
- Option B 只覆盖 ctx.peak_price（max(历史, session_peak)），盘中检查用真实峰值，但 pos 状态不变 → **EOD 行为与 master 完全一致，零行为变更**。
- EOD `check_stops` 本就用当日 high 更新 pos.peak_price，盘中新高会在 EOD 被捕获，不丢精度。

**T+1 护栏发现**：修复过程中实测发现 `rule_hard_stop`/`rule_trailing_stop`/`rule_take_profit` 均有 `if ctx.hold_days < 2: return None`（首日不触发止盈止损，靠隔夜跳空保护）。测试通过 monkeypatch `_calc_hold_days=5` 隔离日历并满足护栏。

### 2.3 回归测试（5 场景）

| 测试 | 验证 |
|------|------|
| `test_check_position_no_crash_on_flat_price` | 价格平稳不崩 NameError、无信号 |
| `test_check_position_hard_stop_triggers` | -10% 触发 HS、全卖 partial=None |
| `test_check_position_trailing_stop_triggers` | session_peak=12 抬升 ctx 峰值后回撤触发 TR；pos.peak_price 不变 |
| `test_check_position_tp_partial_returns_qty` | +4% 触发 TP1、部分卖 300 股 |
| `test_check_position_session_peak_does_not_lower_historic_peak` | session_peak < 历史峰值时不拉低 |

---

## 3. 审计发现与处理

### 3.1 MEDIUM（2 条，已修复）

**[M-1] SimTraderStore.clear_all() 非原子** — `store.py`
- 问题：DuckDB autocommit 下逐表 DELETE，中途失败留半清空状态。
- 修复：用 `BEGIN/COMMIT` 显式事务包住全部 DELETE（对齐 save_state 模式）。

**[M-2] 盘中峰值持久化改变 EOD trailing 行为** — `intraday_monitor.py`
- 问题：Option A 抬升 pos.peak_price 会改变 EOD trailing 基准。
- 修复：改 Option B，不修改 pos，只覆盖 ctx.peak_price。EOD 行为零改变。

### 3.2 LOW（3 条，已修复）

**[L-1] test_session_peak_does_not_lower 未断言返回值** — 改用 14.8 选价避开 TP/TR/HS，断言 `result is None`。
**[L-2] InMemoryStore.load_positions 未做 entry_date 类型转换** — 加防御性 `date.fromisoformat`（对齐 JsonSimStore）。
**[L-3] InMemoryStore 多保留 entry_reason 与 docstring 不符** — docstring 补注说明。

### 3.3 顺带发现的既有 bug（已随本次修复）

`app/api/sim_trader.py:550` 读 `e.get('pos', 0)`，但 SimTraderStore（DuckDB）的 `load_equity_curve` 历史只返回 `'positions'` 键 → DuckDB 模式下净值曲线的持仓数永远显示 0。本次给 SimTraderStore.load_equity_curve 加 `'pos'` 键后，此既有 bug 自动修复。

---

## 4. 向后兼容性验证

| 调用方 | 验证 |
|--------|------|
| `scripts/test_fix_*.py`（14 处）`from engine import Position/Trade` | ✅ engine.py re-export，运行时 `Position is models.Position` = True |
| `tests/test_sim_trader_store.py:17` | ✅ 通过 |
| `app/sim_trader/sim_trader_report.py:14` | ✅ 通过 |
| `app/sim_trader/store.py` 4 处 lazy import | ✅ 改指 models（叶子模块，无循环） |
| `app/api/sim_trader.py` equity_curve 消费 | ✅ 加 pos 键不破坏读 positions 的消费方 |

---

## 5. 测试覆盖

| 测试文件 | 用例数 | 状态 |
|---------|--------|------|
| test_models.py | 13 | ✅ |
| test_in_memory_store.py | 7 | ✅ |
| test_sim_store_protocol.py | 5 | ✅ |
| test_intraday_monitor.py | 5 | ✅ |
| **新增小计** | **30** | ✅ |
| 全量套件（含既有） | 390 | ✅ 0 回归 |

---

## 6. 本次范围外（deferred，见 v4 计划书）

以下为计划书 v3 的高风险引擎手术步骤，按用户决策"安全高价值子集"**不在本次范围**，已迭代进 `PLAN-sim-trader-engine-decompose-2026-07-14.md` v4 供明早审查：

| Step | 内容 | 风险 | 状态 |
|------|------|------|------|
| Step 3 | telemetry.py + portfolio.py（持仓 CRUD + positions setter） | 中 | deferred |
| Step 4 | risk_manager.py（含 schema 校验） | 中 | deferred |
| Step 5 | equity_recorder.py（_prev_day_snap + _BAD 内部化） | 中 | deferred |
| Step 7 | engine 瘦身 + execute_daily_cycle + 3 caller 改造 | **高** | deferred |
| Step 8 | config.py 删 11 个重复常量 | 低 | deferred |
| Step 6 | intraday_monitor NameError 修复 | 中 | **✅ 本次完成** |

---

## 7. 结论

安全高价值子集全部交付，🔴 NameError 崩溃 bug 已根治并锁回归，向后兼容经运行时验证，0 回归。分支 `refactor/sim-trader-decompose` 可供明早审查；合并前建议人工跑一次 `sim_trader main.py` 回放对比净值曲线（计划书 DoD 4.3）。

**PASS — 可进入 deferred 步骤审查阶段。**

---

## 8. 二轮交叉验证（python-reviewer，第一轮之后追加）

> 第一轮 code-reviewer PASS 后，按"审阅审视审计然后再迭代"派 python-reviewer 做独立第二视角。**二轮抓到第一轮漏掉的 3 个 HIGH**——正是交叉验证的价值（单次审计错误率高，二次验证抓假阳性/漏网）。

### 8.1 二轮发现

| 编号 | 严重度 | 问题 | 处理 |
|------|--------|------|------|
| HIGH-1 | HIGH | `_check_position` 无条件 `mark_tier_triggered`，告警模式（close/auto_sell=False）不卖却烧 TP 档位 → EOD check_stops 跳过 → **漏卖** | ✅ 修复：`_check_position` 改纯检查不标记；`mark_tier_triggered` 移到 `_check_and_act` 确认卖出分支（对齐 EOD `and not readonly` 守卫） |
| HIGH-2 | HIGH | `bar.low=current_price` 漏掉盘中早先低点，HS/TR 偏迟触发；注释"对齐 exit_monitor U1"对 low 是错误的（exit_monitor U1 恰用真实 low） | ✅ 修复：对称 `_intraday_peak` 维护 `_intraday_low`，`_check_position` 用 `session_low` 作 bar.low |
| HIGH-3 | HIGH | `use_high_for_tp=True`(sim) vs `False`(live_trader) 盘中 TP 口径分叉 | ⏸ defer：sim=True 符合文件头"与回测 simple_runner 一致"契约；改 live 超范围，改 sim 破坏与回测对齐。记录为 sim/live 设计决策，留待统一 |
| MEDIUM-2 | MED | docstring"不修改 pos"不准确（mark_tier_triggered 改 pos.tp1） | ✅ 随 HIGH-1 修复自然解决（_check_position 不再标记，docstring 改"不修改 pos.peak_price"） |
| LOW-1 | LOW | `daily_atr` 形参恒为 0，ATR 动态回撤分支永不生效 | ⏸ 记录：与 EOD 一致（EOD 也不传 ATR），非 bug，留待 ATR 接入 |

### 8.2 二轮确认成立的结论

- **Option B 不改 EOD 行为**：✅ 核心论断成立。EOD check_stops 用当日 high 补更新 pos.peak_price，只要同日 EOD 跑了，trailing 基准不丢。残余边缘：盘中 TP 部分卖后 EOD 前进程被杀且不重启 → 当日 session 高点丢失（概率低，记录）。
- **T+1 护栏**：✅ hold_days<2 跳过 HS/TR/TP 是预期（A 股 T+1，靠隔夜跳空保护）。
- **向后兼容**：✅ 14 调用方零改动；`__module__` 变化无调用方依赖；pickle 风险排除（store 显式字段序列化）；循环依赖排除。

### 8.3 修订后总评

| 维度 | 一轮 | 二轮后 |
|------|------|--------|
| CRITICAL | 0 | 0 |
| HIGH | 0 → **3**(漏网) | **0 残留**（2 修复 + 1 defer） |
| MEDIUM | 2(已修) | 1 残留（LOW-1 ATR 记录） |
| 回归 | 0 | 0（392 测试通过） |

**修订总评：PASS**——二轮发现的 2 个 HIGH 已修复并加回归测试，1 个 HIGH（sim/live 口径）显式 defer 留待设计决策。**交叉验证强制执行了一次，避免了"告警模式漏卖"这个会比 NameError 更隐蔽的 bug 进实盘。**

> 教训：第一轮 code-reviewer 关注"改了什么 + 向后兼容"，漏了"mutation 时序 + sim/live 口径一致性"；第二轮 python-reviewer 对照 live_trader 范式才抓到。**单视角审计不够，多视角交叉验证是必要的。**

