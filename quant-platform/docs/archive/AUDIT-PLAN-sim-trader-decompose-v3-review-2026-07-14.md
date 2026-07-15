# 审计报告：PLAN-sim-trader-engine-decompose v2 → v3 综合审查

> 审计对象：[PLAN-sim-trader-engine-decompose-2026-07-14.md](PLAN-sim-trader-engine-decompose-2026-07-14.md)（v2 审计迭代版）
> 审计日期：2026-07-14
> 审计方法：两份独立分析交叉验证
>   - 分析 A：主线 Read/Grep 逐条验证 + 调用链追踪
>   - 分析 B：code-reviewer agent AST 解析 + 全文阅读
> 代码基线：master 分支当前 HEAD

---

## 总体评估：WARNING

**2 CRITICAL + 5 HIGH + 7 MEDIUM + 4 LOW**

v2 计划书在架构设计层面（模块拆分、Protocol 定义、RiskManager 双方法设计、实施步骤顺序）方向正确。问题集中在**数据准确性**和**实施细节覆盖不足**——计划书对 engine.py 的真实复杂度（function-body import 数量、公开方法数、常量重复数）多处估计偏差，导致验收标准中的量化指标失真，同时有几个关键设计缺口（冷却检查归属、校验逻辑迁移）未讨论。

CRITICAL 的 2 项必须在 v3 修复后才能开工。

---

## CRITICAL（2 项，必须修）

### CRITICAL-1: function-body import 数量严重低估 — 24 而非 9

**计划书声称**（line 22）："engine.py 里有 9 个函数体内的 import（line 33, 45, 195, 208, 267, 380, 439, 449, 479, 498, 511, 543）"

**问题**：
- 先说"9 个"却列出 12 个行号——自相矛盾
- AST 解析 + 人工核实，实际共 **24 条** function-body import，分布在 14 个函数/方法中
- 计划书漏了 12 条：`_write_log`(2)、`_validate_params_against_schema`(1)、`_fill_missing_snapshots`(3)、`monitor`(1)、`auto_sell`(1)、`auto_buy`(1)、`auto_scan`(1)、`check_stops`(1)、`sell_phase`(1)

完整清单见下表：

| 函数 | import 数 | 计划书提到？ | 备注 |
|------|----------|------------|------|
| `_get_stock_name` | 1 (line 33) | ✅ | DuckDB 查股票名 |
| `_safe_broadcast` | 1 (line 45) | ✅ | WebSocket |
| `_write_log` | 2 (line 55, 56) | ❌ 漏 | line 56 的 `from datetime import date` 与模块级 line 13 完全重复 |
| `_validate_params_against_schema` | 3 (line 195, 202, 208) | 部分 | 计划书只列了 line 195 |
| `_fill_missing_snapshots` | 4 (line 260, 267, 286, 287) | 部分 | 计划书只列了 line 267 |
| `monitor` | 1 (line 330) | ❌ 漏 | 惰性初始化 IntradayMonitor |
| `auto_sell` | 1 (line 344) | ❌ 漏 | 且与模块级 `*` import 完全冗余 |
| `auto_buy` | 1 (line 349) | ❌ 漏 | 同上 |
| `auto_scan` | 1 (line 354) | ❌ 漏 | 同上 |
| `build_live_snapshot` | 1 (line 380) | ✅ | |
| `execute_buy` | 2 (line 439, 449) | ✅ | |
| `check_stops` | 4 (line 479, 498, 511, 512) | ✅ | 计划书只列了 line 479, 498, 511，漏了 line 512 `import dataclasses` |
| `execute_sell` | 1 (line 543) | ✅ | |
| `sell_phase` | 1 (line 599) | ❌ 漏 | |
| **总计** | **24** | 计划书声称 9 | |

**影响**：Step 1 策略基于错误基线——"从 9 降到 ≤ 3"意味着需清理 6 条，但实际需清理 19-21 条。DoD 7.1 的 ≤ 3 目标基于错误前提。

**修复建议**：
- 更正计数为 24 条
- 4 条完全冗余可一步删除（line 56, 344, 349, 354）
- 其余按分类处理：数据获取 → 模块顶部、lazy import → 保留、RiskManager 相关 → Step 4 处理
- DoD 目标调整为 **≤ 5**（保留 intraday_monitor 惰性加载 + 可能的循环依赖规避）

---

### CRITICAL-2: `execute_daily_cycle` 未覆盖 SAME_STOCK_COOLDOWN 冷却检查

**计划书声称**：`execute_daily_cycle` 是"单一核心入口"，收口了三处重复的 sell→buy→record 流程。

**实际代码验证**：三个 caller 的买入循环中都有**完全相同的**冷却检查逻辑：

```python
# main.py:76-78 / api/sim_trader.py:354-356 / cron_jobs.py:503-505
if any(t.code == code and (today - t.entry_date).days <= SAME_STOCK_COOLDOWN
       for t in engine.trades):
    continue
```

**问题**：计划书的 `execute_daily_cycle` 只接受预计算 signals 列表，完全不处理冷却过滤。如果冷却检查留在 caller 侧，三个 caller 仍需各自实现——这与"收口重复逻辑"的设计目标直接矛盾。如果放入 execute_daily_cycle，需要明确如何访问 `engine.trades`。

计划书**完全没有讨论此问题**。

**修复建议**：
- **推荐**：将冷却检查放入 `execute_daily_cycle`，在买入循环内部过滤已冷却的股票。三个 caller 的冷却逻辑完全一致，是理想的收口目标。
- **备选**：显式声明冷却检查留在 caller，但说明理由（如"冷却依赖 caller 侧的状态视图"）
- 无论哪种选择，必须在 plan 中明确并更新 DoD

---

## HIGH（5 项）

### HIGH-1: engine 公开属性/方法去向不完整

**计划书**只列出了 5 个 back-compat 薄包装方法（execute_buy、execute_sell、check_stops、record、refresh_trades_from_store）。但实际 engine 有 **17 个公开方法** + **7 个公开属性/property** 被多个调用方使用。

未被计划书覆盖的方法和属性：

| 属性/方法 | 类型 | 被谁使用 | 计划书去向 |
|-----------|------|---------|----------|
| `engine.cash` | 直接属性 | main, api, cron_jobs, intraday | ❌ 未说明 |
| `engine.trades` | 直接属性 | main, api, cron_jobs | ❌ 未说明（CRITICAL-2 依赖） |
| `engine.pause_until` | 直接属性 | main, api, cron_jobs | ❌ 未说明 |
| `engine.consecutive_losses` | 直接属性 | api | ❌ 未说明 |
| `engine._today_trades` | "私有"属性 | api, cron_jobs | ❌ 未说明 |
| `engine.positions` | 直接属性（含赋值） | intraday, sell_phase | ❌ 部分（property 设计但无 setter） |
| `engine.monitor` | lazy-init property | api, cron_jobs | ❌ 仅隐含保留 |
| `engine.monitor_enabled` | property | api, cron_jobs | ❌ 未说明 |
| `engine.auto_sell` | property | api, cron_jobs, intraday | ❌ 未说明 |
| `engine.auto_buy` | property | api, cron_jobs | ❌ 未说明 |
| `engine.auto_scan` | property | api, cron_jobs | ❌ 未说明 |
| `engine.max_buy_amount` | property | main, cron_jobs | ❌ 未说明 |
| `engine.active_positions` | 方法 | intraday, api | ❌ 未说明 |
| `engine.position_count` | property | main, api, cron_jobs, intraday | ❌ 未说明 |
| `engine.build_live_snapshot` | 方法 | intraday | → PortfolioManager（已提到） |
| `engine.total_equity` | 方法 | main, api, cron_jobs | → PortfolioManager（已提到） |
| `engine.equity_price_coverage` | 方法 | record | → PortfolioManager（已提到） |
| `engine.sell_phase` | 方法（95行） | main, api, cron_jobs | 被 execute_daily_cycle 取代 |

**修复建议**：在 §2.2 engine 改后 API 中补全**所有**公开属性/方法的去向表，标注每个：
- "保留在 engine 作为薄包装/委托 property"
- "委托到 [模块名]"
- "由 execute_daily_cycle 内部取代"

---

### HIGH-2: `sell_phase` 的 7 个附加步骤在 execute_daily_cycle 中遗漏

**计划书**（line 131-136）描述核心三步为 "risk.check_stops_eod → portfolio.execute_sells → portfolio.execute_buys → equity.record"。

**实际代码**：[sell_phase](app/sim_trader/engine.py#L596-L641) 除 check_stops + execute_sell 外，还执行了：

1. **交易时段护栏**（line 599-604）：09:25-15:05 外直接跳过
2. **卖出后 save_state 现金落袋**（line 628-630）：先确保现金落盘再清理持仓
3. **已平仓持仓清理**（line 632）：`self.positions = {k: v for k, v ... if v.is_active}`
4. **_prev_snap 更新**（line 634）：从 snapshot 复制快照
5. **_prev_day_snap 更新**（line 637）：deep copy _prev_snap
6. **prev_day_snap 持久化**（line 639-640）：L5 修复的关键步骤
7. **save_positions 持久化**（line 641）

execute_daily_cycle 的伪代码只覆盖了 check_stops + execute_sell 部分。这 7 个步骤在新架构中的归属必须明确。

**修复建议**：
- 扩充 execute_daily_cycle 的伪代码到 8-10 步，完整列出所有步骤
- 明确哪些步骤留在 execute_daily_cycle，哪些委托到子模块
- 注意步骤 2（先落盘现金再清理持仓）的**时序约束**不可颠倒

---

### HIGH-3: `_validate_params_against_schema` 删除前提不成立

**计划书声称**（line 456）：该函数"可以删除（已经 fail-fast in RiskManager.__init__）"

**实际代码验证**：Step 4 的 `RiskManager.__init__`（line 302-306）：
```python
def __init__(self, risk_params: RiskParams, exit_engine=None):
    self._params = risk_params
    self._params_dict = dataclasses.asdict(risk_params)
    self._engine = exit_engine or ExitRuleEngine()
```
**没有任何 fail-fast 或 schema 一致性校验**。该函数当前在 engine 启动时比较 config.py 常量与 risk_params.py schema 的一致性并告警——删除它意味着这个校验静默丢失。

**修复建议**：
- 在 RiskManager.__init__ 中加入一致性校验（推荐，与计划书声称一致）
- 或保留该函数作为 Engine 的启动自检

---

### HIGH-4: `engine.positions` 直接赋值模式未被覆盖

**位置**：[sell_phase:632](app/sim_trader/engine.py#L632)、[intraday_monitor._execute_sell:162-163](app/sim_trader/intraday_monitor.py#L162-L163)

计划书提议 `engine.positions` 变为 `@property` 委托到 `self._portfolio.positions`。但当前代码中至少 2 处（且都在本次重构范围内）执行了直接赋值：

```python
# engine.py:632 (sell_phase)
self.positions = {k: v for k, v in self.positions.items() if v.is_active}

# intraday_monitor.py:162-163 (_execute_sell)
self.engine.positions = {
    k: v for k, v in self.engine.positions.items() if v.is_active
}
```

如果 positions 变成只读 property（无 setter），运行时会抛 `AttributeError`。

**修复建议**：
- Engine 的 `positions` property 加 setter，委托 `self._portfolio.replace_positions(value)`
- 或把清理逻辑移入 `execute_daily_cycle` / `PortfolioManager.execute_sell` 内部，消除调用方的赋值需求

---

### HIGH-5: 行数减少预估缺乏依据

**计划书声称**：API 层 -50 行，cron 层 -30 行，main 层 -20 行。

**实际 caller 代码分析**：

| Caller | 当前 sell→buy→record 区域行数 | 可被 execute_daily_cycle 取代的部分 | 实际预估节省 |
|--------|---------------------------|----------------------------------|------------|
| main.py | ~15 行（68-82） | 3 行调用 → 1 行 | **~5-8 行** |
| api/sim_trader.py | ~45 行（326-365） | 信号生成/pre_market/cooldown/broadcast 都需保留 | **~10-15 行** |
| cron_jobs.py | ~80 行（468-532） | auto_sell/auto_buy 分支、信号生成、broadcast 都需保留 | **~10-15 行** |

主要收益不在行数减少，而在架构清晰度。建议修正数字或去掉具体行数声称。

---

## MEDIUM（7 项）

### MED-1: engine.py ≤ 200 行目标偏紧

保留 6 公开方法薄包装 + 4 property + execute_daily_cycle（~40 行）+ `__init__`（~25 行）+ imports（~30 行）+ `_fill_missing_snapshots`（约 50 行仍保留在 engine）≈ **180-280 行**。200 行上限偏紧但可能达成，取决于 `_fill_missing_snapshots` 是否移到 EquityRecorder。建议调整为 **≤ 250 行**更现实。

---

### MED-2: Store Protocol 缺少 `load_equity_curve` 返回格式规定

SimTraderStore 返回 `'positions'` 键，JsonSimStore 返回 `'pos'` 键——这是已知的不一致。Protocol 定义时应统一规定格式或标注差异。

---

### MED-3: InMemoryStore 偏差风险未列入风险表

如果 InMemoryStore 行为与 JsonSimStore 不完全一致，基于它的单测会是假阳性。应列入 §6 风险表。

---

### MED-4: CycleResult 缺少 `sell_signals` 字段

cron_jobs readonly 模式需要详细卖出信号（code + reason）来生成告警。当前 CycleResult 只有 `sell_count` 整数，不够。建议加 `sell_signals: list = field(default_factory=list)`。

---

### MED-5: IntradayMonitor 有两个功能重复的市场时间方法

`_in_trading_hours`（line 216-220，用分钟）和 `_is_market_hours`（line 69-75，用 HHMM）逻辑完全相同（都是 9:25-15:00）。重构时可顺手合并。

---

### MED-6: `_prev_snap` / `_prev_day_snap` 拆分后的更新时序需文档化

两者在 `sell_phase` 中耦合更新（line 634-637）：`_prev_day_snap = copy.deepcopy(_prev_snap)`。拆分到 PortfolioManager（_prev_snap）和 EquityRecorder（_prev_day_snap）后，`execute_daily_cycle` 需要协调这两个模块的更新时序。

---

### MED-7: `test_sim_trader_store.py` 直接访问 `eng_mod._BAD_EQUITY_CURVE_DETECTED`

DoD 7.1 要求该测试"一行不改"，但 7.2 加分项又要将 `_BAD_EQUITY_CURVE_DETECTED` 内部化到 EquityRecorder——这两条自相矛盾。需二选一：要么 engine 保留 re-export 兼容，要么接受测试也要改。

---

## LOW（4 项）

### LOW-1: engine.py line 56 + line 344/349/354 的冗余 function-body import

`_write_log` 内 `from datetime import date` 与模块级 line 13 重复。`auto_sell`/`auto_buy`/`auto_scan` 内 `from app.sim_trader.config import ...` 与模块级 `*` import 重复。这些可在 Step 1 直接删除，零风险。

---

### LOW-2: 缺少 `intraday_monitor → RiskManager` 的 ADR

计划书 §9 列了两个 ADR，但 RiskManager 的两个方法（check_stops_eod vs check_stops_intraday）的设计决策（`use_high_for_tp` / `skip_eod_only` 参数语义差异）值得记录为第三个 ADR。

---

### LOW-3: Step 8 "保留在 config.py" 正文列表缺 `SAME_STOCK_COOLDOWN`

映射表中已正确标注 SAME_STOCK_COOLDOWN 保留，但正文的保留列表（INITIAL_CAPITAL / ... / MONITOR_MODE）中遗漏了它。

---

### LOW-4: v2 自评部分指标偏高

function-body import 严重低估表明对 engine.py 理解有盲区，完整性自评 4.5/5 偏高。建议下调至 3.5-4.0/5。

---

## 不在本次范围 — 校验

| 计划书声明 | 校验结果 |
|-----------|---------|
| 不改 IntradayMonitor 事件总线 | ✅ PASS |
| 不重写 _check_and_act 市场时间 | ✅ PASS |
| 不动 live_trader 模块 | ⚠️ WARNING — `signal_picker.py` 的选股逻辑蓝本是 cron_jobs.py 的信号获取段，如 cron_jobs 执行流程改变需同步更新参考注释 |
| 不重构 RiskParams | ✅ PASS |
| 不重新设计 ExitRuleEngine | ✅ PASS |
| 不动 backtest 模块（scripts/） | ✅ PASS — 回测脚本维持原 API 调用 |

---

## 关键数据交叉验证

| 计划书主张 | 计划书值 | 实际值 | 判定 |
|-----------|---------|--------|------|
| engine.py 行数 | 701 | 701 | ✅ PASS |
| function-body import 数 | 9 | **24** | ❌ FAIL |
| SimTraderEngine 公开方法 | 18（23 含 Position） | 17（+ 3 私有 = 20） | ⚠️ WARNING |
| Position dataclass 公开方法 | 5 | 6 | ⚠️ WARNING |
| config.py 与 RiskParams 重复常量 | 12 | 11（SAME_STOCK_COOLDOWN 无对应） | ⚠️ WARNING |
| intraday_monitor NameError bug | 存在（line 136-138） | 确认存在 | ✅ PASS |
| SimTraderStore vs JsonSimStore 签名 | 几乎相同 | 确认，JsonSimStore 有 clear_all() 但 SimTraderStore 无 | ✅ PASS |
| 3 个 caller 重复 daily cycle | 存在 | 确认存在，但"重复"程度低于声称 | ⚠️ WARNING |
| sell_phase 除权跳空保护 | _prev_day_snap | 确认 | ✅ PASS |
| 风控参数统一走 risk_params.py | v5.5 已完成 | 确认 | ✅ PASS |

---

## Review Summary

| Severity | Count | Action |
|----------|-------|--------|
| CRITICAL | 2 | **BLOCK** — 必须 v3 修复才能开工 |
| HIGH | 5 | **WARN** — 应在 v3 修复 |
| MEDIUM | 7 | **INFO** — 建议 v3 修复，部分可开工后处理 |
| LOW | 4 | **NOTE** — 可选 |

**最终结论：WARNING**

2 个 CRITICAL 必须在 v3 中解决：
1. **function-body import 基线修正**（24 而非 9）——调整 Step 1 策略和 DoD 目标
2. **SAME_STOCK_COOLDOWN 冷却检查归属**——决定放入 execute_daily_cycle 还是留在 caller

5 个 HIGH 强烈建议在 v3 中吸收。v3 不需要推翻架构设计，重点是补齐数据准确性、补全 API 去向表、扩充 execute_daily_cycle 的伪代码。
