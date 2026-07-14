# 系统审计报告 — quant-platform

> 生成日期：2026-07-09
> 口径：代码健康度 + 产品业务价值 双视角，全系统扫描
> 方法：6 个独立研究 agent 分模块并行审查，grep 验证调用关系，再综合

---

## 一句话总结

系统**功能完整、风控组件齐全**，但**死代码和重复逻辑偏多**，且有 **3 个高危 bug**（其中一个会让实时行情每 8 分钟才推一次）、**1 处文档与实现脱节**。做减法的空间很大，且收益清晰。

---

## 一、系统全貌

| 模块 | 核心文件 | 状态 |
|---|---|---|
| 回测 | `app/backtest/`（engine/simple/tdx/strict 四引擎） | 3 引擎在用，strict 整文件死代码 |
| 模拟盘 | `app/sim_trader/` | 运行态健康，CLI 回放路径有 bug |
| 实盘 | `app/live_trader/`（14 组件） | 全部接通，但符号级死代码多 + 1 高危 bug |
| 选股 | `app/screener/` | 主力健康，ma5 三变体重复 |
| 策略工厂 | `app/strategy_factory/` | V2 在用，V1 备份冗余 |
| AI 投研 | `app/agents/committee.py` | 在用，import 即编译图有副作用 |
| 数据管理 | `app/data_manager/` | 核心健康，2 个死函数 + 文档脱节 |
| 热点板块 | `app/hot_sector/` | 全链路接通 ✅ |
| TDX 桥接 | `app/tqsdk/` | 活跃 ✅ |
| API 层 | `app/api/`（16 router ~95 端点） | 约 20 个孤立端点无人调 |
| WebSocket | `server/` | 双 WS 重复 + 1 高危单位 bug |
| 前端 | `static/js/`（main.js 268KB） | 双 WS 重复 DOM 写入 |
| 入口/脚本 | `main.py` + `start.bat` + `scripts/`(143 文件) | scripts/ 混乱，3 个废弃入口 |

---

## 二、优势（先说好的）

1. **功能闭环完整**：从数据采集 → 选股 → 策略工厂 → 回测 → 模拟盘 → 实盘 → AI 投研，全链路打通，不是半成品拼凑。
2. **行情三层回退设计**（QMT → 腾讯 HTTP → Parquet）在 `qmt_gateway` 层真正实现，是净值可信度的根基。
3. **实盘风控组件齐全**：risk_gate / kill_switch / reconciler / exit_monitor / audit / clearance_lock 分工清晰，物理隔离 sim/live 状态库。
4. **状态文件有护栏**：`state.json` 唯一真相源 + 灌数隔离区 + 加载期一致性校验（防回测污染），这是金融系统该有的严谨。
5. **配置统一**：`app_setting.json` 14 段集中管理，没有散落的硬编码默认值乱象（除少数历史遗留）。
6. **调度完善**：`cron_jobs.py` 8 类定时任务全部接通真实函数，无空跑。
7. **热点板块全链路打通**：concept_sync → heat_calculator → engine → screener 评分注入，是少有的"无死链"模块。

---

## 三、劣势与问题

### 🔴 高危 bug（建议先修，按影响排序）

| # | 问题 | 位置 | 后果 |
|---|---|---|---|
| H1 | **广播间隔单位错误**（已亲自核验）：`asyncio.get_event_loop().time()` 返回**秒**，`now - last >= 500` 实际每 **500 秒（~8 分钟）**才推一次；注释"500ms"是错的 | `server/market/broadcaster.py:17,34-35` | 实时行情几乎不刷新（首次立即推，之后每 8 分钟一次）。用户可能一直没察觉 |
| H2 | **`_db_lock_if_needed` 未定义**（已亲自核验）：全 `app/` 仅 1 处调用、0 处定义。order_id 重映射（本地 seq → QMT 真实 id）时触发 → AttributeError | `app/live_trader/callback_handler.py:221` | 实盘下单回报中 order_id 重映射分支必崩 |
| H3 | **`save_equity_point` 签名不匹配**：DuckDB 版缺 `source` 参数，engine 调用时传了 `source='...'` → TypeError | `app/sim_trader/store.py:159` vs `engine.py:295,684` | 模拟盘 CLI 回放路径必崩（运行态走 JsonSimStore 不受影响） |

### 🟡 重复逻辑（维护时易漏改）

| # | 问题 | 位置 |
|---|---|---|
| R1 | ma5_angle **三变体**逻辑高度重叠但实现不一致（北交所过滤/涨停阈值各写各的） | `app/screener/strategies/ma5_angle.py` / `ma5_angle_tdx_v2.py` / `ma5_angle_cross.py` |
| R2 | **双 WebSocket**：main.js 自建一条 + websocket.js 模块化一条，同一份行情被两套代码各写一次 DOM | `static/js/main.js:6,2723` + `static/js/websocket.js:117` |
| R3 | **双回测入口**：`/api/backtest`（engine）与 `/api/backtest/run-simple`（simple）参数模型不同，前端两处触发 | `app/api/backtest.py:344` vs `:768` |
| R4 | **风控参数字典三处复制**（hard_stop/take_profit_tiers/trail_*/time_exit_*），改一处漏两处 | `sim_trader/engine.py:488` / `intraday_monitor.py:153` / `live_trader/exit_monitor.py:140` |
| R5 | **双停止脚本**：`stop.bat` 与 `stop_services.py` 都杀 8888/8001，能力交错 | `stop.bat` / `stop_services.py` |
| R6 | `/api/market/sectors` 与 `/api/hot/sectors` 同一数据两个端点 | `market.py:247` / `hot_sector.py:15` |
| R7 | server/market 与 app/api/market 行情获取**两条独立路径**，字段拼装各写一份 | `server/market/quotes.py` vs `app/api/market.py:232` |

### 🟠 设计/文档问题

| # | 问题 | 位置 |
|---|---|---|
| D1 | **文档与实现脱节**：MEMORY 记"TDX 对齐 VERA trailing_first"，但 tdx_runner 用单信号 `check()`，未用多信号 `check_all()` | `app/backtest/tdx_runner.py:206,478` |
| D2 | **CLAUDE.md 承诺 engine.get_realtime_quote 是三层统一封装，实际缺 Parquet 第三层**（只有 QMT→TDX→腾讯） | `app/data_manager/engine.py:211-341` |
| D3 | committee.py **import 即编译 LangGraph 图**，依赖缺失会阻断整个 agents API 链路 | `app/agents/committee.py:298` |
| D4 | engine.py 的 `use_kelly_sizing`/`use_regime_filter`/`use_vol_adaptive` 参数**从未被任何调用方传 True**，分支走不到 | `app/backtest/engine.py:76-78` |
| D5 | **熔断器设计了但不生效**：`connection_manager.call()` 含熔断逻辑，但 qmt_wrapper 各方法自带超时、不走该入口 | `app/live_trader/connection_manager.py:204` |
| D6 | **回测与选股策略接通路径不同**：选股走 DB 表（工厂新建策略立即可用），回测走 simple_runner 硬编码映射表（新策略无法回测） | `app/backtest/simple_runner.py:643` |
| D7 | **WS 订阅只增不减**：自选股变更后订阅集只膨胀，且 broadcaster 群发所有 code（per-client 订阅未生效） | `server/market/broadcaster.py:79` |
| D8 | `main.js` 268KB 巨石文件，难维护 | `static/js/main.js` |

---

## 四、做减法清单

### ✅ A 类：确定可删（高置信，零调用，已 grep 验证）

| # | 对象 | 位置 | 证据 |
|---|---|---|---|
| A1 | `strict_runner.py` 整文件（310 行） | `app/backtest/strict_runner.py` | `run_strict`/`StrictEngine` 全仓零调用 |
| A2 | `translator_v1_bak.py` | `app/strategy_factory/translator_v1_bak.py` | 全仓零 import，V2 已替代 |
| A3 | `qmt_proxy_server.py`（507 行） | 根目录 | 文件头自述废弃，零 import，start.bat 不启动 |
| A4 | `trigger_proxy.py`（3 行一次性脚本） | 根目录 | 全仓零引用 |
| A5 | `_launch_api.bat` | 根目录 | start.bat 的纯子集 |
| A6 | `error-handler.js` 整文件 | `static/js/error-handler.js` | 零 import，index.html 未加载 |
| A7 | `formatCode` 死函数 | `static/js/utils.js:41` | 零调用 |
| A8 | `atomic_write_parquet` / `cleanup_tmp_parquet` | `app/backtest/exit_rules.py:457,465` | 零调用，且放错文件 |
| A9 | `daily_report` 空函数（函数体仅 pass） | `app/sim_trader/sim_trader_report.py:21` | 零调用 |
| A10 | `update_sectors_from_baostock` | `app/data_manager/engine.py:193` | 零调用，行业信息已由 tushare_sync 负责 |
| A11 | `get_all_market_quotes` | `app/data_manager/engine.py:343` | 零调用，全市场快照已由 screener 直接调 gateway 实现 |
| A12 | Trend Radar 空占位注释 | `app/api/agents.py:235` | 注释后无代码 |
| A13 | `python-socketio` 依赖 | `requirements.txt` | 项目代码零引用（用的是 websockets） |
| A14 | live_trader 死符号：9 个 schemas model + 4 个未 raise 的异常类 + `Notifier.qmt_disconnected` + `QmtWrapper.query_trades` + `Reconciler.cold_start_grace` + `AuditLogger.signal/order_filled` + `PnlEngine.get_avg_cost` + `ClearanceLock` 多个方法 + 多处死 import | `app/live_trader/` 各文件 | 全包零调用（详见各文件） |
| A15 | scripts/ 一次性脚本：~28 个 `test_fix_*.py` + 8 个 `fix_*.js` + `rerun_*` | `scripts/` | 文件名带编号/日期，无被引用迹象，建议移 `scripts/archive/` |
| A16 | 约 20 个孤立 API endpoint（前端 0 引用） | 见下表 | grep 验证前后端均无调用 |

孤立 API endpoint 清单（A16）：

| Endpoint | 位置 |
|---|---|
| `GET /api/stocks` | `market.py:82` |
| `GET /api/meta/stocks/name/{code}` | `market.py:137` |
| `POST /api/sync/config` | `data_sync.py:42` |
| `POST /api/sync/redis_harvest` | `data_sync.py:64` |
| `GET /api/redis/status` | `data_sync.py:76` |
| `POST /api/data/sync_stocks` | `data_sync.py:205` |
| `GET /api/logs/dates` | `system.py:239` |
| `GET /api/logs/query` | `system.py:250` |
| `GET /api/backtest/strategies` | `backtest.py:19`（前端用 /api/factory/strategies 代替） |
| `POST /api/agents/generate_strategy` | `agents.py:225` |
| `GET /api/hot/sector/{name}/stocks` | `hot_sector.py:36` |
| `GET /api/hot/concept/{name}/stocks` | `hot_sector.py:50` |
| `GET /api/hot/stocks/batch` | `hot_sector.py:77` |
| `POST /api/hot/sync_concepts` | `hot_sector.py:102` |
| `GET/POST /api/sim-trader/monitor` | `sim_trader.py:699,715` |
| `POST /api/backtest/run-simple/stop` | `backtest.py:934`（疑似被 /api/tasks/stop 替代） |
| `POST /api/tqsdk/backtest` | `tqsdk.py:296` |
| `POST /api/tqsdk/backtest/stop` | `tqsdk.py:413` |
| `POST /api/internal/quotes_push` | `market.py:216`（唯一调用者是已废弃的 qmt_proxy_server） |
| `GET /health`（8888 端口） | `system.py:16`（健康检查端点，常被反代/外部监控调用，**删前务必确认无外部依赖**，建议归 B 类） |

### 🟡 B 类：需你拍板再删（有保留理由的可能）

| # | 对象 | 不确定点 |
|---|---|---|
| B1 | `ma5_angle_cross.py` | 无 BaseStrategy 子类、与另两版重复，仅 simple_runner:647 引用。"MA金叉"策略是否还在用？ |
| B2 | `ma5_angle_tdx_v2.py` | 是"严格匹配通达信"的 A/B 对比版，还是已选定原版为主力可合并？ |
| B3 | `SimTraderStore`(DuckDB 整类) | 是未来替代 JsonSimStore 的迁移目标（有意保留），还是可删？保留则需修 H3 签名 bug |
| B4 | `sim_trader/main.py` CLI 回放 | 还在用吗？用则修 H3，不用则删（replay 脚本已接管） |
| B5 | engine.py 的 kelly/regime/vol_adaptive 三个参数 | 有计划接通（保留），还是废弃（删分支）？ |
| B6 | `connection_manager.call` 熔断器 | 计划让 qmt_wrapper 走该入口接通，还是删掉死熔断逻辑？ |
| B7 | `stop.bat` vs `stop_services.py` | 哪个是正式停止入口？合并还是二选一？ |
| B8 | `/api/backtest` vs `/api/backtest/run-simple` | 长期保留双引擎，还是统一到 simple？ |
| B9 | `sync_index_members` | 要不要进 cron？当前成分股只靠手动触发，长期不更新影响指数成分过滤 |
| B10 | scripts/ 常用工具 | `run_backtest.py`/`populate_sim_trader.py`/`update_index_data.py`/`run_user_bt.py` 等常用脚本需保留，哪些可归档？ |
| B11 | `app_setting.json` 的 `cron.sync_time=17:00` | 与实际用的 `sync_times=[08:30,17:30]` 并存，是否遗留未清理字段？ |

---

## 五、改善方向

### 短期（1-2 天，立竿见影）
1. **修 H1 广播单位 bug**——一行改动（`>= self.broadcast_interval/1000`），实时行情立刻恢复。**优先级最高**，因为这可能掩盖了系统"实时性"的全部问题。
2. **修 H2 `_db_lock_if_needed`**——确认是漏定义还是漏合并，改用 `self.store._db_lock` 之类。实盘每笔订单都走这里。
3. **修 H3 签名**——给 `SimTraderStore.save_equity_point` 补 `source` 参数（或决定删 CLI 路径）。
4. **删 A 类死代码**——纯减法，零风险，立刻让代码库清爽一大截。

### 中期（1-2 周，结构性改善）
5. **合并双 WebSocket**（R2）——二选一，消除重复 DOM 写入和双连接资源浪费。
6. **抽 `build_risk_params` 共享函数**（R4）——风控参数字典三处复制收敛为一处，防漏改。
7. **ma5 三变体合并**（R1+B1+B2）——确认主力后，其余归档或合并到 base.py 公共逻辑。
8. **committee.py 改懒加载**（D3）——import 时不编译图，按需创建。
9. **回测策略加载改读 DB**（D6）——让工厂新建策略立即可回测，与选股路径统一。
10. **scripts/ 整理**（A15+B10）——一次性脚本移 `scripts/archive/`，常用脚本保留并补 README。

### 长期（架构演进）
11. **统一行情获取路径**（R7+D2）——server/market 与 app/api/market 合并为一条，并补齐 engine.get_realtime_quote 的 Parquet 第三层，让实现匹配 CLAUDE.md 承诺。
12. **拆分 main.js**（D8）——268KB 巨石按功能模块拆分。
13. **熔断器接通或移除**（D5+B6）——二选一，不要留"设计了但不生效"的假安全。
14. **WS 订阅增量更新**（D7）——解决订阅只增不减和群发问题。

---

## 六、最需要你确认的 5 件事

1. **H1 广播 bug 你之前察觉过吗？** 如果没察觉，说明实时行情可能一直不刷新——这是最该立刻确认的。
2. **实盘 callback_handler 的 H2 bug**——实盘最近下过单吗？如果下过且没崩，可能我漏看了某个定义，需复核。
3. **strict_runner / SimTraderStore(DuckDB) / kelly 等高级参数**——是有计划的下一步，还是历史重构半成品？这决定"删"还是"修"。
4. **TDX trailing_first 对齐**（D1）——文档说对齐了，代码没对齐。以哪个为准？
5. **scripts/ 哪些是常用工具**——给我个清单，我帮你把一次性脚本归档、常用脚本保留。

---

> 本报告基于代码静态审查 + grep 调用验证。B 类项和"需确认"项均带证据但未做运行时验证，删除前建议再跑一次相关测试或人工确认。
