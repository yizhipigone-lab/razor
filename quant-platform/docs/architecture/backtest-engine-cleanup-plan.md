# 任务计划：回测引擎架构清理

## 目标

厘清 simple_runner / tdx_runner / AI优化器内部结构三者边界；删除 ai_optimizer.py 中的 dead engine 实例；更新架构报告。

## 当前阶段
阶段 3（待执行）

---

## 各阶段

### 阶段 1：需求与发现（已完成）
- [x] 确认三个引擎各自的调用路径和用途
- [x] 确认 `backtest_engine` 调用方（有实际使用，非 dead code！）
- [x] 确认 `ai_optimizer.py` 中的 `self._engine = BacktestEngine()` 是 dead code
- [x] 确认 AI 优化器非 TDX 路径实际使用 `_fast_simulate + simulate_one_trade`
- **状态：** complete

### 阶段 2：确认清理范围（已完成）
- [x] 列出所有需要删除/修改的文件清单
- [x] 确认影响范围
- [x] 审计 plan：全部 PASS，无差异
- **状态：** complete

### 阶段 3：删除 ai_optimizer.py 中的 dead engine 实例
- [ ] 删除 `app/backtest/ai_optimizer.py:29` 的 import：`from app.backtest.engine import BacktestEngine`
- [ ] 删除 `app/backtest/ai_optimizer.py:263` 的赋值：`self._engine = BacktestEngine()`
- [ ] 验证删除后 AI 优化器仍可正常 import（语法正确）
- [ ] 记录到 progress.md
- **状态：** pending

### 阶段 4：更新架构报告
- [ ] 更新 HTML 报告中候选③的描述
- [ ] 重新生成报告文件
- [ ] 记录到 progress.md
- **状态：** pending

### 阶段 5：验证与测试
- [ ] 运行 pytest 确认无回归
- [ ] 记录到 progress.md
- **状态：** pending

---

## 关键问题（已全部回答）

| 问题 | 答案 |
|------|------|
| `backtest_engine` 是 dead code？ | 否。被 `run.py backtest` CLI 和 `POST /api/backtest` 实际使用 |
| `POST /api/backtest` 是否还有调用方？ | 是，`run.py backtest` CLI 和 HTTP API 都用 engine.py |
| `self._engine` 是否是 dead code？ | 是。在 `__init__` 里创建，但从不被 `_run_trial` / `_run_trial_tdx` / `_fast_simulate` 访问 |
| engine.py 应该删除？ | 否。有两个实际调用方 |
| 真正应该删除的是什么？ | 仅 `ai_optimizer.py:29` 的 import 和 `ai_optimizer.py:263` 的 `self._engine` 赋值 |

---

## 调用关系图（审计后确认）

```
HTTP 入口
├── POST /api/backtest           → backtest_engine.run()    [engine.py] ✅ 活跃，保留
├── POST /api/backtest/run-simple → simple_runner.run_backtest()  [846L] ✅ 活跃，保留
└── POST /api/backtest/run-simple → tdx_runner.run_tdx_backtest() [999L] ✅ 活跃，保留

run.py
└── python run.py backtest [策略] → backtest_engine.run()        [engine.py] ✅ 活跃，保留

AI 优化器（ai_optimizer.py）
├── from app.backtest.engine import BacktestEngine  ← 待删除（line 29）
├── self._engine = BacktestEngine()                ← 待删除（line 263）
├── _run_trial_tdx() → run_tdx_backtest()        ← tdx_runner（直接 import 函数）
└── _fast_simulate() → simulate_one_trade()       ← 非 TDX 路径
```

---

## 已做决策

| 决策 | 理由 |
|------|------|
| 不删 engine.py | 被 `run.py backtest` CLI 和 `POST /api/backtest` 实际使用 |
| 删除 `ai_optimizer.py:29` 的 import | 该 import 仅服务于 dead code |
| 删除 `ai_optimizer.py:263` 的 `self._engine = BacktestEngine()` | dead code：从不被任何方法访问 |
| 保留 simple_runner | 活跃使用（`POST /api/backtest/run-simple`） |
| 保留 tdx_runner | 活跃使用（`POST /api/backtest/run-simple` + AI TDX 寻参） |
| 架构报告候选③改为"删除 ai_optimizer dead engine 实例" | engine.py 不是问题，只有 `self._engine` 是 dead code |

---

## 审计结果（2026-07-14）

| 验证项 | Plan 声称 | 实际情况 | 结果 |
|--------|----------|----------|------|
| `backtest_engine` 有两个调用方 | run.py CLI + POST /api/backtest | ✅ 确认 | PASS |
| `self._engine` 在 ai_optimizer.py 只创建不读取 | dead code | ✅ 确认 | PASS |
| ai_optimizer.py 导入了 `BacktestEngine` | 是，用于 `self._engine` 实例化 | ✅ 确认 | PASS |
| `__init__.py` 导出 engine 相关符号 | 未导出 | ✅ 确认 | PASS |

---

## 备注
- 2026-07-14：经 Grep + 审计验证，`backtest_engine` 有两个调用方，engine.py 不能删
- 2026-07-14：只有 `ai_optimizer.py:29` 的 import 和 `ai_optimizer.py:263` 的 `self._engine` 是真正的 dead code
