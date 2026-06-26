# 批 3:测试/AI/一致性 Spec

> 日期:2026-06-26
> 项目:quant-platform 全面优化(3 批拆分)
> 批 3 范围:3 项 P1(Pytest / AI 样本外 / 模拟盘对齐)
> 上批成果:批 2 已完成(push e039e64),4 项引擎统一

---

## 0. 上下文

### 批 1 做了什么
- 真相源统一(删 23 个假默认值)、AI 目标改风险调整、AST 沙箱、.gitignore

### 批 2 做了什么
- 统一成交执行层(4 引擎用 execution.py)、修 T+0 卖出 bug、交易日历、event_engine 队列修复、DuckDB 连接回收、净值改市值

### 批 3 要修的
1. **I. pytest 测试体系**:仓库零正式测试,只有 ad-hoc test_fix_*.py 脚本
2. **J. AI 样本外协议**:无 train/valid/test 分离,WFE 不参与选优
3. **K. 模拟盘参数源与回测对齐**:回测读 self.p,模拟盘读 settings,可能不一致

---

## 1. 项 I — pytest 测试体系

**目标**:建立最小 pytest 框架,覆盖核心模块,不追求 80% 覆盖,先跑通门槛。

**改动**:
- 新建 `tests/` 目录 + `tests/conftest.py`
- 新建 `tests/test_execution.py`(覆盖 execution.py 的 4 个核心函数)
- 新建 `tests/test_trading_calendar.py`
- 新建 `tests/test_exit_rules.py`(覆盖 rule_hard_stop / rule_trailing_stop / rule_take_profit)
- 保留 scripts/test_fix_*.py 不打散(历史产物,批 1-2 产出)

**文件清单**:`tests/conftest.py`, `tests/test_execution.py`, `tests/test_trading_calendar.py`, `tests/test_exit_rules.py`

---

## 2. 项 J — AI 样本外协议

**目标**:引入真 train/valid/test 分离,WFE 进入 best_params 排序,防止全样本过拟合。

**当前问题**(已确认):
- `ai_optimizer.py:898/925/950` 全部在同一 `[start, end]` 全样本上跑
- WFO 的 IS/OOS 唯一样本外切分,但结果不参与选优
- Top-10 按全样本 score 排序

**改动**:
- 改 `ai_optimizer.py:898-950` 区域:分 train(最近 70% 日期) / valid(最近 20%) / test(最近 10%),Optuna 只在 train+valid 上跑
- 改 Top-10 排序逻辑:用 `(valid_score, wfe)` 联合排序(不是全样本 score)
- 加 `_compute_wfe(trades, is_trades, oos_trades) -> float` 函数

**文件清单**:改 `app/backtest/ai_optimizer.py:898-950`

---

## 3. 项 K — 模拟盘参数源与回测对齐

**目标**:**回测用 self.p(回测器内部 dict,来自 config)**和**模拟盘读 self.engine.check_stops 里的 sim_params(来自 settings)**现在是两条链。加启动断言确保三套配置一致。

**当前问题**(已确认):
- 回测用 `self.p`(来自 `run_backtest(params")` 或 `run_tdx_backtest(params)`)
- 模拟盘用 `settings.get("risk", ...)` 兜底 `config.py`
- 没有一致性检查,可能回测找到的"最优"参数与模拟盘实际不同

**改动**:
- 改 `app/sim_trader/engine.py`:启动时加模拟盘参数断言:
  ```python
  # K3 修复: 启动时校验模拟盘参数与 schema 一致
  from app.config.schema import load_risk_params
  _sch = load_risk_params()
  assert abs(self._sim_hard_stop - _sch.hard_stop) < 1e-6, f"模拟盘 hard_stop 与 schema 不一致"
  ```
- 改 `app/sim_trader/main.py` 或 `cron_jobs.py` 启动时同样校验
- 改 `app/scheduler/cron_jobs.py` 启动时校验

**文件清单**:改 `app/sim_trader/engine.py`, `app/sim_trader/main.py`, `app/scheduler/cron_jobs.py`

---

## 4. 5 个 commit 划分

```
C3-1: tests/conftest.py + tests/test_execution.py + tests/test_exit_rules.py
C3-2: tests/test_trading_calendar.py + pytest.ini(可选)
C3-3: 改 ai_optimizer.py 样本外协议 + test_fix_30.py
C3-4: 改 sim_trader/engine.py + sim_trader/main.py + cron_jobs.py 参数源对齐 + test_fix_31.py
C3-5: 批 3 CHANGELOG + push
```

---

## 5. 验证清单

- [ ] `pytest tests/` 全部通过
- [ ] scripts/test_fix_*.py 全部通过
- [ ] test_simple_runner.py 通过
- [ ] AI 样本外:new score 不再全样本
- [ ] 模拟盘对齐:启动时参数断言不报错

---

## 6. 状态

待 review。
