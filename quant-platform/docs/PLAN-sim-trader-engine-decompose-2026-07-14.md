# 计划书：拆分 SimTraderEngine 大管家（v4 — 安全子集已落地版）

> 立项日期：2026-07-14
> 范围：`app/sim_trader/` 整个模块
> 触发：架构评审候选 ①+③（详见 `docs/审计报告/架构评审HTML_验证_2026-07-14.md`）
> v1 → v2 → v3 → v4 演进：v1 解决 6 HIGH + 10 MEDIUM，v3 解决 2 CRITICAL + 5 HIGH + 7 MEDIUM + 4 LOW，**v4 落地安全子集 + 实测修订 deferred 步骤**
> 风险等级：**中**（核心运行态模块，纯重构，无业务逻辑变更）

---

## v4 执行状态（2026-07-14 通宵自主作业，分支 `refactor/sim-trader-decompose`）

### 已完成（本次安全高价值子集，审计 PASS，390 测试 0 回归）

| Step | 状态 | 实际落地 vs v3 计划 |
|------|------|------|
| Step 1（models.py + import 清理） | ✅ 完成 | models.py 抽出 Position/Trade/CycleResult；engine.py re-export 保 14 调用方零改动；store.py 4 处 lazy import 改指 models 断循环。**import 清理仅做 4 条完全冗余**（24→20），剩余 16 条留给 deferred 深模块抽取时一并处理 |
| Step 2（Protocol + InMemoryStore） | ✅ 完成 | store_protocol.py 用 `@runtime_checkable` 结构化 Protocol（非显式继承，isinstance 通过）；InMemoryStore 行为对齐 JsonSimStore；SimTraderStore 加 `clear_all()`（BEGIN/COMMIT 原子）；`load_equity_curve` **同时输出 pos+positions 键**（加 pos 键顺带修了 api:550 既有 bug，保留 positions 不破坏老消费方） |
| Step 6（intraday NameError 修复） | ✅ 完成 | **详见 v4 实测修订下方** |

### 实测发现（注入 deferred 步骤的修订）

1. **NameError 修复采用 Option B（不修改 pos）**：构建 ctx 后覆盖 `ctx.peak_price = max(历史, session_peak)`，而非 v3 计划的"抬升 pos.peak_price"。原因：Option A 会持久化盘中峰值、改变 EOD trailing 基准；Option B 盘中检查用真实峰值但 pos 不变，EOD 行为与 master 零差异。**deferred Step 4 RiskManager.check_stops_intraday 必须沿用此 Option B 语义**。
2. **T+1 护栏**：`rule_hard_stop`/`rule_trailing_stop`/`rule_take_profit` 均有 `if ctx.hold_days < 2: return None`（首日不触发，靠隔夜跳空保护）。Step 4 RiskManager 测试必须覆盖 hold_days=1（不触发）与 hold_days≥2（触发）两条路径。
3. **hold_days 计算**：新增 `_calc_hold_days`（对齐 live_trader.exit_monitor），交易日计数 + 自然日兜底 + 异常返回 1。Step 4 RiskManager.check_stops_intraday 应复用此方法，不要重新发明。
4. **pos/positions 键**：SimTraderStore 现已同时输出两键。Step 8 config 清理时，建议把全仓消费方统一迁到 `pos` 键后，再考虑删 `positions`（本次保留以保兼容）。
5. **store.py lazy import 已改指 models**：Step 3 PortfolioManager / Step 5 EquityRecorder 注入 store 时，store 不再反向依赖 engine，循环已断，可顶层 import models。
6. **二轮复审 HIGH-1 已修**：`_check_position` 改纯检查不标记 TP 档位，`mark_tier_triggered` 移到 `_check_and_act` 确认卖出分支（对齐 EOD `and not readonly`）。Step 4 RiskManager.check_stops_intraday 若重写此逻辑必须保留此"告警模式不烧档位"语义。
7. **二轮复审 HIGH-2 已修**：盘中 `bar.low` 用 `_intraday_low`（对称 `_intraday_peak` 跟踪的盘中真实最低），不用当前 tick 价。Step 4 check_stops_intraday 须保留 session_low 参数。
8. **二轮复审 HIGH-3 defer（sim/live 口径决策）**：`use_high_for_tp=True`(sim 盘中) vs `False`(live_trader)。sim=True 符合"与回测 simple_runner 一致"契约，但与 live 分叉。**需用户决策**：盘中 TP 用 high（冲高即卖）还是 close（站上才卖），然后 sim/live 统一。未决策前不动。

### deferred（明早审查后决定是否做）

| Step | 内容 | 风险 | v4 备注 |
|------|------|------|------|
| Step 3 | telemetry.py + portfolio.py（持仓 CRUD + positions setter） | 中 | positions setter 仍需做：`intraday_monitor.py:162` 仍 `self.engine.positions = {...}` 赋值 |
| Step 4 | risk_manager.py（含 schema 校验） | 中 | check_stops_intraday 沿用 Option B；复用 _calc_hold_days；测试覆盖 T+1 护栏 |
| Step 5 | equity_recorder.py（_prev_day_snap + _BAD 内部化） | 中 | test_sim_trader_store.py 的 `eng_mod._BAD_EQUITY_CURVE_DETECTED` 4-5 行访问路径需改 |
| Step 7 | engine 瘦身 + execute_daily_cycle + 3 caller 改造 | **高** | 风险最高，建议单独审查 + 分小步提交 |
| Step 8 | config.py 删 11 个重复常量 | 低 | 顺带把消费方迁到 pos 键 |

> 完整审计：`docs/AUDIT-REPORT-sim-trader-safe-subset-2026-07-14.md`
> 完整成果：`docs/REPORT-sim-trader-safe-subset-2026-07-14.md`

---


## 0. 一句话目标

把 `SimTraderEngine` 这个 701 行、20 个公开方法 + 7 个公开属性的"大管家"拆成 **3 个深模块 + 1 个调度器**，用 **Protocol** 钉死两个 Store 实现，**同时修复 `intraday_monitor._check_position` 的 NameError bug**（v2 纳入），并**完整覆盖所有公开属性/方法的去向**（v3 修订）。

---

## 1. 问题陈述（为什么必须做）

### 1.1 当前的痛点

- **上帝对象**：`SimTraderEngine` 一个类同时管 6 件事——买卖执行、止损检查、净值记录、快照构建、状态校验、盘中监控。701 行、**20 个公开方法 + 7 个公开属性/property**（v3 实测纠正 v2 的"23"误判）。
- **24 条 function-body import（v3 实测）**：engine.py 在 14 个函数/方法内有 24 条延迟 import，分布如下（v3 完整清单，之前 v2 严重低估为 9 条）：

| 函数 | import 数 | 主要内容 |
|------|----------|---------|
| `_get_stock_name` | 1 (line 33) | DuckDB 查股票名 |
| `_safe_broadcast` | 1 (line 45) | WebSocket |
| `_write_log` | **2** (line 55, 56) | `json, os` + 冗余 `from datetime import date` |
| `_validate_params_against_schema` | **3** (line 195, 202, 208) | schema + config + settings |
| `_fill_missing_snapshots` | **4** (line 260, 267, 286, 287) | date/timedelta + api.sim_trader + Path + pandas |
| `monitor` | **1** (line 330) | 惰性 init IntradayMonitor |
| `auto_sell/buy/scan` | **3** (line 344, 349, 354) | 全部冗余 import config.*（模块顶部 `*` 已含） |
| `build_live_snapshot` | 1 (line 380) | quote_source |
| `execute_buy` | 2 (line 439, 449) | settings + execution |
| `check_stops` | **4** (line 479, 498, 511, 512) | exit_rules + adjust_for_gap + risk_params + dataclasses |
| `execute_sell` | 1 (line 543) | execution |
| `sell_phase` | **1** (line 599) | datetime |
| **总计** | **24** | |

- **循环依赖**：`Position`/`Trade` 数据类住在 `engine.py` 里，但 `store.py` 要用它们来序列化——全靠懒加载 import 掩盖。
- **Store 接口缺失**：两个 adapter 鸭子类型对接，无 Protocol/ABC。
- **重复的每日流程**：sell → buy → record 在 3 处各写一份（main / api / cron），**3 处都含相同的 `SAME_STOCK_COOLDOWN` 冷却检查**（v3 新增 CRITICAL-2）。
- **`sell_phase` 内有 7 个附加步骤**（v3 修订 HIGH-2）——非交易时段护栏、先落盘现金再清理持仓、_prev_snap / _prev_day_snap 更新、持久化等。计划书伪代码必须涵盖。
- **`engine.positions` 直接赋值模式**：sell_phase:632 + intraday_monitor.py:162-163 都在 `self.engine.positions = {...}` 赋值（v3 修订 HIGH-4），property 必须支持 setter。
- **🔴 隐藏的 NameError bug**：`intraday_monitor._check_position:136-138` 引用未定义变量，盘中 tick 触发会崩。

### 1.2 不做的代价

- 改止损逻辑要碰净值代码（高耦合）
- 加新的存储后端没人保证兼容
- 每次有人改一处，三处重复代码会漂移
- 🔴 盘中 tick 触发风控时机非常罕见，bug 潜伏很久没人发现——一旦在实盘"先卖后买"窗口触发就崩
- engine.py 的 24 条延迟 import 让 IDE 看不懂真实依赖图

---

## 2. 目标架构

### 2.1 拆完后长这样

```
app/sim_trader/
├── models.py            ← 新建:Position / Trade / CycleResult dataclass（叶子）
├── store_protocol.py    ← 新建:SimStore Protocol（含 load_equity_curve 返回格式契约）
├── store.py             ← 改:2 个 adapter 显式实现 Protocol + SimTraderStore 加 clear_all()
├── in_memory_store.py   ← 新建:测试用 InMemoryStore adapter
├── telemetry.py         ← 新建:_safe_broadcast / _write_log / _get_stock_name
├── portfolio.py         ← 新建:PortfolioManager（持仓 CRUD + 快照 + 估值 + 持有 _prev_snap）
├── risk_manager.py      ← 新建:RiskManager（check_stops_eod + check_stops_intraday + 启动校验）
├── equity_recorder.py   ← 新建:EquityRecorder（净值 + 校验 + 补齐 + 持有 _prev_day_snap + 内部化 _BAD_EQUITY_CURVE_DETECTED）
├── engine.py            ← 改成调度器（6 back-compat 方法 + 7 back-compat 属性 + execute_daily_cycle）
├── intraday_monitor.py  ← 改:修 NameError bug + 委托 RiskManager.check_stops_intraday + 合并两个时间方法
├── config.py            ← 改:删 11 个 RiskParams 重复常量
├── data_loader.py       ← 不变
├── main.py              ← 改:调 execute_daily_cycle
└── api/...              ← 不在本计划范围
```

### 2.2 Engine 改后完整 API 去向表（v3 补全 CRITICAL-1 + HIGH-1）

| 类别 | 现有 engine 成员 | v3 去向 | 理由 |
|------|------------------|--------|------|
| **公开属性** | `cash` | 委托到 `self._portfolio._cash` 或 `_state` | 主键读写 |
| | `trades` | 委托到 `self._portfolio._trades`（只读视图） | 含 list 语义 |
| | `pause_until` | 委托到 `self._portfolio._pause_until` | 写入时同步 state |
| | `consecutive_losses` | 委托到 `self._portfolio._consecutive_losses` | 同上 |
| | `_today_trades` | 保留在 engine（每日临时态，不持久化） | 日切时清空 |
| | `positions` | **property + setter**（v3 HIGH-4 修复），委托 portfolio | setter 触发 portfolio.replace_positions |
| **公开属性 (lazy)** | `_monitor` | 保留 lazy-init（避免循环） | |
| **property** | `monitor` | 保留 lazy-init | |
| | `monitor_enabled` | 保留 | 委托 `self._monitor.enabled` |
| | `auto_sell/buy/scan` | **改：模块顶部 import AUTO_*** | v3 删 3 条冗余 function-body import |
| | `max_buy_amount` | 委托 `self._portfolio.max_buy_amount` | |
| | `position_count` | 委托 `self._portfolio.position_count` | |
| **公开方法** | `active_positions()` | 委托 `self._portfolio.get_active()` | |
| | `build_live_snapshot()` | 委托 `self._portfolio.build_live_snapshot()` | |
| | `total_equity()` | 委托 `self._portfolio.total_equity()` | |
| | `equity_price_coverage()` | 委托 `self._portfolio.equity_price_coverage()` | |
| | `execute_buy()` | 委托 `self._portfolio.add_position()`（保留 back-compat） | |
| | `execute_sell()` | 委托 `self._portfolio.execute_sell()`（保留 back-compat） | |
| | `check_stops()` | 委托 `self._risk.check_stops_eod()`（保留 back-compat） | |
| | `sell_phase()` | **删除**：被 execute_daily_cycle 取代 | 3 caller 都改 |
| | `record()` | 委托 `self._equity.record()`（保留 back-compat） | |
| | `refresh_trades_from_store()` | 委托 `self._portfolio.load()`（保留 back-compat，`sim_trader_report.py:25` 还要用） | |
| | **`execute_daily_cycle()`** | **新增：单一核心入口**（v3 涵盖 sell_phase 全部 8 步 + SAME_STOCK_COOLDOWN） | |

**v3 关键修正**：v2 列的"5 back-compat + 1 新方法 = 6 个公开方法"——加上 7 个属性/property 后，**实际对外接口仍是 20+ 个成员**（保持向后兼容），只是内部委托到 3 个深模块。

### 2.3 核心接口契约

**`store_protocol.py`**（新建，~40 行）：

```python
from typing import Protocol, Optional
from datetime import date
from app.sim_trader.models import Position, Trade

class SimStore(Protocol):
    """模拟盘存储接口契约。所有 adapter 必须实现。

    返回格式约定（v3 MED-2 明确）:
      load_equity_curve() 返回 list[dict],每个 dict 必有键:
        date (str YYYY-MM-DD), equity (float), cash (float),
        pos (int),  # ⚠ 统一使用 'pos'（v3 修复 JsonSimStore vs SimTraderStore 不对称）
        source (str, optional)
    """
    def load_state(self) -> dict: ...
    def save_state(self, cash: float, consecutive_losses: int,
                   pause_until: Optional[date], trade_count: int) -> None: ...
    def load_positions(self) -> dict[str, Position]: ...
    def save_positions(self, positions: dict[str, Position]) -> None: ...
    def save_trade(self, trade: Trade) -> None: ...
    def load_trades(self) -> list[Trade]: ...
    def save_equity_point(self, d: date, equity: float, cash: float,
                          positions: int, source: str = 'record') -> None: ...
    def load_equity_curve(self) -> list[dict]: ...
    def save_prev_day_snap(self, snap: dict) -> None: ...
    def load_prev_day_snap(self) -> dict: ...
    # 可选：clear_all() 不在 Protocol 必选（v3 MED-2 统一约定）
```

**`models.py`** 新增 `CycleResult`（v3 MED-4 补 sell_signals 字段）：

```python
@dataclass(frozen=True)
class CycleResult:
    """execute_daily_cycle 返回值。字段对齐现有 caller 的 broadcast payload。
    v3 新增 sell_signals（cron_jobs readonly 模式需要详细信号）"""
    sell_count: int
    buy_count: int
    equity: float
    cash: float
    positions: int
    signals_count: int
    sell_signals: list = field(default_factory=list)   # v3: [(code, reason), ...]
```

**`engine.positions` setter（v3 HIGH-4 修复）**：

```python
@property
def positions(self) -> dict[str, Position]:
    return self._portfolio.positions

@positions.setter
def positions(self, value: dict[str, Position]):
    """intraday_monitor.py:162 和 engine.sell_phase:632 需要赋值清理已平仓持仓"""
    self._portfolio.replace_positions(value)
```

**`execute_daily_cycle` 完整伪代码（v3 HIGH-2 修复）**——10 步涵盖 sell_phase 全部：

```python
def execute_daily_cycle(self, today, snapshot, signals, trading_dates,
                        mode: Literal["execute", "readonly"] = "execute"
                        ) -> CycleResult:
    # ── Step 1: 非交易时段护栏（sell_phase line 599-604）──
    if not self._is_trading_hours():
        log.warning(f"非交易时段，跳过: {today}")
        return CycleResult(0, 0, self._equity.last_equity, self.cash, self.position_count, 0)

    sell_signals: list = []
    if mode == "execute":
        # ── Step 2: 风控检测（check_stops_eod，sell_phase line 606-607）──
        raw_sells = self._risk.check_stops_eod(
            positions=self._portfolio.positions,
            snapshot=snapshot,
            trading_dates=trading_dates,
            today=today,
            prev_snap=self._equity._prev_day_snap,  # sell_phase line 607
            readonly=False,
        )
        # ── Step 3: 执行卖出 + 持仓清理（sell_phase line 609-632）──
        for pos, exit_price, reason, partial in raw_sells:
            self._portfolio.execute_sell(pos, exit_price, reason, partial, today)
            sell_signals.append((pos.code, reason))
        # ── Step 4: 现金先落盘，再清理持仓（sell_phase line 627-632 P1-4 关键时序）──
        self._portfolio.persist_state()              # 先 save_state（现金落袋）
        self._portfolio.replace_positions(           # 再清理已平仓
            {k: v for k, v in self._portfolio.positions.items() if v.is_active}
        )
    else:  # readonly
        raw_sells = self._risk.check_stops_eod(..., readonly=True)
        sell_signals = [(p.code, r) for p, _, r, _ in raw_sells]

    # ── Step 5: 买入循环（含 SAME_STOCK_COOLDOWN 冷却检查，v3 CRITICAL-2 修复）──
    buy_count = 0
    if mode == "execute":
        max_new = int(self.cash / self.max_buy_amount()) + 1
        for code, price in signals[:max_new]:
            # 冷却检查从 caller 侧收回
            if self._portfolio.is_in_cooldown(code, today):
                continue
            if self._portfolio.add_position(today, code, price, strategy_name=""):
                buy_count += 1

    # ── Step 6: 净值快照（sell_phase 不做 record，但 execute_daily_cycle 包含）──
    self._equity.record(today, snapshot)             # 含覆盖率 + 跳变告警

    # ── Step 7: 更新 _prev_snap（sell_phase line 634）──
    self._portfolio.update_prev_snap(snapshot)

    # ── Step 8: 更新 _prev_day_snap（sell_phase line 637 deep copy）──
    self._equity.update_prev_day_snap(copy.deepcopy(self._portfolio._prev_snap))

    # ── Step 9: 持久化 _prev_day_snap（sell_phase line 639-640 L5 修复）──
    self._equity.persist_prev_day_snap()

    # ── Step 10: 持久化清理后的 positions（sell_phase line 641）──
    self._portfolio.persist_positions()

    return CycleResult(
        sell_count=len(sell_signals), buy_count=buy_count,
        equity=self.total_equity(snapshot), cash=self.cash,
        positions=self.position_count, signals_count=len(signals),
        sell_signals=sell_signals,
    )
```

### 2.4 depth vs shallowness 对比（v3 修订）

| 模块 | 改前 | 改后 |
|------|------|------|
| `engine.py` | 701 行，20 方法 + 7 属性 | ~220 行，6 方法 + 7 属性（纯委托），**24 → ≤ 5 条** function-body import |
| `store.py` | 两个类鸭子类型 | 两个类显式实现 Protocol + SimTraderStore 加 clear_all() |
| `models.py` | 嵌在 engine.py | 独立叶子模块（含 CycleResult + sell_signals） |
| `portfolio.py` | 不存在 | ~250 行（持仓管理 + 持有 _prev_snap + cooldown 检查 + persist） |
| `risk_manager.py` | 不存在 | ~180 行（**含启动 schema 一致性校验**，HIGH-3 修复） |
| `equity_recorder.py` | 不存在 | ~200 行（净值 + 校验 + 补齐 + 持有 _prev_day_snap + 内部化 _BAD 标志） |
| `intraday_monitor.py` | `_check_position` NameError | 委托 RiskManager + 合并 2 个时间方法（MED-5） |
| `telemetry.py` | 3 个 module-level helper | 集中到独立文件 |
| `config.py` | 12 个 RiskParams 重复常量 | 11 个删除 + SAME_STOCK_COOLDOWN 保留 |

---

## 3. 实施步骤（9 步，按依赖顺序）

### Step 1: 抽 models.py + 全面清理 24 条 function-body import（v3 CRITICAL-1 修复）

**目标**：搬 `Position` / `Trade` / 新增 `CycleResult`；**完整清理 24 条 import**（不是 v2 说的 9 条）。

**改动**：
- 新建 `app/sim_trader/models.py`，搬 `Position` 和 `Trade` 类。
- 新建 `CycleResult` 含 `sell_signals: list = field(default_factory=list)`（v3 MED-4）。
- 从 `engine.py` 删掉这两个类，改 `from app.sim_trader.models import ...`。
- **全面清理 24 条 function-body import**（v3 实测），分类处理：

| 类别 | 处理策略 | 行号 |
|------|---------|------|
| 完全冗余（删） | `_write_log` line 56、auto_sell line 344、auto_buy line 349、auto_scan line 354 | 4 条 |
| 模块顶部已有（移） | `from app.sim_trader.config import *` 已有，AUTO_* 移顶部 | 3 条 |
| 数据获取类（移顶部） | `database.duckdb_manager` / `server.websocket.manager` / `app.data_manager.quote_source` / `app.backtest.execution` / `core.settings` | 7 条 |
| RiskManager 相关（Step 4 处理） | `app.backtest.exit_rules` / `app.config.risk_params` / `dataclasses` | 4 条 |
| 内部 module（Step 5 处理） | `app.api.sim_trader._load_trading_calendar` / `_fill_missing_snapshots` 内部的 pathlib, pandas | 4 条 |
| 保留 lazy（防循环） | `app.sim_trader.intraday_monitor` (line 330) | 1 条 |
| 保留 lazy（合理） | `from datetime import datetime` (sell_phase line 599) / `_validate_params_against_schema` 内部 | 1 条 |

**验收**：
- engine.py function-body import 数：**24 → ≤ 5**（v3 实测目标，含必要的循环依赖规避）
- `tests/test_sim_trader_store.py` 100% 通过（不改测试）。

**风险**：低（搬位置 + 删冗余）。

---

### Step 2: 定义 SimStore Protocol + InMemoryStore

**目标**：定义 Protocol + 测试用 InMemoryStore + 统一 `load_equity_curve` 返回格式（v3 MED-2）。

**改动**：
- 新建 `app/sim_trader/store_protocol.py`，含完整契约注释（特别是 load_equity_curve 必须返回 'pos' 键，v3 修复 JsonSimStore 与 SimTraderStore 不对称）。
- 新建 `app/sim_trader/in_memory_store.py`，纯 dict-based adapter。
- **修复 SimTraderStore：把 `save_equity_point` 入库也用 'pos' 键**（v3 统一格式）。
- **SimTraderStore 加 `clear_all()`**（对齐 JsonSimStore，v2 修订）。
- 两个 adapter + InMemoryStore 都显式标注 `: SimStore`。

**验收**：
- smoke test `isinstance(adapter_instance, SimStore)` 通过。
- InMemoryStore 与 JsonSimStore 行为对齐测试。

**风险**：低（纯加文件 + 修小 bug）。

---

### Step 3: 抽 telemetry.py + PortfolioManager（含 SAME_STOCK_COOLDOWN 冷却 + positions setter 入口）

**目标**：3 helper 独立 + PortfolioManager 包含冷却检查（v3 CRITICAL-2）。

**新建 `app/sim_trader/telemetry.py`**：搬 3 个 helper。

**新建 `app/sim_trader/portfolio.py`**：
```python
class PortfolioManager:
    def __init__(self, store: SimStore, telemetry: Telemetry = None):
        self._store = store
        self._telemetry = telemetry or Telemetry()
        self._positions: dict[str, Position] = {}
        self._trades: list[Trade] = []
        self._cash: float = 0.0
        self._pause_until: Optional[date] = None
        self._consecutive_losses: int = 0
        self._trade_count: int = 0
        self._prev_snap: dict = {}                    # v3: 持有方
        self._today_trades: list[Trade] = []          # 每日临时态

    def load(self) -> None: ...
    def add_position(self, today, code, price, strategy_name) -> Optional[Position]: ...
    def execute_sell(self, pos, exit_price, reason, partial=None, exit_date=None) -> Optional[Trade]: ...
    def get_active(self) -> list[Position]: ...
    def build_live_snapshot(self) -> dict: ...
    def total_equity(self, snapshot) -> float: ...
    def equity_price_coverage(self, snapshot) -> tuple: ...
    def max_buy_amount(self) -> float: ...
    @property
    def positions(self) -> dict[str, Position]: ...
    def replace_positions(self, value: dict) -> None: ...  # v3 HIGH-4
    def is_in_cooldown(self, code, today) -> bool: ...       # v3 CRITICAL-2
    def update_prev_snap(self, snapshot) -> None: ...
    def persist_state(self) -> None: ...                    # sell_phase line 627-630 P1-4 时序
    def persist_positions(self) -> None: ...
    @property
    def cash(self) -> float: ...
    @property
    def trades(self) -> list[Trade]: ...
    @property
    def today_trades(self) -> list[Trade]: ...
```

**Engine 改动**：
- `engine.positions` 改为 property + setter（v3 HIGH-4 修复），委托 portfolio。
- 24 条 function-body import 中，telemetry 相关的 3 条全部删除。
- intraday_monitor 改用 `self.engine._portfolio.replace_positions(...)`（v3 HIGH-4 修复）。

**验收**：
- `tests/test_portfolio_manager.py`：InMemoryStore 注入测试 8 个场景。
- intraday_monitor 测试改用 portfolio setter 路径，不再 `self.engine.positions = {...}`。

**风险**：中（多文件但接口清晰）。

---

### Step 4: 抽 RiskManager（含启动 schema 一致性校验，v3 HIGH-3 修复）

**目标**：包装 exit_rules + 加载 RiskParams + **保留 schema vs config 一致性校验**（v3 修订——不能删 `_validate_params_against_schema`）。

**新建 `app/sim_trader/risk_manager.py`**：
```python
class RiskManager:
    def __init__(self, risk_params: RiskParams,
                 exit_engine: Optional[ExitRuleEngine] = None):
        self._params = risk_params
        self._params_dict = dataclasses.asdict(risk_params)
        self._engine = exit_engine or ExitRuleEngine()
        self._validate_consistency()              # v3 HIGH-3: 保留 schema 校验

    def _validate_consistency(self):
        """对比 schema 与 config.py 常量,差异 > 0.01 告警（保留原 _validate_params_against_schema 行为）"""
        import app.sim_trader.config as _sc
        config_hard_stop = getattr(_sc, 'HARD_STOP', None)
        if config_hard_stop is not None and abs(config_hard_stop - self._params.hard_stop) > 0.01:
            log.warning(f"[RiskManager] hard_stop 不一致: schema={self._params.hard_stop} config={config_hard_stop}")

    def check_stops_eod(self, positions, snapshot, trading_dates, today,
                        prev_snap=None, readonly=False) -> list[tuple]: ...
    def check_stops_intraday(self, pos, current_price, session_peak, daily_atr=0.0) -> Optional[ExitSignal]: ...
```

**Engine 改动**：
- 删除 `engine._validate_params_against_schema`（逻辑搬到 RiskManager._validate_consistency，在 RiskManager.__init__ 时调用）。
- 删除 `from app.backtest.exit_rules` / `from app.config.risk_params` / `import dataclasses` 4 条 function-body import。

**验收**：
- `tests/test_risk_manager.py`：覆盖 5 优先级链 + readonly + intraday session_peak + 启动一致性校验。

**风险**：中（风控核心）。

---

### Step 5: 抽 EquityRecorder（_prev_day_snap 持有 + _BAD 内部化，v3 MED-7 修订）

**目标**：净值 + 校验 + 曲线补齐 + 持有 `_prev_day_snap` + **完全内部化 `_BAD_EQUITY_CURVE_DETECTED`**（v3 修订：不再 re-export）。

**新建 `app/sim_trader/equity_recorder.py`**：
```python
class EquityRecorder:
    def __init__(self, store: SimStore, portfolio: PortfolioManager):
        self._store = store
        self._portfolio = portfolio
        self._prev_day_snap: dict = {}
        self._bad_curve_detected: bool = False      # v3: 内部状态,不再 module-level

    @property
    def bad_curve_detected(self) -> bool:
        """v3 替代 module-level _BAD_EQUITY_CURVE_DETECTED;
        test_sim_trader_store.py 需改为访问 engine._equity.bad_curve_detected"""
        return self._bad_curve_detected

    def load(self) -> None: ...
    def record(self, today, snapshot) -> CycleResult: ...
    def _validate_loaded_state(self, state, curve) -> tuple[bool, str]: ...
    def _fill_missing_snapshots(self, ...): ...
    def update_prev_day_snap(self, snap: dict) -> None: ...
    def persist_prev_day_snap(self) -> None: ...
```

**Engine 改动**：
- 删除 `_BAD_EQUITY_CURVE_DETECTED` module-level 变量。
- 删除 module-level `_stock_name_cache`（搬 telemetry）。
- `engine.record` 委托 equity_recorder。

**v3 修订 MED-7 处理**：
- DoD 7.3 修改：`tests/test_sim_trader_store.py` 中 `eng_mod._BAD_EQUITY_CURVE_DETECTED` 改为 `engine._equity.bad_curve_detected`（**测试需要改这 4-5 行**，与原"一行不改"承诺有冲突，v3 显式接受此改动）。
- engine.py 不再 re-export module-level 变量。

**验收**：
- `tests/test_equity_recorder.py` + 修改 `test_sim_trader_store.py` 的 4-5 行。
- `engine._equity.bad_curve_detected` 与旧 `eng_mod._BAD_EQUITY_CURVE_DETECTED` 语义一致。

**风险**：中（小幅破坏老测试的访问路径，但语义不变）。

---

### Step 6: 🔴 修复 intraday_monitor._check_position + 合并 2 个时间方法（v3 MED-5）

**目标**：NameError bug 修复 + 顺手合并 `_in_trading_hours` 与 `_is_market_hours`（MED-5）。

**改动**：
- `_check_position` line 123-150 重写为委托 RiskManager.check_stops_intraday。
- 合并 `_in_trading_hours`（line 216-220，分钟版）和 `_is_market_hours`（line 69-75，HHMM 版）为单一 `_in_trading_hours(now)` 用统一口径（v3 推荐：分钟版 line 216-220 实现，`_check_and_act` 改用它）。

**验收**：
- `tests/test_intraday_monitor.py`：tick → RiskManager.check_stops_intraday → execute_sell 路径。
- 盘中触发不再 NameError（v3 关键验收）。

**风险**：中。

---

### Step 7: Engine 瘦身 + execute_daily_cycle（v3 HIGH-2 + CRITICAL-2 收口）

**目标**：Engine 只剩调度逻辑，execute_daily_cycle 完整 10 步（v3 HIGH-2）含 SAME_STOCK_COOLDOWN（v3 CRITICAL-2）。

**3 caller 改造**：
- `main.py`：同步预计算信号循环 → 调 `engine.execute_daily_cycle(d, snap, sig_by_date.get(d, []), dates)`（保留信号预计算，删 15 行重复 → 节省 ~5-8 行）
- `cron_jobs.py`：异步包装 + signal_picker 注释同步（v3 "不在范围" 校验警告） + `mode='execute'/'readonly'` 参数
- `api/sim_trader.py`：pre_market 价格分支在 caller 侧算 snapshot（不变），其余调 `execute_daily_cycle`

**v3 修订 HIGH-5**：行数减少预估改为"API -10~15 行、cron -10~15 行、main -5~8 行"（不是 -50/-30/-20）。

**验收**：
- 3 caller 全部改用 execute_daily_cycle。
- `tests/test_engine_dispatch.py`：InMemoryStore 注入测试完整 10 步流程 + readonly 模式。

**风险**：高（涉及 3 caller + Engine API）。分小步提交。

---

### Step 8: 清理 config.py（v3 LOW-3 修订）

**目标**：删除 11 个与 RiskParams 重复的常量，**SAME_STOCK_COOLDOWN 保留**（v3 LOW-3 修订：plan 正文保留列表漏了它）。

**改前 11 个常量删除**：`HARD_STOP` / `TRAIL_ACTIVATE` / `TRAIL_DD` / `TAKE_PROFIT_TIERS` / `TIME_EXIT_DAYS` / `TIME_EXIT_PROFIT` / `TIME_FORCE_DAYS` / `USE_ATR_TRAIL` / `ATR_TRAIL_MULTIPLIER` / `FIRST_DAY_EXIT_MIN_PROFIT` / `FIRST_DAY_EXIT_DAYS`。

**改后保留在 config.py**（完整清单 v3 修订）：`INITIAL_CAPITAL` / `POSITION_SIZE` / `MIN_BUY_AMT` / `LOSS_STREAK_HALVE` / `LOSS_STREAK_PAUSE` / `PAUSE_DAYS` / `SIM_START` / `SIM_END` / `LOAD_START` / `STRATEGY_NAME` / `SIGNAL_PARAMS` / `SELL_TIME` / `BUY_TIME` / `AUTO_SELL` / `AUTO_SCAN` / `AUTO_BUY` / `MONITOR_ENABLED` / `MONITOR_MODE` / **`SAME_STOCK_COOLDOWN`**（v3 修订新增）。

**store.py 改动**：
- `SimTraderStore` 加 `clear_all()` 对齐 JsonSimStore。
- 两个 adapter 加 `: SimStore` 注解。

**验收**：
- grep 11 个常量在 config.py 为 0 次。
- `clear_all()` 调用在 SimTraderStore 不再 AttributeError。

**风险**：低。

---

### Step 9: 收尾测试 + 3 个 ADR（v3 LOW-2 修订）

**目标**：全量测试 + 写 ADR。

**新建 ADR**（v3 LOW-2 修订 3 个）：
- `docs/adr/0001-engine-as-dispatcher.md` — Engine 只做编排
- `docs/adr/0002-simstore-protocol.md` — 3 adapter 接口契约
- `docs/adr/0003-riskmanager-eod-vs-intraday.md` — **`use_high_for_tp` / `skip_eod_only` 双方法的语义差异**（v3 LOW-2）

**验收**：
- DoD 7.1 全部勾选。
- pytest tests/ 100% 通过。

**风险**：低。

---

## 4. 测试策略

### 4.1 新增测试文件（v3 明确清单）

| 测试文件 | 覆盖 | 关键场景 |
|---------|------|---------|
| `tests/test_models.py` | Position / Trade / CycleResult | today_pnl + CycleResult.sell_signals |
| `tests/test_sim_store_protocol.py` | Protocol 一致性 | 3 adapter isinstance(SimStore) |
| `tests/test_in_memory_store.py` | InMemoryStore | 与 JsonSimStore 行为对齐（v3 MED-3 风险） |
| `tests/test_portfolio_manager.py` | PortfolioManager | 8 个场景（add/sell/snap/equity/cooldown/setter） |
| `tests/test_risk_manager.py` | RiskManager | 5 优先级链 + readonly + intraday session_peak + 启动校验 |
| `tests/test_equity_recorder.py` | EquityRecorder | record/partial/跳变/重复日期/_prev_day_snap 持久化 |
| `tests/test_intraday_monitor.py` | IntradayMonitor | tick → RiskManager → sell 路径（**v3 关键：不再 NameError**） |
| `tests/test_engine_dispatch.py` | execute_daily_cycle | 完整 10 步 + readonly + SAME_STOCK_COOLDOWN |

### 4.2 替换策略（replace, don't layer）

**v3 修订**：明确 `test_sim_trader_store.py` 中 `eng_mod._BAD_EQUITY_CURVE_DETECTED` 这 4-5 行**需要改**（改为 `engine._equity.bad_curve_detected`），其余测试断言保持不变。

### 4.3 不破坏现有

- `test_sim_trader_store.py` 必须 100% 通过（除上述 4-5 行访问路径修改）
- `test_live_trader_*` 不受影响
- 手动跑一次 `sim_trader main.py` 验证回放一致

---

## 5. 不在本次范围

| 声明 | v3 校验 |
|------|--------|
| 不改 IntradayMonitor 事件总线 | ✅ PASS |
| 不重写 `_check_and_act` 市场时间判断 | ✅ PASS |
| 不动 live_trader 模块 | ⚠️ WARNING — `signal_picker.py` 注释引用 cron_jobs 信号获取逻辑，需同步更新参考注释 |
| 不重构 RiskParams | ✅ PASS |
| 不重新设计 ExitRuleEngine | ✅ PASS |
| 不动 backtest 模块（scripts/） | ✅ PASS |

---

## 6. 风险与缓解（v3 修订）

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 向后兼容层漏掉某调用方 | 中 | 高 | Step 7 旧方法全部保留为薄包装，grep 全调用点 |
| 风控行为不一致 | 中 | 高 | Step 4 RiskManager 单测覆盖全部 priority chain，再手动跑 sell_phase 对比 |
| 测试覆盖率不够 | 中 | 中 | 8 个测试文件清单 |
| Step 7 大改动引入回归 | 中 | 高 | 分小步提交：Step 3 → 4 → 5 → 6 → 7 |
| 老测试 fail（_BAD 访问路径） | 高 | 中 | v3 显式接受 4-5 行测试修改，语义不变 |
| **InMemoryStore 与 JsonSimStore 偏差**（v3 MED-3） | 中 | 中 | 用 JsonSimStore 跑同一组测试对比 |
| **signal_picker.py 与 cron_jobs 注释不同步**（v3 不在范围） | 低 | 低 | Step 9 同时更新注释 |
| 🔴 intraday_monitor NameError bug 修复引入新行为 | 中 | 高 | Step 6 单独成 step，先写测试再改代码 |
| **execute_daily_cycle 10 步时序错乱**（v3 HIGH-2） | 中 | 高 | P1-4 时序"先落盘现金再清理持仓"必须严格保留 |

---

## 7. 验收标准（DoD，v3 修订）

### 7.1 必须达成

- [ ] `app/sim_trader/engine.py` 减到 ≤ 220 行（v3 从 200 调整为 220，因保留 6 方法 + 7 属性 + 完整 execute_daily_cycle）
- [ ] engine.py function-body import 数：24 → ≤ 5（v3 实测目标）
- [ ] `app/sim_trader/models.py` 新建，含 Position / Trade / **CycleResult（含 sell_signals 字段）**
- [ ] `app/sim_trader/store_protocol.py` 新建，含 `load_equity_curve` 返回格式契约
- [ ] 3 个 adapter（SimTraderStore / JsonSimStore / InMemoryStore）显式实现 Protocol
- [ ] SimTraderStore 加 `clear_all()` 对齐 JsonSimStore
- [ ] `app/sim_trader/telemetry.py` 新建，3 个 helper 集中
- [ ] `app/sim_trader/portfolio.py` 新建，含 positions setter + **SAME_STOCK_COOLDOWN 检查方法**
- [ ] `app/sim_trader/risk_manager.py` 新建，含 **schema 一致性校验**（不删原校验）+ 2 个 check_stops 方法
- [ ] `app/sim_trader/equity_recorder.py` 新建，持有 _prev_day_snap，**完全内部化 `_BAD_EQUITY_CURVE_DETECTED`**
- [ ] 新增 ≥ 8 个测试文件（v3 清单），覆盖率 80%+
- [ ] `tests/test_sim_trader_store.py` 100% 通过（**v3 修订：4-5 行访问路径修改被显式接受**）
- [ ] 全部测试套件通过
- [ ] `engine.execute_daily_cycle(today, snapshot, signals, trading_dates, mode="execute")` 是单一核心入口，**含 sell_phase 全部 10 步 + SAME_STOCK_COOLDOWN**
- [ ] 3 caller 全部改用 execute_daily_cycle，main/api/cron 行数分别节省 ~5-8 / ~10-15 / ~10-15 行（v3 HIGH-5 修订）
- [ ] 🔴 `intraday_monitor._check_position` 不再 NameError（v3 关键验收）

### 7.2 加分项

- [ ] IntradayMonitor 的 `_in_trading_hours` 和 `_is_market_hours` 合并为单一方法（v3 MED-5）
- [ ] `signal_picker.py` 注释与 cron_jobs 信号获取逻辑同步

### 7.3 严禁破坏

- ❌ sim_trader 的所有现有公开 API 必须保留（向后兼容）—— `engine.__init__` 必须保留 `store=None`
- ❌ 现有 trade 数据 / position 数据 / equity 曲线格式不变
- ⚠️ **v3 修订**：`_BAD_EQUITY_CURVE_DETECTED` 访问路径变更被接受（4-5 行测试改动），但**业务语义不变**

---

## 8. 实施计划（v3 维持 8 天工期）

| Day | Step | 交付物 | v3 关键验收 |
|-----|------|--------|-----------|
| D1 | Step 1+2 | models.py + CycleResult + store_protocol.py + InMemoryStore + **24→≤5 import 清理** | engine.py function-body import 数 ≤ 5；全部现有测试通过 |
| D2 | Step 3 | telemetry.py + portfolio.py + PortfolioManager 单测 | cooldown 检查在 portfolio |
| D3 | Step 4 | risk_manager.py + RiskManager 单测 | schema 校验保留 + 5 优先级链全测 |
| D4 | Step 5 | equity_recorder.py + EquityRecorder 单测 + 修改老测试 4-5 行 | _BAD 完全内部化 |
| D5 | Step 6 | intraday_monitor.py 修 NameError + 合并时间方法 | **盘中 tick 不再 NameError** |
| D6 | Step 7 | engine.py 瘦身 + execute_daily_cycle 10 步 + 3 caller 适配 | 含 SAME_STOCK_COOLDOWN + 时序保留 |
| D7 | Step 8 | config.py 11 常量清理 + store.py 加 Protocol 注解 + clear_all() | DoD 7.1 全部勾选 |
| D8 | Step 9 | 全量测试 + 3 个 ADR + signal_picker 注释同步 + buffer | buffer 天 |

**总工期：8 个工作日**（含 1 天 buffer）

---

## 9. ADR 备选（v3 LOW-2 修订 3 个）

- `docs/adr/0001-engine-as-dispatcher.md` — Engine 只做编排不做业务
- `docs/adr/0002-simstore-protocol.md` — 3 adapter 接口契约
- **`docs/adr/0003-riskmanager-eod-vs-intraday.md`** — `use_high_for_tp` / `skip_eod_only` 双方法的语义差异（v3 新增）

---

## 10. v3 审查摘要

### 10.1 v3 审查发现的问题（已全部处理）

| 严重度 | 数量 | v3 处理 |
|--------|------|---------|
| CRITICAL | 2 | **全部修复**（CRITICAL-1: import 计数 9→24；CRITICAL-2: SAME_STOCK_COOLDOWN 收口进 execute_daily_cycle）|
| HIGH | 5 | **全部修复**（HIGH-1: 补全 17+7 公开 API 去向表；HIGH-2: execute_daily_cycle 扩到 10 步；HIGH-3: schema 校验保留；HIGH-4: positions setter；HIGH-5: 行数预估校准） |
| MEDIUM | 7 | **全部处理**（MED-1/2/3/4/5/6/7） |
| LOW | 4 | **全部处理**（LOW-1/2/3/4） |

### 10.2 v2 → v3 主要变更

1. **CRITICAL-1** 🔴：function-body import 从"9 条"修正为"24 条"，附完整函数→行号对照表，DoD 目标从 ≤ 3 调整为 ≤ 5
2. **CRITICAL-2** 🔴：SAME_STOCK_COOLDOWN 冷却检查从"caller 侧分散"收口进 execute_daily_cycle（3 caller 删重复）
3. **HIGH-1**：补全 engine 全部 17+7 公开成员的去向表（之前 v2 只列 5 back-compat）
4. **HIGH-2**：execute_daily_cycle 伪代码从 4 步扩到 10 步（涵盖 sell_phase 全部 P1-4 时序约束）
5. **HIGH-3**：RiskManager.__init__ 保留 schema 一致性校验（不删 `_validate_params_against_schema` 行为）
6. **HIGH-4**：engine.positions 改为 property + setter，委托 portfolio.replace_positions
7. **HIGH-5**：行数预估校准（main -5~8 / api -10~15 / cron -10~15，取代 v2 的 -20/-50/-30）
8. **MED-1**：engine.py ≤ 220 行（v2 是 ≤ 200）
9. **MED-2**：Protocol 加 load_equity_curve 返回格式契约（统一 'pos' 键）
10. **MED-3**：InMemoryStore 行为偏差风险列入风险表
11. **MED-4**：CycleResult 加 `sell_signals: list` 字段
12. **MED-5**：IntradayMonitor 2 个时间方法合并到 Step 6
13. **MED-6**：_prev_snap 与 _prev_day_snap 时序文档化
14. **MED-7**：显式接受 test_sim_trader_store.py 4-5 行访问路径变更
15. **LOW-1**：4 条完全冗余 function-body import 删除清单
16. **LOW-2**：第 3 个 ADR `0003-riskmanager-eod-vs-intraday.md`
17. **LOW-3**：SAME_STOCK_COOLDOWN 加入 config.py 保留清单
18. **LOW-4**：v2 自评下调，v3 自评重新校准

### 10.3 v3 自评

| 维度 | v2 | v3 |
|------|----|----|
| 完整性 | 4.5 / 5 | **4.5 / 5**（v3 补全所有 API 去向表 + 10 步伪代码） |
| 可行性 | 4 / 5 | **4.5 / 5**（数据准确性已修正） |
| 风险识别 | 4.5 / 5 | **5 / 5**（CRITICAL-1/2 + HIGH 全部识别） |
| 可测试性 | 4.5 / 5 | **4.5 / 5** |
| 验收标准 | 4.5 / 5 | **5 / 5**（量化指标全部基于实测） |

**总评**：**PASS** — 18 个审查问题全部处理，2 CRITICAL 已修复。可以开工。