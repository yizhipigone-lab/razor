# Code Review — 未提交改动（2026-07-19）

> 模式：Local Review（`/code-review`，无 PR 号）
> 范围：工作区未提交改动 = 32 个已跟踪文件 + 13 个新增文件（生产代码 app/ + 前端 static/ + 测试 tests/），文档与计划 .md 不审。
> 方法：4 个并行 agent 按不重叠范围审查（实盘主路径 / 数据行情层 / 回测引擎 / 前端测试），每条结论 Read/Grep 实际代码验证。
> 与 `/simplify` 互补：本次专查**正确性 + 安全**（simplify 故意跳过 bug）。

## Decision：⚠️ REQUEST CHANGES → H1/H2/H4 已修，H3 单独排期

有 **0 个 CRITICAL**、**4 个 HIGH**。截至 2026-07-19 02:49 的修复进展：

| HIGH | 状态 |
|---|---|
| H1 format_code 沪市 ETF 51/52 判深市 | ✅ 已修复（[xtquant_compat.py:113](app/utils/xtquant_compat.py#L113)） |
| H2 format_code 880 指数撞北交所 | ✅ 已修复（[xtquant_compat.py:119](app/utils/xtquant_compat.py#L119)） |
| H4 手工下单缺防重复提交 | ✅ 已修复（[live_trader.js:874](static/js/live_trader.js#L874) + 缓存 bump v=21） |
| H3 intraday 回退分支读盘风暴 + has_ohlc 永久翻转 | ⏸ **单独排期**（碰回测核心会改冷路径回测数字，配合基线 diff 一起做，不夹在本次 code-review） |

**验证**：py_compile ✓ / node -c ✓ / format_code 新断言（510300/510050/880001，原 0 覆盖）✓ / **114 个相关测试全过** ✓

当前这批改动（含 H1/H2/H4 修复）可 commit；H3 留作后续独立任务，配合回测基线 diff 评估。

---

## Findings

### CRITICAL（block）
无。未发现硬编码凭据、SQL 注入、买卖方向反、金额算错、kill_switch 完全失效、dry-run 漏判真下单。

### HIGH（should fix before commit）

**H1 — `format_code` 把沪市 ETF（51/52）判成深市**
- 位置：[xtquant_compat.py:112-114](app/utils/xtquant_compat.py#L112)
- 问题：`if code.startswith(('15','16','51','52')): return f'{code}.SZ'` 把 51/52 开头判深市。**错** —— 51/52 是沪市 ETF（510300 沪深300、510050 上证50、510500 中证500、513100 纳指 等），只有 15/16/159 才是深市 ETF。同一 commit 新增的 [price_type.py:50](app/live_trader/price_type.py#L50) `detect_market` 正确把 `50/51/52/56/58 → SH`，两者直接矛盾。
- 放大：本次 diff 把 [signal_picker.py](app/live_trader/signal_picker.py) 和 [qmt_wrapper.py](app/live_trader/qmt_wrapper.py) 从「带后缀原样保留」改成「强制剥后缀走 format_code」，触发面变大。传入 `510300.SH` → 剥成裸码 → `510300.SZ`（错）。
- 后果：信号源/持仓涉及沪市 ETF 时，行情查错市场 → 配价失败/估值错；下单桥走错市场代码可能拒单或错单。
- 修复：拆前缀 `15/16 → .SZ`；`50/51/52/56/58 → .SH`，与 `detect_market` 对齐。补测试 `format_code("510300")=="510300.SH"`。

**H2 — `format_code` 删了 880/999 特例，导致 880xxx 撞北交所分支（回归）**
- 位置：[xtquant_compat.py:119](app/utils/xtquant_compat.py#L119)
- 问题：旧版（`git show HEAD` 确认）有 `if code.startswith(('999','880')): return f'{code}.SH'`，本次 diff 删除。`999` 落默认 `.SH` 无害；但 `880001`（申万综指，在 `_CSI_INDEX_CODES` 内）会撞 `startswith('8')` → `880001.BJ`（北交所，错）。
- 后果：任何裸用 `format_code` 的指数查询路径（main.py `_resolve_instrument_name`、store 入库等）会把申万综指查成北交所代码。
- 修复：在 `8/4/920` 分支前补 `if code.startswith('880'): return f'{code}.SH'`。

**H3 — intraday 回退分支仍有「读盘风暴 + has_ohlc 永久翻转」bug（daily 修了，intraday 没修）**
- 位置：[tdx_runner.py:374-429](app/backtest/tdx_runner.py#L374)（`_run_intraday_backtest` 的 `if not _vec_ok` 回退分支）
- 问题：daily 路径（line 847-886）已修成 `low_map_cache` 按 code 缓存 + `row_has_ohlc` 逐行判断；**intraday 回退分支同段代码没修**：`low_cache` 仍按 `(dt_str, code_num)` 缓存 → 一只股 N 个信号日重读 N 次整只 parquet；`has_ohlc` 定义在 `for i` 外，一行脏数据翻转后该股后续所有行 high/open 永久=close。
- 后果：冷路径（parquet_path 缺失或向量化解析失败）下回测极慢 + 一行脏数据让整只股 TP 难触发 → 改变回测结果。正常缓存命中走 `parse_intraday` 不触发。
- 修复：把 daily 路径的 `_get_low_map` + `row_has_ohlc` 同样移植到 intraday 回退分支。

**H4 — 手工下单缺防重复提交，可重复点下多笔真单**
- 位置：[static/js/live_trader.js](static/js/live_trader.js) `submitManualOrder`（~853 行）
- 问题：`confirm()` → POST 期间下单按钮未 disable、无 inflight 标志；请求 body 不带 `client_order_id`（后端 C3 幂等去重用不上）。用户点确认 → POST 进行中再点 → 再确认 → 重复下两笔真单（限价单会双双成交）。`cancelLiveOrder` 同理但影响小。
- 后果：手抖/不耐烦导致实盘重复下单，真金白银。
- 修复：进入 `submitManualOrder` 立即 disable 按钮 + finally 恢复；或生成客户端 `client_order_id` 让后端幂等去重。

### MEDIUM（fix recommended）

| # | 位置 | 问题 | 后果 |
|---|---|---|---|
| M1 | [quote_source.py:306-313](app/data_manager/quote_source.py#L306) | `_tenc_code` 裸码分支无 ETF 规则，裸 `510300` 落深市 | 4 源降级时裸码沪市 ETF 走腾讯查错标的；带后缀路径对，触发面小 |
| M2 ✅ | [store.py:413](app/live_trader/store.py#L413) + [:950](app/live_trader/store.py#L950) | **已修**:refresh_quotes 加 `avg_cost<=0` 守卫(不瞎算浮盈) + apply_buy_fill filled_price<=0 时 avg_cost 写 NULL | filled_price=0 不再虚高浮盈;补 1 条测试(test_live_trader_audit.py) |
| M3 | [main.py:1363-1379](app/live_trader/main.py#L1363) | `_require_admin` 删了 token 校验，鉴权仅靠 `_is_local` | 改 `--host 0.0.0.0`（Docker 先例已存在）即远程无鉴权可调实盘写端点。**真钱系统建议合并前补一道非环回 token 或启动期 host 断言** |
| M4 | [risk_gate.py:244-262](app/live_trader/risk_gate.py#L244) | 闸门 5a 兜底到 `live_capital`，日亏口径变「自开户盈亏」 | 仅首日/DB 重置时触发，但触发时 5a 熔断口径错。建议加 `baseline_source=live_capital` audit 标记 |
| M5 | [tdx_parse.py:110-117](app/backtest/tdx_parse.py#L110) | `flip_per_code=True` 复刻「一行脏数据→整股 OHLC 永久=close」 | 与日线（`flip_per_code=False`）口径分叉；脏数据股 intraday 回测偏乐观 |
| M6 | [tdx_runner.py:300-302](app/backtest/tdx_runner.py#L300) | 5m bar 缺失降级日线 **close** 作开盘买入价 = look-ahead | 缺 bar 的天买入价偏低虚增收益。**既有逻辑，本次 diff 只加日志**，非新引入 |
| M7 | [tests/test_scheduler_cold_start.py:82-93](tests/test_scheduler_cold_start.py#L82) | `test_cold_start_does_not_add_to_executed_today` 断言恒真 | `_executed_today.add` 在 `_tick` 不在 `_check_signal_heartbeat`，测错了层，没覆盖真正风险 |
| M8 | [live_trader.js:418](static/js/live_trader.js#L418) | `loadLiveSwitches` catch 分支 `const ss=...; if(ss) sb.textContent='—'` 笔误（用 `sb`） | 拉取失败时卖出开关摘要不重置、显示 stale；不影响实际开关状态 |
| M9 | [qmt_wrapper.py:110-114](app/live_trader/qmt_wrapper.py#L110) | `bare_to_fmt` first-wins，同裸码跨市场（000001.SZ 银行 + 000001.SH 指数）丢一个 | 既有问题（原 `next` 也是 first match，/simplify 保持等价），实盘当前不会同裸码混查 |

### LOW（optional，信息性）
- [main.py:728/831](app/live_trader/main.py#L728) dry-run 守卫 fail-open（`runtime_state` 为 None 时不拦）——启动竞态才中招，建议改显式 `not runtime_state → 503`
- [main.py:1399-1405](app/live_trader/main.py#L1399) kill_switch 主开关「关→重开」可能复活陈旧激活态（亚秒崩溃窗口，方向 fail-safe）
- [main.py:308-316](app/live_trader/main.py#L308) `_takeover_positions` 在 QMT `last_price=0` 时显示 -100%（纯显示抖动，不影响风控）
- [risk_gate.py:193](app/live_trader/risk_gate.py#L193) 闸门 7 短路条件加 `kill_switch and` 前置，`kill_switch is None` 时闸门 7 永远 pass（仅测试场景）
- [scheduler.py:344-352](app/live_trader/scheduler.py#L344) 冷启动保护对「当日 14:50 后重启」无效（边缘误告警，无资金风险）
- [store.py:717-722](app/live_trader/store.py#L717) `get_daily_baseline` 取今日首条未过滤 stray 重启点
- [live_trader.js:782](static/js/live_trader.js#L782) `_moDetectMarket` 北交所正则比后端宽（仅影响下拉过滤，下单正确性由后端兜底）

---

## 已验证正确的关键路径（确认无问题）

- 市价单金额估算 + fail-closed（main.py:753-773）：限价缺价 400、市价取不到实时价 503 拒单，安全
- price_type 市场感知降级 + xtconstant 数值（11/5/44/45/42/43/47）与既有常量一致
- 退出优先级（exit_rules.py）：trailing_first + ladder 叠加 + TP1=3%，与 VERA 对齐；`precompute_params` 18 键与原默认值零差异
- 000001.SZ 上证指数事故（2026-07-16）：`is_index_code` 对带后缀码恒返回 False，事故路径已堵死，无残留
- first_bar_of_day O(1) 索引、tqsdk_bridge_worker 直写 parquet（mkstemp 唯一名 + schema 一致 + cleanup age 闸）
- XSS：服务端数据进 DOM 全走 `esc`/`escHtml`；`order_id` 是 BIGINT 数字，onclick 不可注入
- CSRF：实盘写端点走 `application/json`（触发预检）+ `_require_admin` 本机 IP 校验；dry-run/kill_switch/限价后端硬拒
- 前端缓存版本已 bump（?v=45 / v=20 / v=2）

## Validation

| 检查 | 结果 |
|---|---|
| py_compile（8 个改动文件） | Pass（agent1 验证） |
| pytest 11 个测试文件（新增+改动） | **158 passed** in 3.79s |

## Next steps（建议修复顺序）
1. **H1 + H2**（同一函数，5 分钟 + 补 2 条测试）—— 最确定、收益最高
2. **H4**（submitManualOrder 防重复）—— 实盘真风险，前端 disable 按钮 + client_order_id 幂等
3. **H3**（intraday 回退分支）—— 回测冷路径正确性，移植 daily 路径修复
4. M2/M3/M5 排期；M6（look-ahead）单独评估是否改既有口径
