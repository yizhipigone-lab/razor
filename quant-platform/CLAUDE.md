# 项目开发规则 (quant-platform)

> 本文件为项目级规则，Claude 在本项目工作时自动加载。

## 全局规则：行情数据优先用 QMT

**触发**：任何需要股票实时价/昨收价/涨跌幅的场景（净值计算、持仓估值、今日盈亏、盘中监控、选股价格等）。

**要求**：行情数据获取的优先级固定为（4 源，per-code 逐只降级，统一封装见 `app/data_manager/quote_source.py`）：

1. **QMT 实时行情**（首选）
   - 后端网关：`app/trader/gateways/qmt.py` → `qmt_gateway.get_live_trader_quotes(codes)`（纯 QMT HTTP client，供 quote_source.QmtHttpAdapter 调用；老的带兜底 get_realtime_quotes 已删）
   - 统一封装：`app/data_manager/quote_source.py` → `get_realtime_quotes(code_list)`（深 module；`engine.get_realtime_quote` 已委托给它。返回 DataFrame 含 `price` / `last_close` / `change_pct` / `source` 列）
   - 引擎内建：`SimTraderEngine.build_live_snapshot()`（为当前持仓构建 QMT 实时 snapshot，含 close + preClose）
   - QMT 行情：Windows 端 `app/live_trader/main.py`，端口 8001，路径 `/live/quotes?codes=...`（原 qmt_proxy:8081 已废弃，所有接口已迁移至 live_trader）
   - 返回字段：`lastPrice`（现价）、`lastClose`（昨收）、`open/high/low`

2. **TDX 高速通道**（QMT 失败时回退，socket；2026-07-13 grilling 决议保留作第 4 源）

3. **腾讯 HTTP 行情**（QMT+TDX 失败时回退）
   - `app/sim_trader/data_loader.py` → `augment_bars_with_realtime` 通道2

4. **Parquet 历史收盘价**（前三者都失败时兜底；**无昨收 → `last_close=NaN`**，严禁用 close 冒充）
   - `data/parquet/daily/{code}.parquet`

**禁止**：

- ❌ 不要用"昨日快照 / 买入价"冒充实时价来算净值（2026-07 净值失真根因：盘中监控器 record 时误用 `_prev_day_snap`）
- ❌ 不要在有 QMT 的情况下直接跳到 Parquet
- ❌ record() 传入的 snapshot 必须尽量含实时价；缺价时 `total_equity` 会用 `pos.current_price` 兜底并标记 `source=partial`

**净值可信度保护**（已实现，勿绕过）：

- `total_equity` 估值优先级：snapshot 现价 > `pos.current_price`（上次市价） > `entry_price`（兜底）
- `record()` 检测行情覆盖率，不全时标记 `equity_curve` 的 `source=partial` 并告警
- 单日净值跳变 >15% 告警

## 模拟盘状态文件 (state.json) 保护

- 运行态：`output/sim_trader/state.json`（**唯一真相源**）
- 灌数/回测输出：`output/sim_trader/imports/`（与运行态隔离，禁止覆盖 state.json）
- `populate_sim_trader.py` 有护栏：禁止覆盖运行态，除非显式 `--force-overwrite-live`
- 加载期有一致性校验：首条 equity > 本金 1.10 倍则拒绝采用（防回测污染）

## 今日盈亏口径

- **当日买入**的持仓：今日盈亏 = (现价 − 买入价) × 股数
- **过夜持仓**：今日盈亏 = (现价 − 昨收价) × 股数
- 已平仓历史交易：无"今日"概念，显示 `--`
- 前端实时更新基准价通过 `data-basepx` 属性传递

## DuckDB 规则

- 别在不加锁的情况下跑并发 DuckDB 连接——WAL 损坏和 stale lock 已多次导致启动失败
- 优雅关闭**优先用 `/shutdown` HTTP 端点**（已实现，根治 WAL 损坏）；stop 脚本按进程名 + 端口双杀兜底，**不能只按端口**
- 启动 DuckDB 前检查并清理 stale `.wal` / `.lock` 文件
- DuckDB 被锁时，先诊断是不是 stop.bat 进程匹配失败导致残留，再重试
- Windows 的 SIGINT 不触发 lifespan、bat 块内禁半角括号、timeout 改 ping（详见优雅关闭机制）
- **DuckDB 连接必须在 finally 块或 context manager 中显式关闭**，严禁依赖 GC 回收
- **严禁跨多个操作持有 DuckDB 写锁**；有批量写入时合并为单次操作

## 前端标准

- 禁止硬编码颜色值——一律用 CSS 自定义属性 `var(--token-name)`
- 用 replace_all 改 CSS 时，验证没引入硬编码值（曾把 `var(--orange)` 硬编码成 `#ffa657`）
- 改 CSS 文件后 bump 缓存版本（如 `?v=X`），避免浏览器旧缓存导致看不到变化
- CSS 命名与现有代码风格一致（项目用设计 token + 工具类，别混入 BEM 除非该模块已在用）
- 货币格式：负数写成 `-¥500`，不是 `¥-500`
- 禁止用脚本批量改前端代码，必须逐个 Edit + `node -c` 验证

## 止损与交易逻辑

- 退出优先级顺序必须显式文档化，且后端前端一致（已对齐 VERA：stop + trailing_first + 叠加 + TP1=3%）
- 买入执行价：在代码注释和变量名里写清是 T 日收盘还是 T+1 开盘，**别用歧义术语**
- 回测结果和 VERA 不一致时，先查三件事：退出优先级、成交价假设、买入价术语
- 移动止盈逻辑必须用同 bar 双触发检查，避免 look-ahead bias（曾出现 gap-down 保护在开盘价执行而非回撤线）
- 回测 4 引擎（engine/simple/tdx/strict）的成本/T+1/涨停各写各的，结果不可比；规则判断已归一

## 审计规则

- 多 agent 审计时，给每个 agent **明确、不重叠**的范围
- **禁止 audit agent 派子 agent**（2026-07 亲历：研究 agent 递归派子 agent 不返回结果，只能杀掉重派；insights 报告同案例）
- 审计结论必须 Read / Grep 实际代码验证，不靠推断（防 context-dependency 错误：行号对但周边代码已变）
- 审计报告立即存 .md，PASS / FAIL / WARNING 分类，不等用户要
- 完整版见全局 `~/.claude/rules/workflow/audit-verification.md`

## 质量门禁 (Quality Gates)

**触发**：任何代码改动完成后、声称"完工"之前。

**必须执行**：

1. **改完就跑测试**：任何 Python 代码改动后，立即跑相关测试或至少 `python -m py_compile` 语法检查。绝不跳过这步就说"完成了"。
2. **前端改动必 bump 缓存**：修改 CSS/JS/HTML 后，必须更新缓存版本号（如 `?v=X`），否则浏览器拿旧缓存用户看不到变化。
3. **修 bug 前先追根因**：不要靠猜，必须 Read/Grep 实际代码追踪完整调用链，找到根因后再动手。**绝不从症状直接跳结论。**

**反例**：

- ❌ 改完代码不跑测试 → 引入线程死锁到后期才被发现
- ❌ 改 CSS 忘了 bump 缓存版本 → 用户反复说"没看到变化"
- ❌ 不追根因就贴补丁 → bug 复发

## 编码与平台 (Encoding & Platform)

**触发**：所有文件读写、跨平台脚本编写。

| 规则 | 说明 |
|---|---|
| **Python 文件用 UTF-8** | Python 3 默认 UTF-8，`# -*- coding: utf-8 -*-` 非必需 |
| **.bat 文件必须 GBK/ANSI** | Windows cmd 默认 GBK，UTF-8 会乱码。Write 工具写出是 UTF-8，写 .bat 后必须转码 |
| **Bash 输出含 GBK 时 pipe 转码** | `| iconv -f GBK -t UTF-8` 或先 `chcp 65001` |
| **PowerShell 变量用 `${env:VAR}`** | 不用 `$VAR`（那是 Bash 语法） |
| **TDX 文件是 GBK** | 通达信导出的文件按 GBK 读，其他数据源一律 UTF-8 |
| **终端状态码用 ASCII** | `[OK]` / `[FAIL]` / `[PASS]`，避免 ✓/✗（GBK 终端可能乱码）；emoji（✅❌⚠️）可用 |

## 自审计协议 (Self-Audit Protocol)

**触发**：完成任何多文件实现后、commit 之前。

**检查清单**（4 维度，逐项过）：

| 维度 | 检查什么 |
|---|---|
| **死代码** | 有无未使用的 import、函数、变量、文件？ |
| **硬编码值** | 颜色是否用了 `var(--token)` 而非 `#ffa657`？配置值是否应走 config.py 而非写死？ |
| **线程/锁安全** | 有无非重入 Lock 被获取两次？异步任务有无循环依赖？ |
| **边界条件** | 空数据、null 值、超时、网络断开时会不会崩？ |

**输出**：自检完在回复里列出"检查了什么 + 发现了什么 + 修了什么"。已知未修复的问题必须显式标出来，不要让用户事后发现。

**与审计规则的关系**：审计规则管的是"多 agent 并行审计流程怎么跑"；自审计协议管的是"你自己改完代码后先查什么"。两者互补。
