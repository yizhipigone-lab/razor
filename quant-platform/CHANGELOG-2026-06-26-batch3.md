# CHANGELOG 批 3 — 测试/AI/一致性修复

Date: 2026-06-26

## Commits

| # | Hash | Message |
|---|------|---------|
| C3-1 | `df94342` | test: add pytest for execution.py, exit_rules.py (19 tests) (I1/C3-1) |
| C3-2 | `b5c96e4` | test: add TradingCalendar unit tests (J2/C3-2) |
| C3-3 | `61975c5` | fix(ai): add train/valid/test split + valid_score in best selection (J3/C3-3) |
| C3-4 | `1646c2d` | fix(sim_trader): start-up param validation against schema (K4/C3-4) |

## 修复详情

### C3-1: pytest 测试覆盖 (I1)
- 新增 `tests/test_execution.py` — 11 个测试：涨停幅计算、买入限制、T+1 卖出、交易成本
- 新增 `tests/test_exit_rules.py` — 8 个测试：硬止损、移动止损、阶梯止盈
- 总计 19 个 pytest，全部通过

### C3-2: TradingCalendar 测试 (J2)
- 新增 `tests/test_trading_calendar.py` — 2 个测试
- `test_trading_calendar_basic`: 交易日计数（区间内/首尾/区间外）
- `test_is_trading_day`: 交易日判断

### C3-3: AI 样本外协议 (J3)
- `app/backtest/ai_optimizer.py`:
  - Phase 3-4 之间新增 train/valid/test 日期分离（70%/20%/10%）
  - 探索结果和贝叶斯结果均补齐 `valid_score` + `test_score`
  - Top-10 排序改用 `valid_score` 替代全样本 `score`，防止 IS 过拟合
- 测试: `scripts/test_fix_30.py` — 验证代码包含 train_end 和 valid_score

### C3-4: 模拟盘参数源对齐 (K4)
- `app/sim_trader/engine.py`:
  - `SimTraderEngine.__init__` 末尾新增 `_validate_params_against_schema()`
  - 启动时自动对比 `config.py`/`settings.json` 与 `RiskSchema` 的一致性
  - 不一致时输出 WARNING 日志
- 测试: `scripts/test_fix_31.py` — 验证代码引用了 `load_risk_params`/`RiskSchema`

## 测试结果

```
tests/test_execution.py ........... 11 passed
tests/test_exit_rules.py ........   8 passed
tests/test_trading_calendar.py ..   2 passed
scripts/test_fix_30.py              L30 passed
scripts/test_fix_31.py              L31 passed
─────────────────────────────────────
Total: 21 pytest + 2 script = 23 tests, 全部通过
```
