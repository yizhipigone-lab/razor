# 🔍 项目成果审计报告：2026-07-15 通宵成果

> 审计对象：[REPORT-工作成果_2026-07-15通宵.md](./REPORT-工作成果_2026-07-15通宵.md)
> 审计人：三角色（架构分析师 / 功能开发分析师 / 高级测试师）
> 审计标准：最苛刻，不讲好话，逐项验证
> 审计方法：Read 真实代码 + git diff + 全量测试运行 + 交叉引用

---

## 审计对象

- **改动范围**：34 files, +3539 / -247 lines，17 个提交（含报告提交），跨 sim_trader / live_trader / backtest / api / config / data_manager
- **原始要求清单**（从报告提取）：
  1. sim_trader 安全子集重构（models.py 抽出 + Protocol + NameError 修复）
  2. sim/live 盘中 TP 口径统一（use_high_for_tp=True）
  3. QMT Tick 订阅计划书 v2 + POC + Step 1 落地
  4. 6 agent 并行全项目质量审计
  5. 修复全部 5 个 CRITICAL
  6. 修复 6 个用户指定的 HIGH
  7. 全量测试 0 回归
  8. 向后兼容 14 调用方零改动（要求 3：不破坏现有功能）

---

## ✅ 要求 vs 实现比对

| 要求 | 实现情况 | 证据 | 结论 |
|------|---------|------|------|
| NameError 崩溃根治 | ✅ 已修复 | [intraday_monitor.py:172](app/sim_trader/intraday_monitor.py#L172) — `build_context()` 替代未定义 `ctx/overall_peak` | ✅ |
| models.py 抽出 + re-export | ✅ 已实现 | [models.py](app/sim_trader/models.py) 106行；[engine.py:20](app/sim_trader/engine.py#L20) re-export | ✅ |
| SimStore Protocol | ✅ 已实现 | [store_protocol.py](app/sim_trader/store_protocol.py) 39行 `@runtime_checkable` | ✅ |
| InMemoryStore | ✅ 已实现 | [in_memory_store.py](app/sim_trader/in_memory_store.py) 159行 | ✅ |
| C1 持仓永不更新修复 | ✅ 已实现 | [store.py:327](app/live_trader/store.py#L327) `UPDATE...RETURNING` position_applied 原子认领 | ✅ |
| C2 废单不膨胀持仓 | ✅ 已实现 | [callback_handler.py:438](app/live_trader/callback_handler.py#L438) `release_pending_buy`; [store.py:428](app/live_trader/store.py#L428) | ✅ |
| C3 audit 注入 | ✅ 已实现 | [callback_handler.py:39](app/live_trader/callback_handler.py#L39) audit 参数; [main.py:115](app/live_trader/main.py#L115) 注入 | ✅ |
| API C1 密钥 masked | ✅ 已实现 | [system.py:86](app/api/system.py#L86) `v[:6] + "****" + v[-4:]` | ✅ |
| Config C1 webhook 移出 git | ✅ git 层面已解决 | `.gitignore` 含 `config/app_setting.json`；example 已脱敏 | ⚠️ **文件本体仍在磁盘含真实 token** |
| H2 成本基修复 | ✅ 已实现 | [engine.py:486](app/sim_trader/engine.py#L486) 用 `remaining_shares` 做分母 | ✅ |
| H4 NameError 修复 | ✅ 已实现 | [sim_trader.py:346](app/api/sim_trader.py#L346) import STRATEGY_NAME | ✅ |
| H8+M1 除权误判 | ✅ 已实现 | [exit_rules.py:449-462](app/backtest/exit_rules.py#L449-L462) 阈值改 -0.11/-0.21/-0.31 + 北证 4xx | ✅ |
| H3 engine 并发锁 | ✅ 已实现 | [engine.py:85](app/sim_trader/engine.py#L85) `_cycle_lock(RLock)` / 5 方法 `@_cycle_locked` | ✅ |
| H6 参数漂移 | ⚠️ 部分完成 | [config.py:22-23](app/sim_trader/config.py#L22-L23) 从 risk_params 派生 | ⚠️ **engine.py 仍用旧路径** |
| H7 TP tiers 双份 | ⚠️ 部分完成 | config.py 从 risk_params 单源读 | ⚠️ 同上 |
| Tick 订阅 Step 1 | ✅ 已实现 | [tick_subscriber.py](app/data_manager/tick_subscriber.py) 184行 + [poc](scripts/poc_subscribe_quote.py) 200行 | ✅ |
| 全项目审计报告 | ✅ 已交付 | [AUDIT-全项目质量审计_2026-07-15.md](docs/AUDIT-全项目质量审计_2026-07-15.md) | ✅ |
| 416 passed, 0 回归 | 数量✅ 但有个 "但书" | `416 passed in 10.13s`（实测） | ⚠️ **33 warnings，含 20 个 DeprecationWarning** |

---

## 🏛️ 架构分析师发现

### HIGH-1：H6/H7 "单源"不彻底 — engine.py 仍走旧 import 路径

**证据**：[engine.py:134](app/sim_trader/engine.py#L134)

```python
from app.config.schema import load_risk_params  # ← DeprecationWarning
```

`_validate_params_against_schema` 方法内部仍然 import 已被标记 deprecated 的 `app.config.schema.load_risk_params`，而正确路径是 `app.config.risk_params.load_risk_params`。

**影响**：每次 sim_trader 引擎初始化都触发 DeprecationWarning，测试跑 416 个用例会看到 20 个此警告。这是代码坏味道信号——H6 宣称"单源对齐"，但 engine.py 这个调用点是漏网之鱼。

**严重级别**：HIGH — 不是逻辑 bug，但 H6/H7 修复不完整，且 DeprecationWarning 泛滥会掩盖真正重要的 warning。

### HIGH-2：app_setting.json webhook 仍在本地磁盘

**证据**：`config/app_setting.json` 第 223 行仍含真实飞书 webhook URL：
```
"feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/0a0f2284-..."
```

**情况**：git track 已排除（`.gitignore` + `git rm --cached`），不会再进远端仓库。Example 模板已脱敏。**但本地文件仍含完整 token**，任何人拿到本机文件系统访问权即可读取。

**报告措辞问题**：报告写"webhook 进 git"作为已修复，但用户该看到的真实情况是"webhook 本地仍在，需飞书后台作废重建"——报告 §9 确实提到了这点，但 §7 质量指标表写"密钥/webhook 泄露 ✅" 是对读者的误导，因为泄露风险**只是从远程变为本地，并非消除**。

**严重级别**：HIGH — 报告误导，且用户可能因误读而跳过作废 webhook 这一步。

### MEDIUM-3：notify.py 有未提交修改，与"未触碰"冲突

**证据**：`git status` 显示 `M app/live_trader/notify.py`，diff 增加了手续费计算和卖出盈亏显示（约 16 行新增）。

报告 §8 声明 "notify.py WIP 未触碰"。技术上这些改动未被提交（不在 16 个 commit 中），但**文件确实被修改了**。如果这些改动的意图是改进通知功能，它们应该被提交或至少被提及。如果它们是意外残留，则是工作区污染。

**严重级别**：MEDIUM — 报告不精确，但不影响 git 历史中的代码质量。

### MEDIUM-4：8 行测试代码被删除（非纯新增）

**证据**：`git diff 51e97bc~1..HEAD -- tests/` 显示 8 行被删除（`^[-]`）。

报告宣称 "0 回归"——如果"回归"指"测试结果从 PASS 变 FAIL"，确实 0 回归。但如果"回归"指"已有测试代码需要修改才能通过新逻辑"，则这 8 行删除表明**并非纯新增测试，部分已有测试被调整了**。

需要确认这 8 行删除是`test_sim_trader_store.py` 中因 store 重构而做的必要断言更新，还是真正修 bug。

**严重级别**：MEDIUM — 不损害功能，但"0 回归"的语义可能被过度简化。

---

## ⚙️ 功能分析师发现

### HIGH-3：engine.py `_validate_params_against_schema` 双源矛盾

**证据**：[engine.py:131-153](app/sim_trader/engine.py#L131-L153)

该方法从三个源读参数：
1. Line 134: `app.config.schema.load_risk_params()`（deprecated）
2. Line 141: `app.sim_trader.config` 的模块级常量
3. Line 147: `core.settings` 的 `[risk]` 段

然后相互比较。H6/H7 修复使 source 2 和 3 对齐了，但 source 1 走的仍是旧 deprecated 路径。三个源相互校验的设计是好的，但如果 deprecated 路径哪天被删除，这个方法就会崩。

**严重级别**：HIGH — 未来 breakage 风险。

### MEDIUM-5：TP 口径统一 — 三处确认一致，但 tdx_runner 用的是独立 runner

**验证结果**：
- `sim_trader/intraday_monitor.py:173` — `use_high_for_tp=True` ✅
- `live_trader/exit_monitor.py:175` — `use_high_for_tp=True` ✅
- `backtest/simple_runner.py:138,406,425` — `use_high_for_tp=True` ✅
- `backtest/tdx_runner.py:479` — `use_high_for_tp=True` ✅
- `backtest/simulate_one_trade.py:186` — `use_high_for_tp=True` ✅

报告说"sim intraday_monitor + live exit_monitor + 回测 simple_runner 三处口径完全一致"，但实际上 tdx_runner 和 simulate_one_trade 也都用了 True，总共 5 处。报告少列了 2 处——三缺二，描述不完整。

**严重级别**：MEDIUM — 不影响正确性，但报告表述不够精确。

### LOW-6：load_risk_params deprecated import 仍被 3 处调用

**DeprecationWarning 来源**（从测试 warning 追踪）：
1. `app/sim_trader/engine.py:134` — `_validate_params_against_schema`
2. `app/backtest/simulate_one_trade.py:80` — 通过 `app.config.schema.load_risk_params`

`app.config.schema.load_risk_params` 内部已委托给 `app.config.risk_params.load_risk_params` 并加 DeprecationWarning，所以功能上正确，但调用的地方该迁移了。

**严重级别**：LOW — 功能正确，技术债务。

---

## 🧪 测试师发现

### 测试数据验证

| 指标 | 报告声明 | 实测 | 判定 |
|------|---------|------|------|
| 全量测试 | 416 | 416 passed | ✅ |
| 失败 | 0 | 0 | ✅ |
| Warnings | 未提及 | **33** | ⚠️ |
| 基线 | 360 | 无法从 git 直接验证 | ⚠️ |
| 新增测试 | +56 | 416-360=56（数学上成立） | ✅ |
| 新测试文件 | — | 6 个新文件 | ✅ |

### MEDIUM-7：33 warnings 未在报告中体现

明细：
- **20 个 DeprecationWarning**（`app.config.schema.load_risk_params` deprecated）— 来自 engine.py 和 simulate_one_trade.py
- **12 个 DeprecationWarning**（event loop / get_event_loop）— 可能是预先存在的
- **1 个 RuntimeWarning**（`coroutine 'ConnectionManager.broadcast' was never awaited`）— 异步使用不当的信号

一个宣称"质量交付"的报告应当记录并解释 warning 的来源和清理计划。33 个 warning 里 20 个是**本次未彻底修完的证据**。

**严重级别**：MEDIUM — 报告隐瞒了不完美。

### MEDIUM-8：测试文件有 8 行删除

`test_sim_trader_store.py` 中部分已有测试被修改（删除了 8 行旧断言逻辑）。这些修改可能是因为 store API 变化导致的必要更新。需要确认这不是"为了让测试通过而改测试"而非修代码。

**严重级别**：MEDIUM — 如果测试被改弱则严重，但大概率是 store Protocol 引入后合理的适配。

---

## 🖱️ 交互响应发现

### LOW-7：env-keys masked 返回格式变化

`get_env_keys` 现在返回 `{"tushare_key": "configured", "deepseek_key": "configured", "masked": {...}}`，旧版直接返回明文。前端依赖此接口的代码是否同步更新？若前端仍尝试读完整 key，现在得到的是 `"configured"` 字符串或掩码。

**严重级别**：LOW — 需要确认前端同步适配。报告未提及。

---

## 🤔 我额外想到的隐患

### 隐患 1：C1 修复依赖 DuckDB 的 RETURNING 子句

`UPDATE ... RETURNING trade_id` 用于幂等认领。如果 DuckDB 版本不兼容或 RETURNING 返回空集的行为在不同版本不同，可能导致静默失败。需要确认测试中 DuckDB 版本与生产一致。

### 隐患 2：TickSubscriber 背压丢弃后无补偿

`QUEUE_MAXSIZE = 10000`，满则丢最旧。在高波动行情下，如果 worker 处理不过来，tick 丢失可能导致盘中风控漏检。Step 2-7 应该在实盘接线前加入"丢 tick 计数 + 告警 + 降级到轮询"机制。

### 隐患 3：InMemoryStore 不持久化

InMemoryStore 设计为测试用，但 engine.py 允许 `store=None` 时走纯内存模式。如果生产环境意外以 `store=None` 启动（如配置错误），整个交易历史将在重启时丢失且无任何告警。应有显式的模式标记和安全检查。

### 隐患 4：event_engine 同步 dispatch 在 Step 1 未解决

TickSubscriber 加了 worker 队列避免阻塞 xtdata 回调，但 `event_engine.emit()` 在 handler 中仍是同步调用。如果某个 handler（如 intraday_monitor）处理慢，会阻塞后续 handler。报告 §4 审计中标记了 "event_engine 阻塞" 为 data+core 的 HIGH，但 Step 1 没有解决这个问题——这是 Step 2-7 的 scope。

### 隐患 5：app_setting.json 本地 webhook 是安全定时炸弹

即使从 git 中移除，任何能读取本地文件系统的人（包括通过 API 下载日志/ls 的其他服务、备份脚本、恶意 npm 包）都能拿到完整的 webhook URL。真正的安全修复需要飞书后台作废 + 新 webhook 存在环境变量中。报告 §9 提到了这点，但 §7 质量指标打 ✅ 容易让读者跳过这个关键步骤。

---

## 📊 总评

### 严重级别分布

| 级别 | 数量 | 涉及 |
|------|------|------|
| CRITICAL | 0 | — |
| HIGH | 3 | H6/H7 迁移不彻底 / webhook 报告误导 / engine.py 双源残留 |
| MEDIUM | 5 | notify.py 未提交 / 33 warnings 未报告 / 8行测试删除 / TP口径描述不完整 / 测试基线不可验证 |
| LOW | 2 | deprecated import 3处残留 / env-keys 前端适配未确认 |

### 整体评分：7/10

**加分项**：
- 代码改动质量整体扎实，核心逻辑修复经得起读代码验证
- 5 个 CRITICAL 修复全部可追溯、有测试保护
- 416 测试 0 失败，向后兼容做得不错（re-export 策略）
- TickSubscriber + POC 设计合理，worker 队列 + 背压 + 健康检查体现了工程素养

**扣分项**：
- 报告存在美化倾向：33 warnings 只字不提、"0 回归"掩盖了测试修改、"未触碰"与事实不符
- H6/H7 "单源"不彻底（3 分），deprecated import 残留是证据
- webhook 安全在报告中给人的安全感大于实际消除的风险
- 缺少负责任的"仍存在的问题"诚实列表（例如 "DeprecationWarning 泛滥"、"本地 webhook 需轮换"放在显眼位置而非藏在 §9）

### 是否可交付：是（有条件）

阻塞项：无 CRITICAL。
建议在交付前修复：
1. HIGH-1：engine.py:134 改 `from app.config.risk_params import load_risk_params`
2. 更新报告 §7：诚实标注 webhook "本地文件仍含 token，需飞书后台作废"
3. 决定 notify.py 修改是提交还是 revert

---

## 🔧 建议修复项（按优先级）

1. **[HIGH]** 修复 engine.py:134 的 deprecated import → 用 `app.config.risk_params.load_risk_params`，消除 20 个 DeprecationWarning
2. **[HIGH]** 更新报告 §7 质量指标表，webhook 行改为 "git 层面排除 ✅ | 本地文件仍含真实 token ⚠️"
3. **[MEDIUM]** 决定 notify.py 未提交修改的去向（commit 或 revert）
4. **[MEDIUM]** 显式列出 33 warnings 的来源和清理计划
5. **[MEDIUM]** 确认 8 行测试删除是否改变了测试语义（而非合理的适配）
6. **[LOW]** 检查前端 env-keys 接口是否适配新的 masked 返回格式
7. **[LOW]** backlog：规划 engine.py `_validate_params_against_schema` 的统一重构（三步归一）
