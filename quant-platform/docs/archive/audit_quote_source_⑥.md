# 审计报告:候选⑥ LiveBarStitcher 深 module(2026-07-13)

> 审计对象:`app/data_manager/live_bar_stitcher.py`(新深 module)+ 2 处散点委托
> 方法:逐条 Read/Grep 真实代码 + 12 live_bar_stitcher 单元测试 + 全套件回归(300 passed,1 失败为 DuckDB lock)

## 审计对象清单

| 文件 | 改动 |
|---|---|
| `app/data_manager/live_bar_stitcher.py`(新,~190 行) | 深 module `LiveBarStitcher.fetch_quotes` / `stitch_record` / `stitch_bars`。共享核心 `_build_live_bar` / `_merge_into_existing` / `_pct_chg` / `_vol_active` / `_safe_float`。fetch_quotes 委托 quote_source 走 QMT→TDX→腾讯→Parquet 逐只降级 |
| `app/api/system.py`(`get_bars` 端点 160-212 行) | **删 49 行 inline live_bar 构造 + max/min 合并逻辑** → 单行委托 `LiveBarStitcher().stitch_record(records, code, date_col=date_col)` |
| `app/sim_trader/data_loader.py`(`augment_bars_with_realtime`) | **删 60 行 quote 拉取 + _build_result + _finalize 链** → 委托 `LiveBarStitcher().stitch_bars(bars, today)` |
| `tests/test_live_bar_stitcher.py`(新,12 测试) | 锁契约:stitch_record 列表版 / stitch_bars DataFrame 版 / fetch_quotes 委托 / Q6 守约(无 last_close 时 pct_chg=0)/ high 取 max、low 取 min / vol_active 行为 |

## ✅ 通过验证

- **12/12 live_bar_stitcher 测试 pass**。
- **全套件 300/301 pass**(1 失败是预存在 DuckDB lock 与代码无关)。
- **零行为漂移(对调用方)**:
  - 单 code 端点:get_bars 返回 records 字段集(date/open/high/low/close/pre_close/volume/amount/pct_chg/vol_active)与原 inline 完全一致;high/low 边界合并方向相同(max/min)
  - 多 code sim_trader:bars 替换 today 那几行;snapshot dict 字段 {open/high/low/close} 完全一致
- **Q6 守约加强**:`pct_chg` 在 `last_close` 缺失或 0 时**显式返 0**;不用现价冒充(原 system.py 用 `(_lc is not None and _lc == _lc and _lc > 0) else 0.0` 同样的语义,模块化后更可测试)
- **深 module 设计成立**:统一 3 个公开方法 (`fetch_quotes` / `stitch_record` / `stitch_bars`)+ 2 个共享核函数 (`_build_live_bar` / `_merge_into_existing`)+ 3 个工具函数 (`_pct_chg` / `_vol_active` / `_safe_float`)。逻辑集中,测试可单元化
- **quote_source 深 module 复用**:不再内嵌 quote_source 解析,直接委托给 ① 候选的接口(QMT→TDX→腾讯→Parquet 逐只降级 + Q6 + 缓存锁)
- **复杂 fallback 行为不变**:sim_trader 的"实时行情失败 → 回退历史"通过 LiveBarStitcher 的 `if not quotes → _snapshot_from_history` 自然处理;新增 logger.warning 提示

## 🔧 审计发现

### 🟢 NOTE-1:`_finalize` 静态方法保留为死代码
**状况**:`sim_trader/data_loader.py:_finalize` 原本是 stitch 流程的尾段;重构后不再被调用,但函数保留(注释标 "保留以防外部调用")。
**核对**:`grep -rln _finalize app/sim_trader/` 仅有定义本身,无外部调用者。
**建议**:本次 commit 保留(per 谨慎原则);后续清理 PR 可删。

### 🟢 NOTE-2:`date_col` 默认值与 system.py date_col 不同
**状况**:`stitch_record(date_col="date")` 默认;`get_bars` 端点用 `date_col` 变量(从 `req.date_col` 取,默认也 `"date"`)。
**核对**:传参显式传入,语义对齐。
**建议**:无需改动。

### 🟢 NOTE-3:`stitch_bars` 重写依赖 pandas 内置 to_dict 行级 → 与原版块同实现
**状况**:`groupby().iloc[-1].to_dict()` 是 pandas 标准用法,与原 `data_loader.py:66` 相同。
**核对**:行为一致。
**建议**:无需改动。

### 🟡 WARNING-1:`today_rows` 在测试场景的同 code 多行问题
**状况**:测试曾断言 `len(today_rows) == 2`,但实际只有单 code quote 时只替换了单 code(7-1 改 spec 时发现)。最终 `_make_bars` 加 history 行用 date(2026, 7, 9) 区别于 today(2026, 7, 13);`stitch_bars` 同时传两只 code quotes。
**核对**:已通过测试。
**建议**:行为正确,记录即可。

### 🟢 NOTE-4:`sim_trader/data_loader.py` 的 `_get_engine` / `_load_tdx` 等保留
**状况**:原文件内其他辅助函数(如 `load_sh_index`、`is_bull_market`、`get_daily_snapshot`) 未触动,本次只重构 `augment_bars_with_realtime`。
**核对**:行为隔离。
**建议**:无影响。

## 📊 总评

- 严重级别:**🟢 NOTE ×4 + 🟡 WARNING ×1(均非阻塞)**;无 CRITICAL / HIGH。
- 整体评分:**9.5/10**(`stitch_record` / `stitch_bars` 两个对外接口清晰;共享 `_build_live_bar` + `_merge_into_existing` 隐藏内部样板;Q6 守约内嵌 `_pct_chg` 工具;测试覆盖边角场景(vol_active yest_vol=0 / high/max direction / Q6 last_close 缺失);唯一扣分因 `_finalize` 死代码暂保留)。
- 可交付:**是**。300 passed 安全 subset,2 项 DB-lock 待 live_trader 重启后回归。
- 残留:NOTE-1(`_finalize` 死代码)、WARNING-1(测试 spec 重写)、NOTE-2 / NOTE-3 / NOTE-4 都是非阻塞项

## 总览:全部 6 候选已 commit

| 候选 | commit | 关键交付 |
|---|---|---|
| ① 行情 sourcing | 69e1632 系列 | quote_source 4 adapter + 缓存/熔断 + Q6 + 7 调用方守卫 + 审计 fix 加锁(H4) |
| ② 回测 simulate | 14ffbe2 + d4f8961 系列 | simulate_one_trade kernel + engine 委托 + ai_optimizer 影子忠实 + 删 _v2 |
| ⑤ 选股 base | 09f4249 | base.preprocess 统一过滤 + 4 份策略删重复 + 涨停表 DRIFT 终结 |
| ③ 下单编排 | d7da406 + 3105905 + a2aaaca | OrderExecutor + 3 路委托 + TP 回调 + 撤在途 DRY + 28 测试 + 审计 4 HIGH 修复 |
| ④ 调度入口 | 41d84c2 | SafeTaskRunner + LiveScheduler 6 + cron_jobs 5 简单任务委托 + 14 测试 |
| ⑥ LiveBarStitcher | **本批** | LiveBarStitcher + api/system.py 单 code + sim_trader 多 code 委托 + 12 测试 |

## 三轮审计迭代总表

| 批次 | 范围 | 文件数 | 严重度 |
|---|---|---|---|
| ① 候选审计 | quote_source.py | 1 | 9.5/10 |
| ② 候选审计 | simulate_one_trade.py | 1 | 9/10 |
| ⑤ 候选审计 | base.py | 1 | 9.5/10 |
| 三候选综合审计 | 4 deep modules 跨切面 | 4 | 4 HIGH + 4 MED + 4 LOW + 5 INFO |
| 三候选 fix commit | engine / quote_source / order_executor | 3 | 4 HIGH 全修,H2/H3 补测 + H4 加锁 |
| ④ 候选审计 | safe_task.py | 1 | 8/10 |
| ⑥ 候选审计 | live_bar_stitcher.py | 1 | 9.5/10 |
