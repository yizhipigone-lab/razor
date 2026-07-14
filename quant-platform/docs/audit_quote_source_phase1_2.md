# 审计报告:QuoteSource Phase 1 + Phase 2(2026-07-13)

> 审计对象:候选 ① 行情 sourcing 深 module 的落地(Phase 1 深模块 + Phase 2 engine 委托)。
> 方法:逐条 Read/Grep 真实代码验证 + 全仓 grep 消费者盘点 + 全套件回归。

## 审计对象清单

| 文件 | 改动 |
|---|---|
| `app/data_manager/quote_source.py` | 新增:深 module(orchestrator + 4 adapter + 缓存/熔断 + 契约) |
| `tests/test_quote_source.py` | 新增:33 测试 |
| `tests/test_engine_quote_characterization.py` | 新增:6 characterization |
| `app/data_manager/engine.py` | `get_realtime_quote` 委托 + 2 内部调用方 NaN 守卫 |
| `app/sim_trader/intraday_monitor.py` | NaN 守卫(盘中监控器,净值失真旧点) |
| `app/api/sim_trader.py` | `price<=0` → `not(price>0)`(原写法抓不住 NaN) |
| `app/api/market.py` | 过滤缺价行 |
| `CLAUDE.md` | 补 TDX(4 源)+ 指向 quote_source |

## ✅ 通过验证

- **全套件 243 passed / 0 error / 0 fail**(应用停了,含 test_screener_engine)。
- **深模块逻辑**:per-code 降级 / 缓存 TTL / 熔断 / 昨收规则(lastClose→preClose→NaN)全部单测覆盖(33 测试)。
- **engine 委托**:characterization 6 测试锁定新契约;3 个 Q5/Q6 故意变更(source 列 / last_close→NaN / 空→missing)逐条确认预期。
- **7 个 engine 调用方**全 NaN 守卫(grep 盘点无遗漏,见下)。

## 🔧 审计发现 + 迭代

### 🔴 HIGH-1:漏网调用方 `server/market/quotes.py:109`(已修)
**问题**:Phase 2 漏了 `get_fallback_quotes` 也调 `engine.get_realtime_quote`。委托后 missing 行让 `df.empty=False`,`float(row.get('price',0))` 对 NaN 行得到 NaN → 漏进 result dict(SYSTEM_AUDIT.md:167 提过 server/market 这条,我之前漏看)。
**修复**:加 `if not (price > 0): continue` 守卫(与其余 6 调用方一致)。
**证据**:[server/market/quotes.py:113-122](../server/market/quotes.py#L113)。

### 🟡 LOW-2/3/4:文档腐烂(已修)
- quote_source.py 模块 docstring 还写"不碰现有文件 / 待 Phase 1b"(早已过时)→ 更新。
- TdxAdapter docstring "CLAUDE.md 待补 TDX"(已补)→ 更新。
- ParquetAdapter docstring "废除 qmt.py:222"(qmt.py 尚未动,Phase 3 才动)→ 改为"本 adapter 不冒充;qmt.py:222 待 Phase 3 废除"。

### 🟢 NOTE-5:Parquet 兜底行的 NaN(Q6 预期,非 bug)
engine 现在能到 Parquet(D2 修复)。仅 Parquet 能定价的票(QMT+TDX+腾讯 全失败)会带 `last_close=NaN, change_pct=NaN, source='parquet'` 到调用方。这是 D2+Q6 的有意结果。前端/调用方应处理 NaN 或按 `source='parquet'` 识别。**频率低**(盘中 QMT 通常可用)。

### 🟢 NOTE-6:`server/market/quotes.py` 无直接测试
该 caller 一直 0 测试覆盖(故审计才漏发现)。建议后续补一个 async characterization(mock get_realtime_quote)。

## 📊 总评

- 严重级别:HIGH ×1(已修)· LOW ×3(已修)· NOTE ×2(记录)· **无 CRITICAL**。
- 整体评分:**9/10**(漏一个调用方扣分,但审计自己抓回并修了)。
- 可交付:**是**。迭代后 243 passed 零破坏。

## 残留风险(Phase 3 前须知)

- **格式真实性只能实盘验**:腾讯/TDX 的解析索引逐字复刻自 engine.py,但真实响应格式匹配只能靠运行时(审计/单测验不出来)。用户尚未在实盘验证 engine 链路。
- **qmt_gateway 不能委托回 quote_source**(会递归:QmtHttpAdapter → qmt_gateway → quote_source → QmtHttpAdapter…)。Phase 3 的正解是**把 qmt_gateway 的 4 个直接调用方迁到 quote_source(DataFrame)**,qmt_gateway 退化为 QmtHttpAdapter 的内部 HTTP client。
- **实盘下单/退出路径用的是 `qmt_wrapper`(xtdata 直连),不是 qmt_gateway** —— Phase 3 不碰它们(qmt_wrapper 是 live_trader 内部,grilling Q3 决议 HTTP 边界永久)。
