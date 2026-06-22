# Bug 修复设计 Spec v2:5 个已知遗留问题

> **日期**:2026-06-22
> **作者**:Claude
> **范围**:5 个 spec 范围外但同根源的 bug
> **状态**:已获用户批准,等待 review
> **Spec 上下文**: [docs/superpowers/specs/2026-06-22-bug-fixes-design.md](2026-06-22-bug-fixes-design.md) (上批 8-bug 修复)

---

## 0. 硬约束(沿用上批)

- **零新问题**: 修 bug 不能引入新问题
- **业务连续性**: 用户修复期间暂停止损/实盘/模拟盘自动交易
- **5 步强制验证**: 每个 commit 必跑(测试+回归+服务+API+手测)
- **范围纪律**: 严格不超范围,范围外发现记入 CHANGELOG

---

## 1. 背景:5 个已知遗留(本批修复目标)

[CHANGELOG-2026-06-22.md](../CHANGELOG-2026-06-22.md) 列出的 5 个同根源 bug:

| # | 来源 | 严重度 | 真实状态 | 文件 |
|---|---|---|---|---|
| L1 | #7 sibling | 🟠 | `backfill_daily_tushare.py:55` 仍有 `df['amount'] = (df['amount'] * 1000)` | [scripts/backfill_daily_tushare.py:55](../scripts/backfill_daily_tushare.py) |
| L2 | #10 sibling | 🟠 | 3 处 `model="deepseek-v4-pro"` 残留(DeepSeek 实际不存在此模型) | [app/agents/concept_miner.py:100](../app/agents/concept_miner.py), [app/agents/stock_analyst.py:47](../app/agents/stock_analyst.py), [app/backtest/llm_advisor.py:144](../app/backtest/llm_advisor.py) |
| L3 | #11 sideeffect | 🔴 | `len(engine.trades)` 算"今日交易数",Task 11 修复后运行时新增 trade 不再进内存 → 前端 WebSocket 推送会"突然归零" | [app/api/sim_trader.py:253,306](../app/api/sim_trader.py), [app/scheduler/cron_jobs.py:440](../app/scheduler/cron_jobs.py) |
| L4 | #11 sideeffect | 🟠 | reporter.py 在 `persist=False` 模式依赖 `engine.trades`,回测报表"无交易" | [app/sim_trader/reporter.py:44](../app/sim_trader/reporter.py), [app/sim_trader/main.py:61](../app/sim_trader/main.py) |
| L5 | #9 sibling | 🟠 | store.py 未持久化 `_prev_day_snap` 和 `_prev_snap`,服务重启后第一天除权跳空保护不生效 | [app/sim_trader/store.py](../app/sim_trader/store.py), [app/sim_trader/engine.py:144-149](../app/sim_trader/engine.py) |

**前置核对**: 全部 5 个 L 都已读源码验证,真实存在。

---

## 2. 修复策略

### 2.1 5 个 Task 按风险分组

| 域 | Task | 简述 | 风险 | Commit 顺序 |
|---|---|---|---|---|
| **A 数据** | 9 (L1) | `backfill_daily_tushare.py` 删 `* 1000` | 低 | A1 |
| **B Agent** | 10 (L2) | 3 处 `deepseek-v4-pro` → `deepseek-chat` | 低 | B1 |
| **C 模拟盘** | 11 (L3) | 维护 `self._today_trades`,API 改用 | **中** | C1 |
| | 12 (L4) | engine 加 `refresh_trades_from_store()`,reporter 入口调用 | 低 | C2 |
| | 13 (L5) | store 持久化 `_prev_day_snap`(复用 sim_state 表 + JSON value) | **中** | C3 |
| **D 回归** | 5 个 test_fix_*.py | | 低 | 穿插 |

**C 严格顺序**: C1 → C2 → C3
- C1 先解决"今日交易数"虚高/归零
- C2 修回测报表
- C3 持久化 prev_day_snap(可独立)

### 2.2 关键设计原则(沿用上批)

1. 零行为回归
2. 修复域内聚
3. 测试隔离(`scripts/test_fix_*.py` 独立可跑)
4. **不修范围外** — L 范围外的发现仍记入 CHANGELOG

---

## 3. 域 A:数据层(L1)

### Task 9 (L1) - `backfill_daily_tushare.py` 删 `* 1000`

**改动**:`[scripts/backfill_daily_tushare.py:55](../scripts/backfill_daily_tushare.py)` 删 `* 1000` + 加注释

```python
- df['amount'] = (df['amount'] * 1000).fillna(df['close'] * df['volume'] * 100)
+ # Tushare amount 字段单位是元(与 #7 同根因)
+ df['amount'] = df['amount'].fillna(df['close'] * df['volume'] * 100)
```

**新问题预演**:
- `df['amount']` 直接 fillna 行为变化:原代码 `NaN → 0`(因为 `* 1000 * NaN = NaN` 再 fillna)→ 新代码 `NaN → close * volume * 100`(直接 fillna)
- 但 `df['close']` 列本身被 `dropna(subset=['close'])` 过滤了(line 56),所以**实际 NaN 只发生在 amount 列**,fillna 行为等价

**测试**:`scripts/test_fix_09.py`
- 验证脚本源码无 `* 1000`(防回归)
- 验证 amount 数值不被放大

---

## 4. 域 B:Agent 层(L2)

### Task 10 (L2) - 3 处 `deepseek-v4-pro` → `deepseek-chat`

**改动**:3 个文件各改 1 行

| 文件 | 行 | 改动 |
|---|---|---|
| `app/agents/concept_miner.py` | 100 | `model="deepseek-v4-pro"` → `model="deepseek-chat"` |
| `app/agents/stock_analyst.py` | 47 | `model="deepseek-v4-pro"` → `model="deepseek-chat"` |
| `app/backtest/llm_advisor.py` | 144 | `model: str = "deepseek-v4-pro"` → `model: str = "deepseek-chat"` |

**新问题预演**:
- 这 3 处的 key 处理逻辑(已有 None / log warning)是否健全? → grep 验证,**有缺陷则记入 CHANGELOG,不修**(YAGNI)
- `llm_advisor.py:144` 是函数默认参数,改默认值不影响已有调用

**测试**:`scripts/test_fix_10.py`
- grep 3 个文件源码无 `deepseek-v4-pro` 残留
- grep 全 app/ 验证全部清空
- 注:`app/agents/stock_analyst.py` 已有正确 key 处理(已有 log.warning + None),仅改模型名

---

## 5. 域 C:模拟盘核心(L3/L4/L5)

### Task 11 (L3) - 维护 `self._today_trades` + 改 API 调用

**思路**:Task 11 上批删了两处 `self.trades.append` 避免双路径重复,副作用是运行时 `self.trades` 不再增长。修复:在 `execute_sell` 内部维护 `self._today_trades`(只增不删,日切时清空),API 层用它算"今日交易数"。

**改动 1**:`[app/sim_trader/engine.py](../app/sim_trader/engine.py)` `SimTraderEngine.__init__`:

```python
+ self._today_trades: List[Trade] = []  # 当日新增 trades(L3 修复)
```

**改动 2**:`[app/sim_trader/engine.py](../app/sim_trader/engine.py)` `execute_sell` 内部,在 `save_trade(trade)` 之后:

```python
if self._store:
    self._store.save_trade(trade)
    ...
+ # 维护当日 trades 列表(供 API 算"今日交易数"),L3 修复
+ self._today_trades.append(trade)
```

**改动 3**:`[app/sim_trader/engine.py](../app/sim_trader/engine.py)` `sell_phase` 开头或 `record()` 中,日切时清空 `_today_trades`:

```python
# 在 record() 开头加(每日 14:56 调一次)
def record(self, today, snapshot):
+   # 日切:清空当日 trades 列表(L3 修复)
+   if self._today_trades and self._today_trades[-1].exit_date < today:
+       self._today_trades = []
    # ... 原有逻辑
```

**改动 4**:`[app/api/sim_trader.py:253](../app/api/sim_trader.py)` 改用 `_today_trades`:

```python
- 'trade_count': len(engine.trades),
+ 'trade_count': len(engine._today_trades),
```

**改动 5**:`[app/api/sim_trader.py:306](../app/api/sim_trader.py)` 改用 `_today_trades`:

```python
- sell_count = len([t for t in engine.trades if t.exit_date == today])
+ sell_count = len([t for t in engine._today_trades if t.exit_date == today])
```

**改动 6**:`[app/scheduler/cron_jobs.py:440](../app/scheduler/cron_jobs.py)` 同样改用 `_today_trades`(一行)

**新问题预演**:
- `_today_trades` 何时清空? → `record()` 日切时
- `record()` 不一定每天调? → spec 已确认 main.py:81 每个交易日 14:56 调一次
- `_today_trades` 与 `engine.trades` 的关系? → `_today_trades ⊆ engine.trades`(当日的子集)
- 冷启动时 `_today_trades = []`,首次 execute_sell 后开始填充
- `execute_sell` 重复被调(模拟盘中+尾盘)会不会双 append? → **不会**,execute_sell 是单次卖出只调一次,`save_trade` 和 `_today_trades.append` 都在它内部

**测试**:`scripts/test_fix_11.py`
- 验证 `execute_sell` 后 `_today_trades` +1
- 验证 sell_phase 内部多次 execute_sell 时,`_today_trades` 正确累加
- 验证 record() 日切时清空
- 验证 API 层 `sim_trader_status` 返回的 trade_count 正确
- 验证 cron_jobs.py:440 引用 `_today_trades` 而非 `engine.trades`

---

### Task 12 (L4) - `refresh_trades_from_store()` + reporter 入口调用

**改动 1**:`[app/sim_trader/engine.py](../app/sim_trader/engine.py)` 新增方法:

```python
def refresh_trades_from_store(self):
    """从 store 重新加载 trades(回测模式 persist=False 仍可工作,L4 修复)"""
    if self._store:
        self.trades = self._store.load_trades()
        self.positions = self._store.load_positions()
        self.equity_curve = self._store.load_equity_curve()
```

**改动 2**:`[app/sim_trader/reporter.py:21](../app/sim_trader/reporter.py)` `final_report` 入口前:

```python
def final_report(engine: SimTraderEngine, trading_dates: List[date]):
+   # L4 修复: 从 store 重新加载 trades(回测模式 persist=False 也有效)
+   engine.refresh_trades_from_store()
    eq_df = pd.DataFrame(engine.equity_curve)
    ...
```

**改动 3**:`[app/sim_trader/reporter.py:21](../app/sim_trader/reporter.py)` `daily_report` 同样调用(虽然 daily_report 当前是 `pass`,但保持一致)

**新问题预演**:
- `refresh_trades_from_store` 是否安全(无 store 时)? → 加 `if self._store:` 保护
- `reporter.py:21` 当前是 `pass`,改它无副作用
- 实际生效需要 main.py:61 调用 `SimTraderEngine(persist=False)` 的回测流程 → **先确认 main.py:61 实际是 `SimTraderEngine(store=...)` 还是 `SimTraderEngine(persist=False)`** — 如果是 `SimTraderEngine(store=store)`,reporter 入口加 refresh 就够了;如果是 `persist=False`,需要 main.py 也改

**测试**:`scripts/test_fix_12.py`
- 验证 `refresh_trades_from_store` 在有 store 时从 store 加载
- 验证 reporter.py 源码引用 `refresh_trades_from_store`
- 验证 main.py 实际调用模式

---

### Task 13 (L5) - store 持久化 `_prev_day_snap`(复用 sim_state 表)

**思路**:复用现有 `sim_state` 表的 `key-value` 模式,加 `prev_day_snap` key,value 是 JSON 文本。

**改动 1**:`[app/sim_trader/store.py](../app/sim_trader/store.py)` 新增方法(SimTraderStore DuckDB 版):

```python
def save_prev_day_snap(self, snap: dict):
    """持久化 _prev_day_snap(供下次启动后第一天使用),L5 修复"""
    import json
    self.conn.execute(
        "INSERT OR REPLACE INTO sim_state (key, value) VALUES (?, ?)",
        ['prev_day_snap', json.dumps(snap, default=str)]
    )
    self.conn.commit()

def load_prev_day_snap(self) -> dict:
    """加载 _prev_day_snap(冷启动后用)"""
    import json
    row = self.conn.execute(
        "SELECT value FROM sim_state WHERE key = 'prev_day_snap'"
    ).fetchone()
    if row and row[0]:
        return json.loads(row[0])
    return {}
```

**改动 2**:`[app/sim_trader/store.py](../app/sim_trader/store.py)` 同样加 JsonSimStore 版(若有):

```python
class JsonSimStore:
    def save_prev_day_snap(self, snap: dict):
        self._data['prev_day_snap'] = snap
        self._save()

    def load_prev_day_snap(self) -> dict:
        return self._data.get('prev_day_snap', {})
```

**改动 3**:`[app/sim_trader/engine.py](../app/sim_trader/engine.py)` `__init__` 加载:

```python
+ # L5 修复: 冷启动加载 _prev_day_snap
+ if store is not None:
+     self._prev_day_snap = self._store.load_prev_day_snap() or {}
+ else:
+     self._prev_day_snap = {}
```

**改动 4**:`[app/sim_trader/engine.py](../app/sim_trader/engine.py)` `sell_phase` 末尾同步保存:

```python
+ # L5 修复: 同步持久化 _prev_day_snap
+ if self._store:
+     self._store.save_prev_day_snap(self._prev_day_snap)
```

**新问题预演**:
- `sim_state` 表的 key 命名规范? → 已有 `state` key,加 `prev_day_snap` 不冲突
- `json.dumps` 时 `dict` 含 `date` 对象? → 用 `default=str` 兜底
- 冷启动时 `load_prev_day_snap` 返回空 dict → `_prev_day_snap = {}`,第一天不触发保护(可接受,与上批设计一致)
- 写盘频率:每次 sell_phase 末尾都写 → 1 天 1 次,可接受

**测试**:`scripts/test_fix_13.py`
- 验证 `save_prev_day_snap` 写入 sim_state
- 验证 `load_prev_day_snap` 读回
- 验证 engine __init__ 加载 _prev_day_snap
- 验证 sell_phase 末尾保存 _prev_day_snap
- 验证 roundtrip(写后读,值一致)

---

## 6. 强制 5 步验证(沿用上批)

```
Step 1: 自己的测试:python scripts/test_fix_XX.py
Step 2: 全部 test_fix_*.py(本批 5 个 + 上批 8 个 = 13 个)
Step 3: 现有回归:python scripts/test_simple_runner.py
Step 4: 服务启动 + 主页 200(注意 base 已知 hang)
Step 5: 浏览器手测涉及到的页面
```

---

## 7. 已知范围外(明确不修,YAGNI)

- ❌ 不改 `scripts/_deprecated/` 下任何文件
- ❌ 不动 HTTP GET / hang(base 已知问题,与本批无关)
- ❌ 不重写 `_today_trades` 持久化(本次只持久化 `_prev_day_snap`,`_today_trades` 仍内存)
- ❌ 不优化 `llm_advisor.py` 的 key 校验逻辑(只改模型名)
- ❌ 不为 `concept_miner.py` 加 try/except(只改模型名)

---

## 8. 文档产出

修复完成后,生成 `[CHANGELOG-2026-06-23.md](../CHANGELOG-2026-06-23.md)`,列出:
- 5 个 L 修复 commit hash
- 任何新发现的已知遗留
- 监控表(全勾)

---

## 9. 收尾

- **不调** `finishing-a-development-branch` skill(因为这是持续开发的项目,不是 PR 收尾)
- CHANGELOG 写完后,提示用户做最终手测
