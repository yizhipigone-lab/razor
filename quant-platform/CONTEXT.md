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

## (后续 grilling 候选 ②③④⑤⑥ 的概念,待各自 grilling 时补入)
