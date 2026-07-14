# ADR-001: 风控参数单位约定

> 决策日期: 2026-07-15
> 状态: Accepted (强制执行)
> 适用范围: 所有风控参数 (risk_params.py + settings.py + exit_rules.py + simulate_one_trade.py)

## 背景

2026-07-15 全项目审计发现 CRITICAL 量纲 bug:`first_day_exit_min_profit` 漏 `/100.0`,保本止损 breakeven 字段缺失,导致所有风控参数在 settings → engine 路径上单位混乱。

审计同时发现 `_pct()` 函数用 `abs(v) > 1` 魔法判定单位 — 0.5(0.5%) 会被当成 50%,2.5(2.5%) 会被正确转 0.025。这是定时炸弹。

## 决策

### 1. 唯一真相源: `app.config.risk_params.load_risk_params()`

所有风控参数**必须**通过 `load_risk_params()` 读取。该函数返回 `RiskParams` frozen dataclass,**所有百分比字段已统一转小数** (0.03 表示 3%)。

### 2. 三层单位约定

| 层 | 单位 | 例子 | 责任人 |
|---|---|---|---|
| `config/app_setting.json` | 百分比数 (整数或浮点) | `"hard_stop_loss_pct": -4.6` 表示 -4.6% | 配置维护者 |
| `RiskParams` dataclass | 小数 | `hard_stop = -0.046` 表示 -4.6% | 引擎/调用方 |
| `settings.py` property | 百分比数 (与 settings.json 一致) | `settings.hard_stop_loss_pct = -4.6` | 前端/配置面板 |

**禁止跨层**: settings.json 的 4.6 → RiskParams 的 0.046,中间**只走 `load_risk_params()` 一条路**,不走 `settings.xxx * 0.01`。

### 3. `_pct()` 废弃

`app/backtest/exit_rules.py:_pct()` 自 2026-07-15 起标 `[DEPRECATED]`。新代码禁止使用。

替代方案:
- 风控参数走 `RiskParams` (小数)
- `params_override` 传参约定也是小数 (`-0.05` 不是 -5.0)
- 旧调用方迁移见 `docs/DEPRECATION-TIMELINE.md`

### 4. ctx_params 单位

`build_context()` 接收的 dict 单位**必须是小数** (与 `RuleContext` dataclass 默认值一致)。

模拟盘 `engine.py` 和回测 `simulate_one_trade.py` 在构造 ctx_params 时**直接传小数**,不走 `_pct()` 二次转换。

## 后果

### 正面

- CRITICAL 量纲 bug 根治 (C1/C2 修复)
- 7/10 key 不匹配的循环依赖 fallback 删除
- 8 处 property 单位约定有 ADR 记录,后人能查

### 负面

- 老调用方传 `params_override={"hard_stop_loss_pct": -5.0}` (意图 -5%) 必须改为 `-0.05`
- 测试用 `_PARAMS` 必须用小数
- `schema.py` 兼容层保留到 2026-08-15,届时强制删除

## 验证

- `tests/test_risk_params.py::TestRiskParamsCriticalBugRepro`: 强制 settings 给百分比数,断言 RiskParams 转小数正确
- `tests/test_risk_params.py::TestRiskParamsRealSettings`: 真实 settings.json 量纲对齐
- 440 passed, 0 回归 (2026-07-15 验证)

## 相关变更

- `app/config/risk_params.py:60-66` — 加 `/100.0`
- `app/config/schema.py` — 改为委派兼容层
- `app/backtest/exit_rules.py:19-37` — `_pct()` 标 DEPRECATED
- `app/backtest/simulate_one_trade.py:95-103,168-179` — `_p()` 和 ctx_params 直接传小数
- `tests/test_simulate_one_trade.py` — `_PARAMS` 改小数

## 未来工作

- 2026-08-15 删除 `schema.py` 兼容层
- 2026-08-30 删除 `_pct()` 函数
- 引入 Pydantic 类型系统强制单位标注