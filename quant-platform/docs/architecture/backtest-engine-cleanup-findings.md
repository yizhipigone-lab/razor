# 发现与决策

## 需求

用户要求：
1. 理解三个回测引擎（engine.py / simple_runner / tdx_runner）的真实关系
2. 确认是否应该删除无用 engine，保留 simple_runner 和 tdx_runner
3. 确认 AI 优化器的"内部引擎"应该如何定位

## 研究发现

### 三个引擎的真实调用路径

**engine.py (594行)**
- 模块级单例：`backtest_engine = BacktestEngine()`（line 594）
- 调用方（两个，均活跃）：
  1. `POST /api/backtest`（`app/api/backtest.py:334`）
  2. `python run.py backtest [策略]` CLI（`run.py:65`）
- AI 优化器也 import 了 `BacktestEngine` 并在 `__init__` 里 `new` 了 `self._engine`，但**从未调用**
- `engine._cost_rates`（line 343）：返回 0.125%/0.175% 无 min_commission，是 **dead code**，实际路径不走这里

**simple_runner.py (846行)**
- 主入口：`run_backtest(params, progress_cb, stop_event, stock_names, stock_pool)`
- 调用方：`POST /api/backtest/run-simple`（strategy_type != "tdx" 时）
- 核心：`FastEngine` 类 + `_execute_signal()` 函数
- 成本逻辑：自己调用 `calc_buy_cost` / `calc_sell_revenue`，**独立于 `simulate_one_trade`**

**tdx_runner.py (999行)**
- 主入口：`run_tdx_backtest(params, progress_cb, ...)`（line 47，模块级函数）
- 调用方：
  1. `POST /api/backtest/run-simple`（strategy_type == "tdx" 时）
  2. AI 优化器 `_run_trial_tdx()` 直接 import 函数调用

### AI 优化器非 TDX 路径实际用的什么

```
_run_trial() → _fast_simulate() → simulate_one_trade()
```

- **不是** simple_runner
- **不是** engine.py
- **不是** tdx_runner
- 数据：Phase 1 预加载 minute bars 缓存，自己聚合成日线 OHLC
- 输出：单笔 pnl_pct（用于比较参数好坏，不需要完整 equity curve）
- `self._engine = BacktestEngine()` 在 `__init__` 里 new 了但**从未被访问**

### 关键洞察

| 观点 | 结论 |
|------|------|
| AI 优化器"内部引擎"？ | 不存在。AI 优化器不是回测引擎，是**寻参框架** |
| simple_runner 和 AI 优化器可以合并？ | 不能。simple_runner 每次重加载数据，AI 优化器一次加载 N 次复用；输出也不同 |
| 三个引擎应该合并？ | 不能。数据来源不同，场景不同 |
| engine.py 应该删除？ | ❌ 不能删！`backtest_engine` 有两个活跃调用方（run.py CLI + POST /api/backtest） |
| AI 优化器里的 `self._engine` 应该删除？ | ✅ 是。dead code：只在 `__init__` 里创建，从未被任何方法访问 |

## 审计结果（2026-07-14）

| 验证项 | Plan 声称 | 实际情况 | 结果 |
|--------|----------|----------|------|
| `backtest_engine` 有两个调用方 | run.py CLI + POST /api/backtest | ✅ 确认 | **PASS** |
| `self._engine` 在 ai_optimizer.py 只创建不读取 | dead code | ✅ 确认 | **PASS** |
| ai_optimizer.py 导入了 `BacktestEngine` | 是，用于 `self._engine` 实例化 | ✅ 确认 | **PASS** |
| `__init__.py` 导出 engine 相关符号 | 未导出 | ✅ 确认 | **PASS** |

**无任何差异或风险发现。**

## 技术决策

| 决策 | 理由 |
|------|------|
| 不删 engine.py | `backtest_engine` 被 `run.py backtest` CLI 和 `POST /api/backtest` 实际使用 |
| 删除 `ai_optimizer.py` 中的 `self._engine = BacktestEngine()` | dead code：仅在 `__init__` 创建，从未被 `_run_trial` / `_run_trial_tdx` / `_fast_simulate` 访问 |
| 删除 `ai_optimizer.py` 中的 `from app.backtest.engine import BacktestEngine` | 该 import 仅服务于 dead code |
| 保留 simple_runner | 活跃使用（`POST /api/backtest/run-simple`） |
| 保留 tdx_runner | 活跃使用（`POST /api/backtest/run-simple` + AI TDX 寻参） |
| 保留 engine.py | 有两个活跃调用方，非 dead code |
| 架构报告候选③改为"删除 ai_optimizer dead engine 实例" | engine.py 不是问题，只有 ai_optimizer 里的 `self._engine` 是真正的 dead code |

## 遇到的问题

| 问题 | 解决方案 |
|------|---------|
| 初始误判 engine.py 是死代码 | Grep 验证后发现 run.py CLI + POST /api/backtest 是真实调用方，不能删 |
| 无 | - |

## 资源

- `app/backtest/engine.py` — **保留**，非 dead code
- `app/backtest/ai_optimizer.py:29` — 待删除 `from app.backtest.engine import BacktestEngine`
- `app/backtest/ai_optimizer.py:263` — 待删除 `self._engine = BacktestEngine()`
- `app/api/backtest.py:321-334` — 确认活跃调用方
- `run.py:57-78` — 确认活跃调用方
