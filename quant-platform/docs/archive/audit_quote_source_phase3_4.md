# 审计报告:QuoteSource Phase 3 + loose end + Phase 4(2026-07-13)

> 审计对象:候选 ① 收官批次(Phase 3 迁 4 调用方 + QmtHttpAdapter QMT-only + Phase 4 删死代码)。
> 方法:逐条 Read/Grep 真实代码 + 全仓 grep 消费者/接线 + 全套件回归。

## 审计对象清单

| 文件 | 改动 |
|---|---|
| `app/trader/gateways/qmt.py` | 加 `get_live_trader_quotes`(纯 QMT);删 `get_realtime_quotes`+`_fallback_tencent/parquet`+死属性 `_quote_cache/_cache_ttl`(砍半 270→146 行) |
| `app/data_manager/quote_source.py` | QmtHttpAdapter.fetch 改调 `get_live_trader_quotes` |
| `app/api/system.py` | get_bars 迁 quote_source(DataFrame)+ NaN 守卫 |
| `app/sim_trader/engine.py` | build_live_snapshot 迁 quote_source + NaN 守卫 |
| `app/screener/engine.py` | _fetch_live_quotes 迁 quote_source(边界转 camelCase dict) |
| `app/sim_trader/data_loader.py` | augment 迁 quote_source,**删内嵌腾讯双通道** |
| `CLAUDE.md` / `tests/test_quote_source.py` | 同步更新 |

## ✅ 通过验证

- **全套件 243 passed / 0 error / 0 fail**。
- **接线**:`get_live_trader_quotes` 定义于 [qmt.py:78](../app/trader/gateways/qmt.py#L78),被 [QmtHttpAdapter:241](../app/data_manager/quote_source.py#L241) 调用,4 个 adapter 现真正独立。
- **死代码确认**:`qmt_gateway.get_realtime_quotes` 全仓 grep 零生产调用方(仅文档/历史/测试注释提及),删除安全。
- **消费者字段映射匹配**:
  - screener 缝合器(engine.py:68-76)读 lastPrice/open/high/low/volume/amount ↔ `_fetch_live_quotes` 返回的 camelCase dict 完全一致。
  - data_loader `_build_result`(data_loader.py:60-78)读 price/open/high/low/volume ↔ augment 转换的 dict 一致。
- **净值安全**:[total_equity](../app/sim_trader/engine.py#L395) 用 `snapshot[code]['close']`,**不用 preClose** → preClose 的 NaN 不污染净值。
- **前端 NaN 防御**:main.js / ui-renderer.js 对 preClose 一律 `> 0` 守卫后才算涨跌 → NaN 显示 '--'/0。

## 🔧 审计发现 + 迭代

### 🟡 FIXED-1/2:两处 `or 0` NaN-slip(已修)
**问题**:`build_live_snapshot`([engine.py:381](../app/sim_trader/engine.py#L381))与 `get_bars`([system.py:172](../app/api/system.py#L172))原写 `float(row.get('last_close', 0) or 0)`。Q6 下 `last_close` 可能是 NaN,而 **NaN 是 truthy → `NaN or 0` = NaN**(兜底失效)→ preClose/pre_close=NaN(老代码是 0)。
**影响**:净值不受影响(total_equity 用 close);前端 `>0` 守卫也挡住;但代码意图错 + 偏离老行为。
**修复**:改成显式 NaN 检查 `_lc is not None and _lc == _lc and _lc > 0`,NaN/缺失→0.0,真实正值→float。

### 🟢 NOTE-3:Q6 的 preClose NaN 是"诚实"而非 bug
build_live_snapshot 的 preClose 在无真实昨收时为 0(修后),而非伪造昨收——这守 Q6/CLAUDE.md:26。盘中持仓通常 QMT 给真实 lastClose,preClose 正常;仅极少数缺昨收的持仓 preClose=0。

## 📊 总评

- 严重级别:🟡 ×2(已修)· 🟢 NOTE ×1 · **无 HIGH / CRITICAL**。
- 整体评分:**9.5/10**(只有 2 处 NaN-slip,且影响低、已修;净值主路径从头到尾没受 preClose 影响)。
- 可交付:**是**。243 passed 零破坏。

## 残留风险

- **格式真实性只能实盘验**(老问题):腾讯/TDX 解析照抄 engine.py,真实响应格式匹配要运行时。用户至今只做代码级测试,未实盘验证。
- **2 处 dict 边界转换**(screener _fetch_live_quotes、data_loader augment)是临时 adapter,等候选 ⑥(缝合器)再迁 DataFrame 时可进一步简化——非 bug,记录。
