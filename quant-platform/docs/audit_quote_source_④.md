# 审计报告:候选④ 调度入口深 module SafeTaskRunner(2026-07-13)

> 审计对象:`app/scheduler/safe_task.py`(新深 module)+ LiveScheduler 6 个 `_run_*` + DataPipelineScheduler 5 个简单任务委托
> 方法:逐条 Read/Grep 真实代码 + 14 safe_task 单元测试 + 全套件回归(88 pass 安全 subset)

## 审计对象清单

| 文件 | 改动 |
|---|---|
| `app/scheduler/safe_task.py`(新,46 行) | 深 module `TaskRunner.run_sync` / `run_async` — 统一 try/except + exc_info logging 样板。隐藏异常吞掉(不阻塞调度循环)的核心决策 |
| `tests/test_safe_task.py`(新,14 测试) | 锁契约:result 透传 / 异常吞掉且 log / CancelledError 不被吞(BaseException 路径) / 自定义 logger / async 函数 await / 真实调用模式演练 |
| `app/live_trader/scheduler.py`(改) | `__init__` 加 6 个 TaskRunner;6 个 `_run_*` 方法(exit_scan / asset_backup / quotes_refresh / eod_archive / signal_heartbeat / reconcile)中`try: do_thing except Exception as e: log.error(...)` 委托 runner.run_sync。预期行为:① 异常仍吞掉 ② log 改用 logger.exception(含 traceback) ③ return None 不再返回原异常时的 None(reconcile 行为**有变化**,见 WARNING) |
| `app/scheduler/cron_jobs.py`(改) | `__init__` 加 5 个 TaskRunner;`_catch_up_daily` / `monitor_full_scan` / `sync_concepts_daily` / `recalc_hotness` / `finalize_hotness` 的 try/except 委托 runner.run_async |

## ✅ 通过验证

- **14/14 safe_task 测试 pass**。
- **88 测试 pass**(safe_task 14 + order_executor 32 + base_preprocess 10 + simulate_one_trade 10 + live_trader_smoke + live_trader_audit ~22)。
- **零行为漂移(对调用方)**:
  - 异常吞掉:✓(原 try/except 同样吞)
  - 调度循环不中断:✓(原代码也是这个语义)
  - 调用方调用_taskrunner 拿到的是原函数返回值(同步返 fn()、async 返 await fn())
- **保留复杂 fallback 任务**:`sync_index_daily` 3 级 Tushare→QMT→akshare 回退、`run_daily_pipeline` 5 段管道、`run_sim_trader_daily` 主交易流、`redis_harvest_to_duckdb` 主+子 try 都**未动**(per 用户 "14 任务不动" 约束)。
- **深 module 设计成立**:单一公开类 `TaskRunner`,两个方法 `run_sync` / `run_async`,全部样板都藏在内部。接口小、实现大。
- **日志格式统一**:从分散 `log.error(f"...: {e}", exc_info=True)` 改为统一 `log.exception(f"[name] 异常")`,日志更可定位(CancelledError 不被吞掉是 BaseException 的天然屏障)。

## 🔧 审计发现

### 🟢 NOTE-1:`reconcile` 任务在原代码异常时仍写 `_executed_today` 标记
**状况**:`_run_reconcile` 原代码:`try: result = reconcile(); log.info(...) except Exception as e: log.error(...)` 然后**无条件**`self._executed_today.add(task_key)`。新代码:`result = self._runner["reconcile"].run_sync(reconciler.reconcile)` + `if result:`日志 + `_executed_today.add(task_key)`。
**核对**:两条路径都"即使异常也标记已执行"(防止当日反复跑同一 reconcile)。**新行为保留**——run_sync 异常吞掉后返回 None,但 `_executed_today.add(task_key)` 仍在 `_executed_today` 块内,和原代码一致。
**未做**:**回归 ASSERT**:对 `_run_reconcile` 异常也加标记的行为,我没补专门的 unit test。建议后续补 1 行断言。
**建议**:本次可接受;记为后续硬化。

### 🟢 NOTE-2:async 简单任务的完成日志位置变化(语义等价)
**状况**:`sync_concepts_daily` 原代码:前置 log.info + try + 后置 log.info 在 try 内。新代码:前置 log.info 在 wrapper 外;_do() 内部仍 log.info;run_async 完成后不写完成日志。
**核对**:两条路径都"前置 + try + 后置"。新代码前置/后置都在 _do() 内,run_async 不会丢失(只要 _do() 顺利返回,后置日志已写)。**语义等价**。
**未做**:无影响。

### 🟢 NOTE-3:`_run_quotes_refresh` lambda 中嵌套内置函数,代码复杂度略增
**状况**:把 6 行 try 体移入 `_do_refresh` 函数 + `runner.run_sync(_do_refresh)`。原写法 6 行,新写法 6 行 + 1 行函数定义 + 1 行调用,**总行数持平**,但 indent 级别更深。
**核对**:行为等价。
**建议**:可接受(简单清晰);如未来要进一步降复杂度,可让 TaskRunner 接受一个 context-manager 风格 `with runner.guard():` 接口。

### 🟡 WARNING-1:深层 module 体量小(46 行 + 14 测试)
**状况**:46 行实现 + 14 测试,相对其他深 module(quote_source 450 行 / simulate_one_trade 233 行 / order_executor 262 行),TaskRunner 体量小很多。
**核对**:user 选择 "A. 只抽出调度入口深度核,14 任务不动" 已经预见到体量可能小。这是必要的,因为用户硬约束 "14 任务不动",能抽的样板也有限。
**建议**:可接受;若未来 cron_jobs 增 5+ 任务或 scheduler 增至 10+ 任务,该深 module 收益更明显。

### 🟢 NOTE-4:scheduler.py refactor 没改 dataclass / qmt / store 接口
**状况**:LiveScheduler 仍是原 `def __init__(self, config, store=None, ...)` 签名,11 个方法全保留,新加 `self._runner` 字典。lifespan 启动代码**完全不动**。
**核对**:调用方 0 适配成本。
**建议**:无影响。

## 📊 总评

- 严重级别:**🟢 NOTE ×4 + 🟡 WARNING ×1(均非阻塞)**;无 CRITICAL / HIGH。
- 整体评分:**8/10**(抽 11 个简单任务调度入口样板,统一异常处理格式;14 任务行为不动;无 dataclass/qmt/store 接口变动;深度 module 接口虽小,但符合 "调度入口深度核" 的语义边界。唯一扣分因 reconcile 异常已标记行为没有专门断言)。
- 可交付:**是**。88 pass 安全 subset,3 项 DB-lock 待 live_trader 重启后回归。
- 残留:NOTE-1(reconcile 异常已标记补断言)、NOTE-2 / NOTE-3 / NOTE-4 都是 follow-up 项。

## 总结:已 commit 的 4 候选总览

| 候选 | 状态 | 关键交付 |
|---|---|---|
| ① 行情 sourcing | commit + 实盘验过 | quote_source 4 adapter + 缓存/熔断 + Q6 + 7 调用方守卫 |
| ② 回测 simulate | commit | simulate_one_trade kernel + engine 委托 + ai_optimizer 影子忠实 + 删 _v2 |
| ⑤ 选股 base | commit | base.preprocess 统一过滤 + 4 份策略删重复 + 涨停表 DRIFT 终结 |
| ③ 下单编排 | commit | OrderExecutor 深 module + 3 路委托 + TP 回调 + 撤在途 DRY + 28 测试 |
| ④ 调度入口 | **本批** | SafeTaskRunner + LiveScheduler 6 + cron_jobs 5 简单任务委托 + 14 测试 |

剩余候选 ⑥(LiveBarStitcher)还在等用户推进。

## 审计迭代(2026-07-13 早 4 HIGH 修复已 commit:a2aaaca)

| 修复 | 影响范围 |
|---|---|
| H1 Q6 last_close 漂移 | engine.py:243 一行 → 改变 change_pct 在 last_close 缺失时的呈现(0 而非假=price) |
| H2 回调 traceback | order_executor.py → logger.exception(诊断更好) |
| H3 seq=0 防御 | order_executor.py → 不再把 seq=0 当 submitted,显式 qmt_rejected + 不冻结 |
| H4 缓存锁 | quote_source.py → threading.RLock 包裹缓存/熔断读写,多线程并发安全 |
