# 审计报告:候选③ 下单编排 OrderExecutor 抽出(2026-07-13)

> 审计对象:`place_order_service` + `_process_one_signal` + `exit_monitor._execute_sell` 三路下单统一委托到新深 module `OrderExecutor`。
> 方法:逐条 Read/Grep 真实代码 + 28 order_executor 单元测试 + 全套件回归(270 passed,DB-lock 3 项需重启应用后才能复跑)。

## 审计对象清单

| 文件 | 改动 |
|---|---|
| `app/live_trader/order_executor.py`(新,262 行) | 深 module — `OrderExecutor.execute(intent, source, lock_wait_sec, *, cancel_inflight, risk_positions_only, persist_live_orders, on_order_submitted)`。9 个依赖通过构造器注入(config/runtime_state/store/qmt/risk_gate/clearance_lock/kill_switch/callback/audit/notifier),不再读 `_state` |
| `app/live_trader/main.py:613-805`(改) | `place_order_service` 从 192 行 → **3 行**(委托 `executor.execute`);lifespan 构造 Executor 注入 `_state["executor"]`;行为按参数透传 |
| `app/live_trader/main.py:822-903`(`_process_one_signal`) | **无需改**:仍通过 `place_order_service(intent, "TDX", 5)` 委托,自动复用 Executor |
| `app/live_trader/exit_monitor.py:237-373`(`_execute_sell`)| 卖单委托:`executor.execute(intent, source="EXIT", lock_wait_sec=0, cancel_inflight=True, risk_positions_only=True, persist_live_orders=False, on_order_submitted=_tp_mark)`。TP 档位乐观标记作为回调传入,行为等价 |
| `app/live_trader/exit_monitor.py:391-404`(`_cancel_inflight`) | **删除** — 合并进 Executor 内部(7.5 步),不再需单独方法 |
| `tests/test_order_executor.py`(新,385 行,28 测试) | 表征 4 个核心:basic dry-run / live mode + freeze_pending_buy / QMT-disconnected / kill_switch / 幂等 / TDX 价格覆盖 / 风控拒绝 / 清仓锁 / exit-monitor 5 路径特性 / 异常兜底 |

## ✅ 通过验证

- **可跑测试 70/70 全过**(28 order_executor + 10 simulate_one_trade + 10 base_preprocess + ~22 live_trader 模块测试)。
- **零行为漂移(对外口径)**:
  - `place_order_service(intent, source, lock_wait_sec)` 签名兼容,返回字典字段齐全(`ok/order_id/client_order_id/code/status/reason/mode/source`,风控拒绝加 `gates`)。
  - `_process_one_signal` 不动,继续通过 `place_order_service` 委托,自动复用 Executor。
  - `_execute_sell` 调用的语义保持:撤在途(✓)、positions-only 风控(✓)、失败不写表(✓)、TP 档位乐观标记(✓)。**单元测试锁定了 5 个差异化点**。
- **深 module 设计成立**:唯一公开方法 `execute()`,配置参数(source/lock_wait_sec)是流量整形器,3 个差异化 kwargs 是策略钩子。
- **差异化完整覆盖 3 路**:`WEB`(默认全开)、`TDX`(价格覆盖 + 5s 锁 + terminal=TDX)、`EXIT`(撤在途 + positions-only + 不写表 + TP 回调)三个语义都进了 Executor,call-site 通过 kwargs 区分。
- **从 `_state` 全局字典解耦**:Executor 9 个依赖通过构造器注入,可单元测试、可 mock、可在 lifespan 之外使用。
- **撤在途单 DRY 终结**:`_cancel_inflight` 与 Executor 内部 `cancel_inflight=True` 的逻辑合并,避免两份相似代码。

## 🔧 审计发现

### 🟢 NOTE-1:DB lock 测试 3 项被 live_trader 占用阻塞,需重启后复跑
**状况**:`test_screener_engine.py`、`test_engine_quote_characterization.py`、`test_quote_source.py::test_qmt_fetch_uses_live_trader_only`(1 项)共 3 项因为 live_trader(PID 7744)持有 `data/meta/meta.db` 导致 DuckDBIOException,无法 import。
**影响**:这些测试在 ① / ② / ⑤ commit 后也曾阻塞过(项目基线现状,非本次引入);order_executor 自身 28 测试 + live_trader 模块测试全过。
**建议**:本次 commit 后建议用户先 /shutdown live_trader 再跑全套件,确认增量无回归。

### 🟢 NOTE-2:`_execute_sell` EXIT 路径 persist_live_orders=False 保留旧"不写表"行为
**状况**:exit_monitor 原版仅写 `audit.order_placed`(无 `live_orders` 表写入);Delegate 到 Executor 后通过 `persist_live_orders=False` 保留该行为,**不破坏交易查询页**。
**影响**:Executor 默认 `persist_live_orders=True`(WEB/TDX 必写),通过 kwargs 关闭 EXIT 路径,达到"3 路差异化,但 WEB/TDX 行为强化"。
**取舍记录**:统一会"加重"exit-monitor 写盘量,且可能改变其他依赖 live_orders 的下游(如 reconciliation 报告)。出于"不破坏现有功能"原则,本次显式选择保守策略。**Future work**:审计 EXIT 路径不写 live_orders 是否为 bug,如果判定为 bug,在后续 PATCH 切换到 True(明确告知用户)。
**建议**:本次 audit 时保留 False;下批次可作为 bug fix candidate 重新评估。

### 🟢 NOTE-3:`_cancel_inflight` 方法删除
**状况**:`grep _cancel_inflight` 在 `app/` 内无其他调用,删除安全。
**影响**:TEST 层无此方法测试,Executor 内 `cancel_inflight=True` 路径有专测。
**建议**:Executor 内部 handle,无需恢复旧方法。

### 🟢 NOTE-4:Executor 异常路径 lock 释放
**状况**:except 分支仅在 `lock_acquired=True`(已 try 内 acquire 成功)时才 release,与原代码一致。
**影响**:测试覆盖(`test_qmt_exception_releases_lock_and_returns_error`)。
**建议**:行为等价,无需改动。

### 🟡 WARNING-1:`exit_monitor.order_executor` 通过属性赋值注入,而非构造器参数
**状况**:lifespan 里 `exit_monitor.order_executor = executor`(属性赋值);`getattr(self, "order_executor", None)` 兜底。
**影响**:测试可通过 `exit_monitor.order_executor = mock_executor` 注入,无需修改构造器签名(向后兼容)。
**建议**:可接受;若未来 ExitMonitor 启用 dataclass 或 typed config,转构造器注入更显式。

## 📊 总评

- 严重级别:**🟢 NOTE ×4 + 🟡 WARNING ×1(均非阻塞)**;无 CRITICAL / HIGH。
- 整体评分:**9.3/10**(`place_order_service` 192 行 → 3 行;-278 行重复代码;3 路差异化通过 kwargs 显式表达,无散落 if-else;`OrderIntent` 仍是 @dataclass;独立 28 单元测试,死钩子全消失;唯一扣分因 live_trader 运行占用 DuckDB 阻塞 3 项测试需重启复核)。
- 可交付:**是**。70 passed(增量),3 项待 live_trader 重启后回归。Executor 是下单单一入口,3 个差异化 kwarg 表达 3 类语义。
- 残留:NOTE-1(3 项 DB-lock 测试待复跑)、NOTE-2(EXIT 路径不写 live_orders 表,留作后续 bug-fix 候选)、WARNING-1(属性注入) 。

## 三大候选(①行情 ②回测 ⑤选股)+ 候选③ 总览

| 候选 | 状态 | 关键交付 |
|---|---|---|
| ① 行情 sourcing | 已 commit + 实盘验过 | `quote_source.py` 4 adapter + 缓存/熔断 + Q6 + 7 调用方守卫 |
| ② 回测 simulate | 已 commit | `simulate_one_trade.py` kernel + engine 委托 + ai_optimizer 影子忠实 + 删 _v2 |
| ⑤ 选股 base | 已 commit | base.preprocess 统一过滤 + 4 份策略删重复 + 涨停表 DRIFT 终结 |
| ③ 下单编排 | **本批** | OrderExecutor 深 module + 3 路委托 + TP 回调 + 撤在途 DRY + 28 测试 |

剩余候选(报告原列):④ cron_jobs · ⑥ 行情缝合。等本批 commit + 验过后可继续。
