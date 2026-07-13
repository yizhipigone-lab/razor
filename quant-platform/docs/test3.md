# 量化平台实盘交易模块实施开发书 v4.0

## 第 1 章 背景与目标

p9 量化平台已经具备完整的能力栈：
- 回测（app/backtest）
- 模拟盘（app/sim_trader）
- AI 优化
- 通达信桥接选股
- 数据同步

缺的最后一块：把模拟盘验证过的策略，真实下单到券商柜台。

### 1.1 业务背景

p9 量化平台已经具备完整的能力栈，下文详细列出各模块的状态和职责。

| 模块 | 状态 | 关键文件 |
|---|---|---|
| 回测 | 完整 | app/backtest/engine.py |
| 模拟盘 | 完整 | app/sim_trader/engine.py |
| AI 优化 | 完整 | app/backtest/ai_optimizer.py |
| 通达信桥接 | 完整 | app/screener/strategies/ |
| 数据同步 | 完整 | app/data_manager/ |
| 行情代理 | 部分 | app/trader/gateways/qmt.py |
| 同花顺代理 | 部分 | app/trader/gateways/ths.py |

### 1.2 投入目标

| 目标 | 度量 |
|---|---|
| 资金安全 | 实盘期间 0 笔非用户预期的真实委托 |
| 行为可解释 | 每一笔真实单都能在 5 分钟内回放 |
| 异常可恢复 | 任意单点故障都能在 60 秒内恢复 |
| 策略可对比 | 实盘与模拟盘并行跑同一策略 |
| 一键可停 | 任何时刻都能 5 秒内切断真实下单 |
| 投产可演练 | 上线前必须经过 1-2 周 dry-run |

## 第 2 章 现状分析

### 2.1 MQ 项目分析

MQ 是一个生产环境运行中的实盘交易系统。

#### MQ 的 11 个关键设计

1. 熔断器 + WaitingFreeWriter 兜底
2. 股票级清理锁
3. Callback 推 HTTP 到中台
4. buildSimpleCycles 交易闭环 + 加权均价
5. 5 条独立止损/止盈规则 + 优先级链
6. TEST/RUN 双模式
7. 11 状态码字典
8. 持仓天数跨源查询
9. prepareSellAction 三步流程
10. 多源持仓数据
11. NSSM 守护 Windows 服务

#### MQ 的关键设计缺口

- 单连接 = 单账户
- 无风控前置
- 无订单超时监控
- Callback HTTP POST 同步线程
- callback 失败就丢
- on_disconnected 仅日志
- on_account_status 是 pass
- callback 用 HTTP POST 不带签名
- 单 xt_trader 全局状态
- 资源池熔断粗暴
- QMT 调用无超时
- 无 dry-run 灰度

## 第 3 章 关键决策记录

| # | 决策 | 选项 | 决议 |
|---|---|---|---|
| D1 | 开发书版本 | A 独立 / B 增量 / C 空白 | A |
| D2 | 进程架构 | A 单 / B 双 / C 事件总线 | A |
| D3 | 多账户 | A 单 / B 同实例 / C 多进程 | A |
| D4 | 资金量级 | A 1-2万 / B 5-10万 / C 50万+ | A + B 配置档 |
| D5 | 5 规则 | A 全 / B 4 条 / C 不 | C |
| D6 | 可观测性 | A 基础 / B 全链路 / C 三件套+Web | C |
| D7 | 影子模式 | A 影子 / B 回放 / C 对账 / D dry-run | D + C |
| D8 | 模拟实盘 | A 并行 / B 停 / C 二选一 | A |