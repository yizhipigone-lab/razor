# 2026-06-26 批 2 引擎统一 CHANGELOG

> 5 个 commit,4 项引擎统一,严格冻结 11 项涉及文件,排除实盘

## 修复的 4 项 P1

| # | 项 | 简述 | Commit |
|---|---|---|---|
| A | 成交执行层(engine) | 新建 execution.py 统一涨停/T+1/成本,engine.py 用 can_buy 替代硬编码 9.9% | `9de7730` (amend) |
| A | simple/strict runner | 用 execution.py,simple 扣成本(之前零成本), strict 修双滑点 | `b7e1905` (amend) |
| A | tdx_runner | 修 T+0 卖出 bug + 涨停买入过滤 | `6cb1f51` |
| G | 交易日历 | 新建 TradingCalendar, tdx 改用交易日(替代日历天 .days) | `1fb3cc5` |
| F+H | event_engine + DuckDB + 净值 | 删假异步队列,threading.local 连接回收,净值用市值 | `e7f12ca` |

## 关键设计

### A 成交执行层
- `app/backtest/execution.py` 含: `can_buy`(涨停过滤,按板块 10/20/30%), `can_sell_today`(T+1 约束), `calc_buy_cost`(佣金+滑点), `calc_sell_revenue`(佣金+印花+滑点)
- 4 引擎都引用执行

### F event_engine 修复
- 删除 `_queue.append` 只进不出的内存泄漏
- 改为直接同步广播

### F DuckDB 连接回收
- `threading.local()` 替代 `dict[thread.ident]`
- `atexit` 进程退出优雅清理

### H 净值改市值
- engine.py 组合模式: 持仓段用 close * shares(不用 invested_capital)
- tdx_runner 终值: 用末根 K 线 close(不用 entry_price)
- simple_runner 已在之前用 close 算净值

## 验证清单(全部 ✅)

- [x] test_simple_runner.py 通过(0 报错 0 崩溃)
- [x] test_fix_25/26/27/28/29.py 全部通过
- [x] 0 报错 0 崩溃(用户硬约束)

## 已知遗留(批 3)

- pytest 测试体系
- AI 样本外协议
- 模拟盘参数源对齐
