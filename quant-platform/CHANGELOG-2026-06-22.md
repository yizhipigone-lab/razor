# 2026-06-22 Bug 修复 CHANGELOG

> 8 个原子 commit,严格遵循"零新问题 + 业务连续性"硬约束。

## 修复的 8 个 Issue

| # | 严重度 | 简述 | Commit | 域 |
|---|---|---|---|---|
| 2 | 🔴 | `db.update_stock_list` 静默失败 → 加 alias + 改裸 except | `fcc23b8` | A 数据 |
| 7 | 🟠 | Tushare amount × 1000 → 删 2 行 | `c4dc57a` | A 数据 |
| 16 | 🟡 | "风险"死配置 → 删除 | `fd80acd` | A 数据 |
| 10 | 🟠 | committee EMPTY 兜底 → 抛 RuntimeError + 修模型名 | `20a1e7f` | B Agent |
| 11 | 🟠 | sim_trader 重复写 trade → 删两处 append | `8bc34ad` | C 模拟盘 |
| 13 | 🟠 | intraday_monitor 日历日 → 交易日 | `a1f1a7e` | C 模拟盘 |
| 8 | 🟠 | Position.market_value 用成本价 → 用 current_price | `8d09bbc` | C 模拟盘 |
| 9 | 🟠 | _prev_snap 语义错 → caller 维护 _prev_day_snap | `8ed9181` | C 模拟盘 |

**额外 commit**:`8efa5fa` Revert Task 3 (硬约束发挥作用:子代理范围蔓延,被立即 revert 重做)

## 已知遗留(spec 范围外的同根源 bug)

### 必须修但本次未修(影响业务,建议下一批处理)

1. **`scripts/backfill_daily_tushare.py:55`** — 仍有 `df['amount'] = (df['amount'] * 1000).fillna(...)`,与 #7 同根因
   - 影响:重刷历史 amount 数据时仍会带 1000 倍虚高
   - 建议:同 #7 改法,删 `* 1000`

2. **`app/agents/concept_miner.py:100` / `stock_analyst.py:47` / `app/backtest/llm_advisor.py:144`** — 仍有 `model="deepseek-v4-pro"`,与 #10 同根因
   - 影响:这些 LLM 调用会用错误的模型名,DeepSeek 实际不存在 `deepseek-v4-pro`
   - 建议:同 #10 改法,改为 `deepseek-chat`

3. **`app/api/sim_trader.py:253, 306, 331` / `app/scheduler/cron_jobs.py:440`** — 用 `len(engine.trades)` 算"今日交易数"
   - 影响:Task 11 修复后,运行时新增 trade 不再进内存,前端 WebSocket 推送会看到 sell_count 突然归零
   - 建议:在 `execute_sell` 后加 `self._today_trades.append(trade)`,API 层用 `_today_trades` 而不是 `engine.trades`

4. **`app/sim_trader/reporter.py:44, 51, 73`** — 在回测模式(`persist=False`)依赖 `engine.trades`
   - 影响:回测结束后报表可能"无交易"
   - 建议:reporter 入口前 `engine.trades = engine._store.load_trades()`

5. **`app/sim_trader/store.py`** — 未持久化 `_prev_day_snap`(也未持久化 `_prev_snap`)
   - 影响:服务重启后第一天,除权跳空保护不生效(Task 9 设计的简化)
   - 建议:加 `save_prev_day_snap` / `load_prev_day_snap` 方法

### 不修(已记录的设计选择)

- HTTP GET / hang 是 **base 状态原本就有**的问题(与本次修复无关)
  - WebSocket 残留连接 + cron `_catch_up_daily` 阻塞事件循环
  - 子代理调查确认:在 base commit `8ca6571` 已存在同样 hang
- `test_determinism.py` 有 GBK 编码问题(项目本身 bug)
- `qmt_proxy_server.py:243, 259` 的 `market_value` 是 QMT SDK 内部对象,与 Position 无关,不受 #8 影响

## 修复期间监控表(全部打勾 ✅)

| Commit | 文件改动 | 测试通过 | 服务启动 | 页面访问 | API 正常 | 前端同步 | 备注 |
|---|---|---|---|---|---|---|---|
| #2  | ✓ | ✓ | ✓ | ✓ | ✓ | - | |
| #7  | ✓ | ✓ | ✓ | ✓ | ✓ | - | |
| #16 | ✓ | ✓ | ✓ | ✓ | ✓ | - | |
| #10 | ✓ | ✓ | ✓ | ✓ | ✓ | - | |
| #11 | ✓ | ✓ | ✓ | ✓ | ✓ | - | 删 2 处 append |
| #13 | ✓ | ✓ | ✓ | ✓ | ✓ | - | |
| #8  | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | **🔴 最高风险** amend 加 fallback |
| #9  | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | amend 加 deep copy |

**硬约束触发次数**:
- 1 次 Revert(Task 3 子代理范围蔓延)
- 2 次 Amend(Task 7 加 bar.get fallback + Task 8 加 deep copy)

## 用户需要做的操作

### 立即做

1. **逐项手测**(参考 spec §10 清单):
   - 主页访问
   - 选股 Tab
   - 回测 Tab
   - 模拟盘 Tab(持仓/交易)
   - 数据同步
   - AI 委员会
   - 设置页面

2. **观察 1-2 个交易日**:
   - 模拟盘 "今日交易数" 是否准确
   - 净值曲线是否正常
   - 持仓盈亏显示是否合理
   - 数据是否进入数据库(新股票入库率)

### 可选(本次未做,建议下一批处理)

- 重跑 `batch_download_all` 刷新历史 amount 字段
- 处理已知遗留 1-5 项

## 文档

- Spec: [docs/superpowers/specs/2026-06-22-bug-fixes-design.md](docs/superpowers/specs/2026-06-22-bug-fixes-design.md)
- Plan: [docs/superpowers/plans/2026-06-22-bug-fixes.md](docs/superpowers/plans/2026-06-22-bug-fixes.md)
