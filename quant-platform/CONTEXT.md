# 领域词汇表 (CONTEXT.md)

> 本文件收录**架构 seam 的领域命名**,供 `/improve-codebase-architecture`、`/codebase-design`、grilling 使用。
> 项目业务规则与约束见 [CLAUDE.md](CLAUDE.md);本文件只记"深 module / seam 的概念命名与契约"。
> 维护原则:grilling 里命名了一个新深 module 或敲定了一个 seam 契约,就补进这里;未来的架构评审用这套词,避免重新发明。

---

## 行情 sourcing(2026-07-13 grilling 结晶 · 候选 ①)

**QuoteSource** — 行情 sourcing 的深 module,位于 `app/data_manager/quote_source.py`(待建)。

- **port(interface)**:`get_realtime_quotes(codes: list[str]) -> pd.DataFrame`,返回 snake_case 列(`code` / `price` / `open` / `high` / `low` / `volume` / `amount` / `last_close` / `change_pct` / `source`)。
- **DataFrame 契约**(interface 的一部分,非仅类型签名):
  - 每个请求 code **必有一行**;拿不到价的字段为 `NaN`。
  - `source` 列 ∈ `{qmt, tdx, tencent, parquet, missing}`,标该行由哪个 adapter 解析出来。
  - `last_close` = `lastClose → preClose → NaN`(**不伪造**,严禁用现价/Parquet close 冒充昨收,守 CLAUDE.md:26)。
- **4 个 adapter**(真 seam —— 满足"两 adapter = 真 seam"两倍):
  - `QmtHttpAdapter` — server 经 `live_trader:8001` HTTP 拿 QMT(server 侧 QMT 唯一 adapter)。
  - `TdxAdapter` — TDX socket(`119.147.212.81:7709`)。
  - `TencentAdapter` — 腾讯 HTTP。
  - `ParquetAdapter` — 本地 `data/parquet/daily/{code}.parquet`;**无昨收**,行恒为 `last_close=NaN, source='parquet'`(partial)。
- **orchestrator**(藏在 port 后,非 interface):per-code **逐只降级**(QMT→TDX→腾讯→Parquet,每只各自落到能拿到的最高源,一批可混源);持有 3s TTL 共享缓存 + 每源 30s 可用性熔断。
- **HTTP 边界(永久约束)**:server 永远走 HTTP 到 `live_trader:8001` 拿 QMT;xtdata 只在 Windows 本机 live_trader 里。`qmt_wrapper` 是 **live_trader 内部**实现,不是 server 侧 adapter。

**迁移姿态**:4 旧入口(`engine.get_realtime_quote` / `qmt_gateway.get_realtime_quotes` / `data_loader.augment_bars_with_realtime` / `qmt_wrapper`)首批迁到 port;`qmt_gateway` **不留 dict 门面**,8+ 调用方首批迁到 DataFrame。测试策略 **replace, don't layer**:现 0 测试,新测试直接写在 port interface 上,用 in-memory fake adapter 注入。

**待办(本结晶引发的)**:
- **CLAUDE.md:9-22** 行情优先级规则需补 TDX(现为 `QMT→腾讯→Parquet` 三层,与本结晶的 4 源 `QMT→TDX→腾讯→Parquet` 冲突)。
- `qmt.py:222` Parquet 用 close 冒充 lastClose 的旧行为废除。

---

## 退出策略 ExitPolicy(2026-07-19 grilling 结晶 · 候选 ②)

**ExitPolicy** — 退出策略"形状"的深 module,位于 `app/backtest/exit_policy.py`(待建),**组合**现有 `exit_rule_engine`(不替换;该引擎 2026-07-16 刚做 precompute_params 性能优化,勿搅)。

- **port(interface)**:
  - `evaluate(ctx: RuleContext) -> list[ExitSignal]` — 规范有序栈(VERA trailing_first + 叠加:部分卖 TP1 在前、全卖 TR/HS 在后,按 remaining 预算)。**唯一真相**,语义 = 现 `check_all`。
  - `top(ctx: RuleContext) -> Optional[ExitSignal]` — 栈顶信号,单信号调用方用(= `evaluate` 首项;实现可短路,不必算完整栈)。行为与现 `check` 等价。
  - `preview(ctx: RuleContext) -> dict` — 每条规则距触发的结构化距离,供 UI 渲染;**与 evaluate 同源**(读 `ctx.low`/`ctx.high`,非收盘口径),取代 `main.py:1505-1744` 的 close 口径改写。
- **接口契约（caller 必知，2026-07-19 审计发现）**:`evaluate`/`top` 经 `check_all`/`check` → `rule_take_profit` 会**原地改 `ctx.triggered_tiers`**(exit_rules.py:191-192)。调用方必须传一次性 ctx,不得在 `evaluate`/`top` 之间复用同一 ctx(否则触发态串味 → 结果不一致)。现状所有生产 caller 每次 `build_context` 都新建 ctx,符合;测试须每断言现场新建。
- **behind the seam**(非 interface):现有 `ExitRuleEngine` + `RuleContext` + 规则函数(`rule_hard_stop` / `rule_take_profit` / `rule_trailing_stop` / ...)原封不动;ExitPolicy 只锁"求值形状",不动公式。
- **依赖类别 = in-process**(纯计算:bar + 持仓 + params → 信号);interface 处**无 port/adapter**,直接用 in-memory 假 ctx 测。
- **测试姿态 = replace, don't layer**:引擎层现 ~0 测试;新测试直接写在 ExitPolicy interface 上。**keystone 测试**:`preview()` 的触发判定与 `evaluate()` 的信号一致(preview 说 safe ⟺ evaluate 该规则不触发)——这条锁死 CLAUDE.md "后端前端一致"。
- **迁移姿态(绞杀者)**:`simple_runner.py:166`(唯一 `check_all` 调用方)→ `evaluate`(零行为变化);8 个 `check` 调用方 → `top`(行为等价);`main.py:1497-1755` risk-status → `preview`(260 行收成 ~20 行 shaping)。老路径留到迁完再拆。
- **显式缺口(2026-07-19 grilling 决议,Q1=B)**:`evaluate` 恒返回规范栈(唯一真相),但**实盘/盘中 scan 仍只执行 `top()`**(最高优先级单信号),不处理同 bar 多信号叠加。backtest↔实盘 的叠加栈钱分歧从"静默丢栈"升级为"**显式记录的已知缺口**";让实盘真正吃栈(改 `exit_monitor` 扫描模型 + `MAX_SELL_PER_SCAN`)留作**后续候选 ②-延展**。⚠️ **勿误以为本结晶已修完叠加栈分歧。**

**待办(本结晶引发的)**:
- 建 `app/backtest/exit_policy.py`,迁 9 个调用方(1 evaluate + 8 top)。
- 删 `main.py:1497-1755` 的 close 口径改写,改走 `preview()`。
- 后续候选 ②-延展:实盘吃栈。

---

## (后续 grilling 候选 ③④⑤⑥ 的概念,待各自 grilling 时补入)
