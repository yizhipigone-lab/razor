# 实盘交易控制面板 设计文档 v2

- 日期: 2026-07-10
- 状态: 已审计修订，进入实施
- 分支: fix/sim-trader-data-pollution-20260701
- v2 修订依据: 三视角审计（架构落地/完整性一致性/安全资损），见 §9 修订日志

---

## 1. 背景与问题

实盘交易(live_trader)界面仅 200 行前端。用户要求参照模拟盘"交易控制"补强，并做深度排查。

经代码核实 + 三视角审计，发现问题分两层：**表层**（v1 已诊断）与**穿层**（v2 审计补出，致命）。

| # | 问题 | 严重度 | 出处 |
|---|---|---|---|
| 1 | exit_monitor 构造 RuleContext 抛 TypeError 被 except 吞，每只持仓 continue 跳过 | 🔴 | exit_monitor.py:139-153 |
| 2 | **signal 字段取值全错**：取 trigger_type/sell_pct/priority/note，但 ExitSignal 只有 reason/sell_price/sell_ratio → TP1 该卖20%变全仓卖 | 🔴 v2 | exit_monitor.py:67-69 |
| 3 | **triggered_tiers 用 sell_count 推断且 sell_count 从不自增** → 修后 TP1 每60s重复触发清仓 | 🔴 v2 | exit_monitor.py:146-147 |
| 4 | **callback upsert_position 覆盖 peak_price 成0** → 移动止盈永久不激活 | 🔴 v2 | callback_handler.py:335 |
| 5 | **live_equity 表从未被写入** → 净值曲线交付空图 | 🔴 v2 | store.py:135 无 INSERT |
| 6 | **peak_price 字段已存在**，v1 误诊为"需加字段" | 🔴 v2 | store.py:83 已有 |
| 7 | **config.mode 被12处硬读**，v1 方案自相矛盾 | 🔴 v2 | main.py 8处+exit_monitor:253+callback 2处 |
| 8 | 实盘未读止盈止损参数，用 exit_rules 默认值（TP1 空列表=不触发） | 🔴 | exit_monitor 未接 config |
| 9 | 四个开关里选股/实盘转发在实盘无对应物 | 🟡 | 信号100%来自主服务POST |
| 10 | dry-run 不可运行时切换 | 🟡 | config.py:24 frozen |

---

## 2. 决策清单（v2 含审计修订）

| # | 决策项 | 选择 | 修订 |
|---|---|---|---|
| 1 | exit_monitor 修复优先级 | P0 先修，界面后做 | |
| 2 | 止盈止损参数源 | 共用模拟盘 risk 段 | |
| 3 | exit_monitor 修法 | 复用 exit_rule_engine.build_context | |
| 4 | 执行开关 | 买入+卖出两个独立开关 | |
| 5 | 模式开关 | 运行时 dry-run↔live 切换 | |
| 6 | 净值曲线 | 做 | v2: 新建采样管线（非"数据现成"）|
| 7 | 风控参数不写死 | 做 | |
| 8 | 手动下单/撤单 | 不做 | |
| 9 | high/low 用当日真实值 | **HS 用当日真实 low；TP 保持 last 对齐模拟盘** | v2 修订（U1）|
| 10 | GBK 乱码 | 做 | |
| 11 | **signal 字段映射** | trigger=reason, sell_pct=sell_ratio×100 | v2 新增（A2）|
| 12 | **triggered_tiers 持久化** | live_positions 加 tp_triggered TEXT 字段 | v2 新增（A3）|
| 13 | **peak_price 维护** | 补 refresh_quotes 更新逻辑（非加字段）+ callback 用 GREATEST 保留 | v2 修订（A1/A4）|
| 14 | **peak 历史兜底** | `GREATEST(avg_cost, last_price)` | v2 修订（F2）|
| 15 | **首扫熔断** | 修后首次扫描强制只扫描不执行 + 单次扫描卖出上限≤3只 + 闸门5a对sell生效 | v2 新增（F1）|
| 16 | **参数变更缓冲** | 30s 生效 + sanity check + 审计 | v2 新增（F3）|
| 17 | **config.mode** | 移出方案：归 RuntimeState，config 保持 frozen，12处改 is_live()/is_dry_run() | v2 写死（A6）|
| 18 | **buy_enabled** | 复用 buy_signal_enabled，移到 RuntimeState 改名 | v2 修订（A9）|
| 19 | **交易日历** | 抽到 app/utils/trading_calendar.py | v2 新增（A10）|
| 20 | **高危接口鉴权** | /live/config/* 写接口 + kill-switch 强制 token | v2 新增（A8）|
| 21 | **清仓重置** | volume=0 时重置 peak/sell_count/tp_triggered | v2 新增（F4）|
| 22 | **pnl mode 过滤** | pnl_engine.build_cycles 强制过滤 mode='live' | v2 新增（F8）|

---

## 3. 设计

### 3.1 P0：exit_monitor 修复（含 v2 连带工作）

**核心修改**：用 `exit_rule_engine.build_context(pos_obj, bar, hold_days, params_dict)` 替换手撸 RuleContext 构造。删除 `import inspect` 调试残留。

**v2 连带工作 1：signal 字段映射修正**（A2）
[exit_monitor.py:67-69](app/live_trader/exit_monitor.py#L67-L69) 改为：
- `trigger = signal.reason`
- `sell_pct = signal.sell_ratio * 100`（如 sell_ratio=0.2 → sell_pct=20）
- `priority` 从 reason 映射（HS/TP/TR/TF/TC 等已知优先级）或省略
- 删除对不存在的 trigger_type/sell_pct/priority/note 的 hasattr/getattr

**v2 连带工作 2：triggered_tiers 持久化**（A3）
- live_positions 表新增 `tp_triggered TEXT`（JSON 序列化的 set，如 `["0","1"]`）。
- build_context 的 pos_obj 提供 `tp_triggered` 属性，从 DB 读出反序列化。
- TP 档位成交后，callback_handler 显式 `UPDATE live_positions SET tp_triggered = ?` 追加该档。
- **不用 sell_count 推断**（sell_count 语义是累计卖出次数，含 HS/TR/TF 全卖，不等价 TP 档位）。

**v2 连带工作 3：peak_price 维护**（A1/A4）
- **不加字段**（store.py:83 已有 peak_price DOUBLE）。
- refresh_quotes 的 UPDATE SQL 加 `peak_price = GREATEST(COALESCE(peak_price, 0), ?)`。
- callback_handler upsert_position 的 UPDATE 子句对 peak_price 用 `GREATEST(live_positions.peak_price, excluded.peak_price)`（防覆盖成0）。
- 历史 peak_price=0 的行回填：`UPDATE live_positions SET peak_price = GREATEST(avg_cost, last_price) WHERE peak_price IS NULL OR peak_price = 0`。

**v2 连带工作 4：清仓重置**（F4）
- callback_handler 卖出成交路径补 volume 递减。
- volume 归零时：`peak_price=0, sell_count=0, tp_triggered='[]'` 重置，防再买同票用旧值。

**v2 连带工作 5：bar high/low**（U1/决策9修订）
- HS 用当日真实 low（`_execute_sell` 已能拿 today_low，[exit_monitor.py:183](app/live_trader/exit_monitor.py#L183)，传入 _build_context）。
- TP/TR 保持用 last_price（对齐模拟盘）。
- QMT quotes 取今日 high/low。

**连带工作 6：hold_days 交易日历**（A10）
- 新建 `app/utils/trading_calendar.py`，把 `_load_trading_calendar` 从 app/api/sim_trader.py 抽离（无 fastapi/pandas 依赖）。
- 模拟盘和实盘都从 utils 导入。
- baostock 失败时 fallback 语义：返回空 set 则 hold_days=1（首日），**需文档明示此风险**；实盘侧若 entry_date 缺失则用今日填充。

**pos_obj 属性清单**（v2 修正，M3）
按 build_context 源码（exit_rules.py:381-431）实际读取：`entry_price`、`peak_price`、`shares`、`remaining_shares`、`tp_triggered`(set)。**不读** triggered_tiers、entry_date。

### 3.2 止盈止损参数接入 risk 段（含 v2 修订）

- exit_monitor 内 `_cfg(key, default)` 照搬 intraday_monitor.py:132-138，优先 `settings.get("risk", key)`。
- params_dict **完整字段**（v2 修正 M2）：hard_stop/take_profit_tiers/trail_activate/trail_dd/time_exit_days/time_exit_profit/time_force_days/first_day_exit_min_profit/first_day_exit_days/breakeven_threshold/breakeven_stop/use_atr_trail/atr_trail_multiplier。**显式声明是否含 breakeven**（模拟盘 intraday_monitor 漏传 breakeven 导致静默禁用，实盘决定是否补齐）。
- 新增 `GET /live/config/risk-params` 返回 risk 段当前值。

**v2 参数变更缓冲**（F3）：
- risk 段参数变更后 30s 生效缓冲（exit_monitor 缓存参数 30s）。
- sanity check：hard_stop 变幅>50% 拒绝或要求 kill_switch 确认。
- 参数变更写 audit（param_changed 事件）。
- 参数变更后下一次扫描强制"只扫描不执行"预览。

### 3.3 运行时开关机制（含 v2 修订）

**存储**（v2 修订 M5/M6）：
- RuntimeState 单例，含 `buy_enabled`、`sell_enabled`、`mode`。
- 持久化到 `app_setting.json` 的 `live_trader.runtime` 段。
- **统一 threading.Lock**，开关 PUT 与 mode POST 共用。
- mode 走 DB（live_runtime 表或复用 live_killswitch 表模式）+ settings + 内存，可靠性对齐 kill_switch。

**v2 修订（A9）**：buy_enabled 复用现有 `buy_signal_enabled`，移到 RuntimeState 改名。config.py:80 的 buy_signal_enabled 字段废弃（保留 fallback 读取）。检查顺序调整：token → kill_switch → buy_enabled → cutoff（[main.py:837](app/live_trader/main.py#L837) 检查挪到 852 之后），与优先级链对齐。

**生效点**：
- 买入开关 off：buy_signal 拒收信号 + audit + 告警。
- 卖出开关 off：exit_monitor 扫描但不执行 + 记告警"本该卖 X 股未执行"。
- **v2 升级机制**（M4）：卖出开关 off 期间连续 3 轮有未执行卖出 → 自动重开 sell_enabled 或激活 kill_switch + 强通知。

**v2 鉴权**（A8）：所有 `/live/config/*` 写接口 + `/live/kill-switch/*` 强制 token（不复用 `_verify_token` 的"未配置则放行"兜底）。

**后端 API**：`GET/PUT /live/config/switches`、`GET/POST /live/config/mode`、`GET /live/config/risk-params`。

### 3.4 运行时模式切换（含 v2 修订）

**v2 config.mode 方案写死**（A6）：
- mode 从 config.py frozen dataclass **移出**，归 RuntimeState。
- config.py 保持 frozen，保留 `initial_mode` 字段做启动日志。
- RuntimeState 提供 `is_live()` / `is_dry_run()` 方法。
- **12 处 config.mode 读取点全清单**（逐一改调 is_live()/is_dry_run()）：main.py:81/308/387/658/697/706/713/720、exit_monitor.py:253/268、callback_handler.py:57/284、config.py:149-150。
- **启动单源**（F6/H3）：启动时 RuntimeState.mode 优先；若 live_trader.runtime.mode 不存在则用 config.initial_mode 种子；切 mode 时同步回写 live_trader.runtime.mode（废弃 live_trader.mode 字段）。

**v2 终态确认机制**（A7/H1）：
- live→dry-run：撤所有在途真实委托 → **轮询** `store.get_inflight_orders()` 每 500ms 一次、上限 30s → 全部终态(54/56)才切；超时未回报 → **阻断切换 + 告警 + 不切**（mode 保持 live）。
- 撤单失败 = 阻断切换 + 激活 kill_switch。
- 切换中持 threading.Lock 防并发。
- **WAL 预写**（额外隐患1）：切换走预写日志（撤单请求→撤单确认→mode切换）原子提交；崩溃重启检测"切换中"未完成 → 强制 reconcile + 激活 kill_switch。

**dry-run→live**：
- 清 mock 残留单：`DELETE FROM live_orders/live_deals/live_cycles WHERE mode='dry-run'`（**不调 qmt.cancel_order**，mock 单无真实 order_id）。
- pnl_engine.build_cycles 强制过滤 mode='live'（F8）。
- 前置检查 QMT 已连接 + 资产备份成功（M4），未就绪阻断。

**通用约束**：kill_switch 激活时禁止切 live；前端悲观更新（收到后端确认才改显示，M3）；切换记 audit（mode_switched 事件，含 old/new_mode + 在途单快照）。

### 3.5 前端展示（含 v2 修订）

| 功能 | 数据源 | v2 修订 |
|---|---|---|
| 净值曲线 | live_equity 表 | **v2 新建采样管线**：scheduler 加定时任务（1min）调 build_live_snapshot() 写 live_equity（A5）|
| 成交记录表 | GET /live/deals | |
| 风控参数 | GET /live/config/risk-params | |
| 止盈止损参数 | 同上 | 显示 risk 段真实值 |
| 执行开关 UI | GET/PUT /live/config/switches | 买入+卖出 |
| 模式 UI | GET/POST /live/config/mode | 悲观更新 + 二次确认 |
| **盘中监控(状态+模式)** | exit_monitor 状态 + 卖出开关 | **v2 显式映射**（F5）：卖出开关 off = 仅告警模式（close）；exit_monitor 常开无独立开关 |
| 乱码修复 | index.html:992/1307/1312/1319 | 两处 config.py 文案同步改 |

**文案修正**：止盈止损参数区改"参数来自 risk 段，实盘与模拟盘共用（改此参数影响实盘真钱）"。两处行号：index.html:992、:1319。

### 3.6 首扫熔断（v2 新增，F1）

- exit_monitor 修复后**首次 scan_once 强制只扫描不执行**，输出"本该触发"清单，人工确认后才开启执行（标志位 `first_scan_dryrun=True`，启动时置位，确认后清除）。
- 单次扫描卖出数量上限 ≤3 只。
- 闸门 5a（日亏熔断）对 sell 生效（当前只在 is_buy 块内，[risk_gate.py:80](app/live_trader/risk_gate.py#L80)）。
- 日卖出金额上限熔断。

---

## 4. 不做项（v2 修正矛盾）

- 手动下单 / 撤单入口
- 一键清仓 / Kill Switch 增强（后续）
- TP/TR 用当日真实 high（HS 用真实 low，决策9修订）
- 选股开关 / 实盘转发开关
- **独立的仅告警运行模式**（dry-run 已覆盖不下单场景；**卖出开关 off 提供卖出侧的仅告警**——v2 修正 M1 矛盾）
- backtest 段统一（本次只统一实盘+intraday 读 risk 段，sim_trader/config.py 读 backtest 段留待后续，F7 降低口径）

---

## 5. 风险与安全（v2 补充）

| 风险 | 缓解 |
|---|---|
| 模式切换 live→dry-run 撤单失败 | 阻断 + 激活 kill_switch（§3.4）|
| 模式切换中途崩溃 | WAL 预写 + 重启检测强制 reconcile（§3.4）|
| 参数共用 risk 段误改触发实盘 | 30s 缓冲 + sanity + 审计 + 预览（§3.2）|
| peak_price 迁移丢失历史峰值 | GREATEST(avg_cost, last_price) 兜底（§3.1）|
| callback 覆盖 peak_price | UPDATE 用 GREATEST（§3.1）|
| **首扫集中抛售** | 首扫强制 dry-run + 卖出上限 + 闸门5a对sell（§3.6）|
| **dry-run 成交污染 pnl** | pnl_engine 过滤 mode='live'（§3.4）|
| **TP1 历史空列表补卖** | 首扫预览暴露 + 人工确认（§3.6）|
| **切换超时前端显示不一致** | 前端悲观更新（§3.4）|
| 向后兼容 | exit_monitor 改动不动模拟盘；RuntimeState 默认值与现状一致 |

---

## 6. 实施顺序（v2 细化）

**P0 致命修复（先做，恢复止盈止损）：**
1. exit_monitor.py：build_context 替换 + signal 映射 + 删调试代码
2. store.py：refresh_quotes 加 peak_price 更新 + live_positions 加 tp_triggered 字段 + 回填 + 清仓重置 SQL
3. callback_handler.py：卖出成交更新 volume/peak/tp_triggered + 清仓重置 + peak 用 GREATEST
4. app/utils/trading_calendar.py：新建抽离
5. exit_monitor.py：_cfg 从 risk 段读参数 + bar HS 用真实 low
6. 首扫熔断标志位 + 闸门5a对sell

**P1 参数 + 开关 + 模式：**
7. GET /live/config/risk-params + 参数缓冲
8. RuntimeState（buy/sell/mode）+ 12处 config.mode 迁移 + is_live()/is_dry_run()
9. GET/PUT /live/config/switches + 鉴权
10. GET/POST /live/config/mode + 终态确认 + WAL

**P2 前端：**
11. 净值采样管线（scheduler 写 live_equity）
12. 前端：净值曲线、成交表、参数、开关、模式、盘中监控映射、乱码修复

**P0 完成即恢复止盈止损（即使 P1/P2 未做）。**

---

## 7. 验证清单（v2 补 8 项）

- [ ] exit_monitor dry-run 下 scan_once 不再跳过持仓，能产生卖出信号
- [ ] **TP1 触发时卖出股数 = can_use × 0.2 取整，非全仓**（A2）
- [ ] **TP1 触发后 tp_triggered 持久化，下次扫描不重复触发**（A3）
- [ ] **peak_price 随行情单调递增，callback 成交不清零**（A1/A4）
- [ ] **持仓清仓后 peak/sell_count/tp_triggered 重置**（F4）
- [ ] **首扫强制只扫描不执行**（F1）
- [ ] 参数：前端显示值 = risk 段值
- [ ] hold_days 跨周末不多算；entry_date 缺失用今日填充
- [ ] 买入开关 off：信号被拒 + audit
- [ ] 卖出开关 off：扫描不执行 + 连续3轮升级（M4）
- [ ] 模式 live→dry-run：撤在途单等终态，超时阻断
- [ ] kill_switch 激活时禁止切 live
- [ ] **并发切换有锁**（M5）
- [ ] **切完重启状态恢复正确**（F6）
- [ ] **pnl_engine 过滤 mode='live'，dry-run 不污染**（F8）
- [ ] **高危接口强制 token**（A8）
- [ ] **live_equity 有数据写入**（A5）
- [ ] 模拟盘未受影响：intraday_monitor 行为不变

---

## 8. 开放项确认结果

1. 模式切换超时：阻断 + 告警
2. 卖出开关 off：扫描但不执行
3. peak_price 迁移：GREATEST(avg_cost, last_price) 兜底（v2 修订，非纯 avg_cost）

---

## 9. v2 修订日志（对照审计编号）

| 审计编号 | 严重度 | v2 修订 |
|---|---|---|
| A1 | 致命 | peak_price 改"补更新逻辑"非"加字段"（§3.1 连带3）|
| A2 | 致命 | signal 字段映射 trigger=reason/sell_pct=sell_ratio×100（§3.1 连带1）|
| A3 | 致命 | triggered_tiers 持久化 tp_triggered 字段（§3.1 连带2）|
| A4 | 致命 | callback 用 GREATEST 保留 peak_price（§3.1 连带3）|
| A5 | 致命 | live_equity 新建采样管线（§3.5）|
| A6 | 致命 | config.mode 移出方案写死 + 12处迁移清单（§3.4）|
| A7 | 高 | 终态确认轮询机制（§3.4）|
| A8 | 高 | 高危接口强制 token（§3.3）|
| A9 | 高 | buy_enabled 复用 buy_signal_enabled（§3.3）|
| A10 | 高 | 交易日历抽到 utils（§3.1 连带6）|
| F1 | 致命 | 首扫熔断（§3.6）|
| F2 | 致命 | peak 兜底改 GREATEST(avg_cost, last_price)（§3.1 连带3）|
| F3 | 致命 | 参数变更缓冲（§3.2）|
| F4 | 致命 | 清仓重置（§3.1 连带4）|
| F5 | 高 | 盘中监控维度映射（§3.5）|
| F6 | 高 | mode 重启单源（§3.4）|
| F7 | 高 | backtest 段统一降级为后续（§4）|
| F8 | 高 | pnl 过滤 mode + dry-run 残留按 mode 清理（§3.4）|
| M1 | 中 | §4 仅告警矛盾修正（§4）|
| M2 | 中 | params 完整字段 + breakeven 声明（§3.2）|
| M3 | 中 | pos_obj 属性清单修正（§3.1）|
| M4 | 中 | 卖出开关 off 升级机制（§3.3）|
| M5 | 中 | RuntimeState 统一锁（§3.3）|
| M6 | 中 | mode 走 DB 可靠性对齐（§3.3）|
| U1 | 中 | HS 用当日真实 low（决策9修订）|
| T1 | 中 | 验证清单补8项（§7）|
| T3 | 中 | audit 补 switch/mode/param 事件（§3.3/3.4）|
| T4 | 中 | entry_date 缺失用今日填充（§3.1 连带6）|
