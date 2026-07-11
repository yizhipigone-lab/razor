# 项目开发规则 (quant-platform)

> 本文件为项目级规则，Claude 在本项目工作时自动加载。

## 全局规则：行情数据优先用 QMT

**触发**：任何需要股票实时价/昨收价/涨跌幅的场景（净值计算、持仓估值、今日盈亏、盘中监控、选股价格等）。

**要求**：行情数据获取的优先级固定为：

1. **QMT 实时行情**（首选）
   - 后端网关：`app/trader/gateways/qmt.py` → `qmt_gateway.get_realtime_quotes(codes)`
   - 统一封装：`app/data_manager/engine.py` → `get_realtime_quote(code_list)`（返回含 `price` / `last_close` / `change_pct` 的 DataFrame）
   - 引擎内建：`SimTraderEngine.build_live_snapshot()`（为当前持仓构建 QMT 实时 snapshot，含 close + preClose）
   - QMT 代理：Windows 端 `qmt_proxy_server.py`，端口 8081，路径 `/api/quotes?codes=...`
   - 返回字段：`lastPrice`（现价）、`lastClose`（昨收）、`open/high/low`

2. **腾讯 HTTP 行情**（QMT 失败时回退）
   - `app/sim_trader/data_loader.py` → `augment_bars_with_realtime` 通道2

3. **Parquet 历史收盘价**（前两者都失败时兜底）
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
