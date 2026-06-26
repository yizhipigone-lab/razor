# 批 3:测试/AI/一致性 Implementation Plan

> **Goal:** 5 个 commit 完成 3 项(pytest / AI 样本外 / 模拟盘对齐)

## Task 1: C3-1 pytest 基础测试

**Files:** `tests/conftest.py`, `tests/test_execution.py`, `tests/test_exit_rules.py`

- [ ] 新建 `tests/conftest.py`(加 sys.path)
- [ ] 新建 `tests/test_execution.py`(6 tests: get_limit_up_pct × 4 + can_buy × 2 + calc_buy_cost + calc_sell_revenue)
- [ ] 新建 `tests/test_exit_rules.py`(4 tests: rule_hard_stop low/close 分支, rule_trailing_stop peak触发, rule_take_profit TP1触发)
- [ ] `pytest tests/ -v` 全部通过
- [ ] Commit: `test: add pytest for execution.py and exit_rules (I1/C3-1)`

## Task 2: C3-2 pytest + pytest.ini

**Files:** `tests/test_trading_calendar.py`, `pytest.ini`

- [ ] 新建 `tests/test_trading_calendar.py`
- [ ] 新建 `pytest.ini`(配置路径和测试发现规则)
- [ ] `pytest tests/ -v` 全部通过
- [ ] git add tests/ pytest.ini; git commit

## Task 3: C3-3 AI 样本外协议

**Files:** `app/backtest/ai_optimizer.py:898-950`, `scripts/test_fix_30.py`

- [ ] 加 train/valid/test 分离逻辑(最近 70/20/10%)
- [ ] Top-10 排序改用 `(valid_score, wfe)`
- [ ] `python scripts/test_fix_30.py` 验证

## Task 4: C3-4 模拟盘参数源对齐

**Files:** `app/sim_trader/engine.py`, `scripts/test_fix_31.py`

- [ ] 启动时加参数断言(用 `load_risk_params()` 对比)
- [ ] `python scripts/test_fix_31.py` 验证

## Task 5: C3-5 CHANGELOG + push

