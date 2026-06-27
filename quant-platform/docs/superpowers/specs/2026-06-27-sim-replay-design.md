# 模拟盘 QUANTQQ 数据灌入 Spec

> 日期: 2026-06-27
> 项目: quant-platform 交易控制 TAB
> 范围: 一次性脚本 → SimTraderStore → 前端自动展示

---

## 0. 背景

### 需求

让"交易控制"TAB 展示 QUANTQQ + 5分钟线 + 2026.1.1~今天的完整模拟盘结果。这是一次性数据构建，不需要交互式回放。

### 现状

| 组件 | 能做什么 |
|------|---------|
| `app/backtest/tdx_runner.run_tdx_backtest()` | QUANTQQ + 5m 全市场回测，返回 trades/equity/summary |
| `app/sim_trader/store.py` | DuckDB 持久化（sim_trades / sim_equity / sim_state 表） |
| `app/sim_trader/engine.py` | 启动时从 store `load_trades()` + `load_equity_curve()` 恢复 |
| `app/api/sim_trader.py` | GET trades/equity/status → 前端自动渲染 |
| 前端"交易控制"TAB | 净值图/持仓/交易记录/日历/盈亏分析，全部从 API 读 |

**差距**：缺少一条通路，把 `run_tdx_backtest` 的结果灌入 `SimTraderStore`。

---

## 1. 方案

一个独立脚本，跑一次即可。

```
scripts/populate_sim_quantqq.py
  │
  ├─ ① 调用 run_tdx_backtest(QUANTQQ, 5m, 2026.1.1~today)
  │      → 复用 TDX 桥接、5m K线回放、统一止盈止损规则
  │
  ├─ ② 格式转换（simple_runner.Trade → sim_trader.Trade）
  │
  ├─ ③ 写入 SimTraderStore (DuckDB)
  │      → 清空旧数据 → 逐笔写交易 → 逐日写净值 → 写终态
  │
  └─ ④ 打印摘要
```

### 不改动的组件（零风险）

- `app/backtest/tdx_runner.py` — 纯调用
- `app/sim_trader/engine.py` — 不碰
- `app/sim_trader/store.py` — 不碰
- `app/api/sim_trader.py` — 不碰
- `static/index.html` — 不碰
- `static/js/main.js` — 不碰

---

## 2. 详细设计

### 2.1 文件

**新建**: `scripts/populate_sim_quantqq.py`

### 2.2 参数

```python
# 全部从 config.py 读取，不硬编码
START_DATE = date(2026, 1, 1)
END_DATE   = date.today()
STRATEGY   = "QUANTQQ"
PERIOD     = "5m"
```

### 2.3 流程

```
① 读 config
     from app.sim_trader.config import INITIAL_CAPITAL, POSITION_SIZE, ...

② 调 run_tdx_backtest
     params = {
         strategy_name: "QUANTQQ",
         strategy_type: "tdx",
         intraday_freq: "5m",
         start_date: date(2026, 1, 1),
         end_date: date.today(),
         ...  # 风控/仓位参数全从 config 读
     }
     result = run_tdx_backtest(params)

③ 格式转换 (simple_runner.Trade → sim_trader.Trade)
     for t in result["trades"]:
         trade = Trade(
             code=t.code,
             entry_date=t.entry_date,
             exit_date=t.exit_date,
             entry_price=t.entry_px,    # ← 字段名映射
             exit_price=t.exit_px,
             shares=t.shares,
             return_pct=t.ret,
             profit_amount=t.profit,
             exit_reason=t.reason,
             hold_days=t.hold,
             entry_reason=STRATEGY,      # ← 脚本补充
             exit_timing="close",
             entry_time=getattr(t, 'entry_time', '09:30'),
             exit_time=getattr(t, 'exit_time', '15:00'),
         )

④ 写入 SimTraderStore
     store = SimTraderStore()
     store.clear_all()
     for trade in converted_trades:
         store.save_trade(trade)
     for point in result["equity"]:
         store.save_equity_point(
             date.fromisoformat(point["date"]),
             point["equity"], point["cash"], point["pos"],
         )
     store.save_state(
         cash=cash_end,
         consecutive_losses=0,
         pause_until=None,
         trade_count=len(converted_trades),
     )

⑤ 打印摘要
     print(f"写入完成：{len(converted_trades)} 笔交易, "
           f"{len(result['equity'])} 个交易日")
     print(f"收益率: {summary['total_return']:+.2f}%  "
           f"胜率: {summary['win_rate']:.1f}%  "
           f"最大回撤: {summary['max_drawdown']:.2f}%")
```

### 2.4 字段映射

| 源 (simple_runner.Trade) | 目标 (sim_trader.Trade) | 说明 |
|--------------------------|------------------------|------|
| `t.code` | `t.code` | 直通 |
| `t.entry_date` | `t.entry_date` | 直通 |
| `t.exit_date` | `t.exit_date` | 直通 |
| `t.entry_px` | `t.entry_price` | 字段名映射 |
| `t.exit_px` | `t.exit_price` | 字段名映射 |
| `t.shares` | `t.shares` | 直通 |
| `t.ret` | `t.return_pct` | 字段名映射 |
| `t.profit` | `t.profit_amount` | 字段名映射 |
| `t.reason` | `t.exit_reason` | 字段名映射 |
| `t.hold` | `t.hold_days` | 字段名映射 |
| — | `"QUANTQQ"` | 脚本补充 |
| — | `"close"` | 默认 |
| `t.entry_time` | `t.entry_time` | 直通 |
| `t.exit_time` | `t.exit_time` | 直通 |

### 2.5 SimTraderStore 需要补充的方法

`store.py` 缺少 `clear_all()`。当前只有各表独立的 `DELETE FROM` 写在注释里但没封装。新增一个方法：

```python
def clear_all(self):
    """清空所有模拟盘数据（交易/净值/持仓/状态）"""
    for table in ('sim_positions', 'sim_trades', 'sim_equity', 'sim_state'):
        self.conn.execute(f"DELETE FROM {table}")
    try:
        self.conn.execute("DROP SEQUENCE IF EXISTS sim_trade_id")
    except Exception:
        pass
    self._ensure_tables()  # 重建序列
```

这个方法加在 `SimTraderStore` 类末尾，不改变现有接口签名。

### 2.6 使用方式

```bash
# 确保通达信已启动并登录 → 然后：
python scripts/populate_sim_quantqq.py
```

依赖：通达信客户端（TDX bridge 需要）

---

## 3. 错误处理

| 场景 | 处理 |
|------|------|
| TDX bridge 不可用 | `run_tdx_backtest` 返回 `status: "error"`，脚本 exit 1 + 打印错误信息 |
| 5m 数据不可用 | tdx_runner 内部自动降级（1m→5m→daily） |
| 区间内无信号 | 写入 0 条交易，前端显示空 |
| DuckDB 写入失败 | try/except 打印错误，不写残缺数据 |

---

## 4. 改动清单

| 文件 | 改动 |
|------|------|
| `scripts/populate_sim_quantqq.py` | **新建**，约 80 行 |
| `app/sim_trader/store.py` | 新增 `clear_all()` 方法，约 10 行 |

**不改动的文件**：
- `app/backtest/tdx_runner.py`
- `app/sim_trader/engine.py`
- `app/sim_trader/config.py`
- `app/api/sim_trader.py`
- 所有前端文件

---

## 5. 验证

| 步骤 | 预期 |
|------|------|
| 运行脚本 | 终端打印进度 + 最终摘要 |
| 打开前端"交易控制"TAB | 净值图显示 2026.1~6 曲线 |
| 查看交易记录 | 列出所有 QUANTQQ 买卖 |
| 刷新页面 | 数据持久化，仍在 |
| 与 CLI 对比 | `scripts/run_quantqq_backtest.py` 结果一致 |

---

## 6. 架构图

```
┌─────────────────────────────────────────────────────────┐
│                scripts/populate_sim_quantqq.py           │
│                      （唯一新增文件）                      │
│                                                         │
│  run_tdx_backtest()  ────→  格式转换  ────→  Store      │
│  (不修改，纯调用)           (字段名映射)       (新增       │
│                                              clear_all) │
└─────────────────────────────────────────────────────────┘
        │                                              │
        ▼                                              ▼
┌──────────────┐                          ┌──────────────────────┐
│ tdx_runner   │                          │   SimTraderStore     │
│ (零改动)      │                          │   (DuckDB)           │
│              │                          │                      │
│ TdxBridge    │                          │ sim_trades  ← 交易   │
│ FastEngine   │                          │ sim_equity  ← 净值   │
│ exit_rules   │                          │ sim_state   ← 终态   │
└──────────────┘                          └──────────┬───────────┘
                                                     │
                                                     ▼
                                          ┌──────────────────────┐
                                          │ SimTraderEngine      │
                                          │ (启动时 load 恢复)    │
                                          └──────────┬───────────┘
                                                     │
                                                     ▼
                                          ┌──────────────────────┐
                                          │ 前端"交易控制"TAB     │
                                          │ (零改动，自动展示)     │
                                          └──────────────────────┘
```
