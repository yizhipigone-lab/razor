# 审计报告:候选② 回测 simulate_one_trade(2026-07-13)

> 审计对象:候选② 行情 sourcing 之"回测"侧收敛(单笔交易仿真 4 拷贝 → 1 kernel)。
> 方法:逐条 Read/Grep 真实代码 + 10 kernel 单元测试 + 全套件回归(253 passed)。

## 审计对象清单

| 文件 | 改动 |
|---|---|
| `app/backtest/simulate_one_trade.py`(新) | 深 module,233 行;`simulate_one_trade(code, stock_name, entry, signal_date, bars_daily, params_override, time_exit_min_pnl, apply_costs) -> Optional[dict]`;内含 `_build_trade_result`(原 `_wrap_result` 逻辑) |
| `app/backtest/engine.py` | `_simulate_trade_daily_fallback` 改为薄委托(`return simulate_one_trade(...)`,8714→841 字符);`_simulate_trade_v2` 死代码删除(13572 字符) |
| `app/backtest/ai_optimizer.py` | `_fast_simulate` 改为薄委托(6602→2581 字符):1 分钟线聚为日线 OHLC → kernel → 映射 `pnl_pct=return_pct` 兼容 optimizer |
| `tests/test_simulate_one_trade.py`(新) | 10 测试覆盖 kernel 契约 |

## ✅ 通过验证

- **全套件 253 passed / 0 error / 0 fail**(此前 243 + kernel 10 新测试)。
- **kernel 10 测试关键覆盖**:硬止损、TP1=3% 覆盖(候选② 主修的 correctness bug)、trailing_first/stack 生效、time_exit、期末清仓、cost(buy 摊入 + sell 扣)、缺价返 None、params_override 无假默认(替代旧 `-7.0/15.0`)。
- **回归零破坏**:sim_trader_store、quote_source、engine_quote_characterization、live_trader_audit 等全部仍绿。
- **死代码确认**:`_simulate_trade_v2` 全仓 grep 零调用方(仅 doc/历史报告提及),删除安全。
- **委托忠实性**:`_fast_simulate` intraday→日线 聚合(1min 聚为 high=max/low=min/open=first/close=last 的日线);丢 1 天内分钟级触发时机,但参数寻优无关(优化目标是 return_pct/胜率,非毫秒级时机)。
- **返回值兼容**:`_fast_simulate` 返回的 `pnl_pct` / `hold_days` / `exit_price` 键与旧形同,optimizer 读 `trade["pnl_pct"]` 不破。
- **成本单一真相**:`simulate_one_trade` 通过 `execution.get_cost_cfg()` 拿比例(commission+slippage / +stamp);与旧 `TRADE_COST_BUY=0.125 / SELL=0.175` 数值一致(0.125%/0.175%),但消除了"engine 与 optimizer 两份硬编码常量"的隐患。

## 🔧 审计发现

### 🟡 NOTE-1:`_fast_simulate` 无直接测试
**状况**:`_fast_simulate` 旧影子本就 0 测试(报告原话"最危险")。本次 refactor 让它变忠实,但**委托映射**(`pnl_pct = result["return_pct"]` + intraday→日线聚合)本身没有单测覆盖。
**影响**:kernel 10 测试覆盖了被委托对象的正确性;委托映射靠 compile + 推理验证。如果未来有人改聚合逻辑(比如把 high 从 max 改成 mean),无测试会立即察觉。
**建议**:加 1-2 个 `_fast_simulate` 单测(用 fake intraday cache + assert `pnl_pct` 与 `simulate_one_trate(daily_aggregated_bars)` 直接调用一致)。**未做**(留作候选② 的收尾 / 后续硬化项)。

### 🟢 NOTE-2:`simulate_one_trade` 当前成本仍为比例口径
kernel 走 `get_cost_cfg()` 的 commission+slippage/stamp 比例,**未接 `execution.calc_buy_cost/calc_sell_revenue` 的金额口径(min_commission)**。这是与所有审计报告一致建议"全链路金额口径"的差异点。**不属于 ② 范围**(② = 抽 kernel + 修影子,口径切换是独立 scope);记录在此,后续若做"金额口径"硬化,只需替换 `_cost_pnl` 内 `buy_pct/sell_pct` 的计算源,接口与单元测试不变。

## 📊 总评

- 严重级别:**🟡 NOTE ×1(已记录,建议后续补测)**;无 CRITICAL / HIGH。
- 整体评分:**9/10**(影子→忠实 + kernel 单点真相 + 死代码清 + 零破坏;扣 1 分因 `_fast_simulate` 委托映射无直接测试)。
- 可交付:**是**。253 passed。`_simulate_trade_v2` 已删,旧 bug 路径切断,优化器调出的参数生产里可复现。
- 残留:`_fast_simulate` 单测(建议下次硬化时补);金额口径成本(独立 scope)。

## 与候选①的衔接

候选① 统一了**行情 sourcing**(实时报价侧);候选② 统一了**回测 simulate**(回测仿真侧)。两条候选共同把"量化平台核心两条数据路径"收敛到深 module。剩余候选(③ 下单编排 / ④ cron_jobs / ⑤ 选股 base.py / ⑥ 行情缝合)继续按 grilling 推进。
