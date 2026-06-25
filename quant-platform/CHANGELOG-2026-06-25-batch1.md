# 2026-06-25 批 1 地基优化 CHANGELOG

> 5 个 commit,4 项地基优化(真相源/.gitignore/AI 目标/沙箱),严格冻结 11 项涉及文件,排除实盘

## 修复的 4 项 P0/P1

| # | 项 | 简述 | Commit |
|---|---|---|---|
| B | 真相源(引擎) | engine.py 删 9 个假默认值,改用 schema.py 唯一加载 | `b06b56a` (amend 965f63b) |
| B | 真相源(settings) | settings.py 8 个 property 删假默认,缺键从 schema 读;backtest.py:262 区域 6 行删假默认;顺带修 time_exit_min_profit_pct sign 反 bug | `d62e334` |
| E | 工程卫生 | 新建 .gitignore,清理 41 个 logs/*.log + server.log + server_stdout.log 入库 | `c2c1abf` |
| C | AI 目标函数 | `_calmar_score` 改真风险调整(mean - 0.5*std);LHS 加 seed 42;WFE 进 best 排序 | `fa9b848` |
| D | 安全沙箱 | 新建 ast_sandbox.py,strategy_coder 加载前 AST 校验,禁导 os/subprocess/eval | `0b1d91c` |

## 监控表

| Commit | 文件改动 | 测试 | 服务 | 备注 |
|---|---|---|---|---|
| b06b56a | schema.py + engine.py | ✅ | ✅ | amend 修架构偏差 |
| d62e334 | settings.py + backtest.py | ✅ | ✅ | 顺带修 sign 反 |
| c2c1abf | .gitignore + 清理 | ✅ | ✅ | 70621 行删除 |
| fa9b848 | ai_optimizer.py | ✅ | ✅ | 真 Calmar |
| 0b1d91c | ast_sandbox.py + strategy_coder.py | ✅ | ✅ | 文件位置实际在 app/agents/ |

## 关键设计决策

### B 真相源架构
- 新建 `app/config/schema.py` 含 `RiskSchema` dataclass + `load_risk_params()`
- `app/sim_trader/config.py` 是**唯一真相源**
- engine.py 和 settings.py 都从 schema 读,**不读 settings.json**(避免假默认)
- `params_override` 仍优先(AI 优化器注入)
- 缺键即 `RuntimeError`,**不静默兜底**(用户铁律)

### C AI 风险调整
- 原 `_calmar_score` 是 `np.mean(pnls)`,名不副实
- 新公式 `mean - 0.5*std(ddof=1)`,Sharpe 简化版
- 系数 0.5 经验值,平衡收益与风险
- LHS 固定 seed=42,结果可复现
- WFE(样本外衰减)进入 best 排序,缺失视为 0

### D 沙箱覆盖
- 黑名单:os/sys/subprocess/shutil/socket/http/urllib/requests/ftplib/smtplib/asyncio
- 禁函数:__import__/eval/exec/compile/open
- 验证失败返回错误注释字符串,**不抛异常**(保持 API 路由契约)
- 文件位置:`app/agents/strategy_coder.py`(spec 写 `app/backtest/`,实际在 `app/agents/`)
- 限制:AST 静态扫描,无法拦截反射式构造,批 1 后可考虑补强

## 验证清单(全部 ✅)

- [x] 所有 test_fix_*.py 通过(test_fix_20/21/22/23/24)
- [x] test_simple_runner.py 回测行为不变(return=27.99%)
- [x] 真 Calmar 风险调整生效(低方差组 4.00 > 高方差组 0.13)
- [x] AI 优化器:固定 seed 可复现
- [x] strategy_coder:恶意 prompt 注入被拒绝
- [x] 0 报错 0 崩溃

## 已知遗留(批 2/3 处理)

- 4 引擎成交执行层未统一(批 2)
- hold_days 口径不一致(批 2)
- event_engine 队列泄漏(批 2)
- DuckDB 连接回收(批 2)
- 净值口径用成本价(批 2)
- pytest 测试体系(批 3)
- AI 样本外协议(批 3)
- 模拟盘参数源对齐(批 3)
- 沙箱补强:静态+动态双层(批 1 后)

## 用户行动

1. 批 1 完成后必须 merge 才能开始批 2(用户原话:冻结 11 项涉及文件)
2. 网络通时 push: `git push origin master`
3. 下批启动:写批 2 spec → plan → 5 commits

## 文档位置

- Spec: `docs/superpowers/specs/2026-06-25-batch1-foundation-spec.md` (`7aecb90`)
- Plan: `docs/superpowers/plans/2026-06-25-batch1-foundation-plan.md` (`915edbc`)
- 复盘: `桌面/OPUS/Quant-Platform-全局复盘报告.md`
