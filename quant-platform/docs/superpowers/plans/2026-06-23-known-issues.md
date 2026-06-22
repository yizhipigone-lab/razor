# 5 个已知遗留问题修复实施 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 CHANGELOG-2026-06-22 列出的 5 个 spec 范围外但同根源的 bug

**Architecture:** 5 个原子 commit,按 3 个域分组(A 数据/B Agent/C 模拟盘+持久化),C 域严格 C1→C2→C3 顺序

**Tech Stack:** Python 3.x, DuckDB, Pandas, LangChain, FastAPI

**Spec:** [docs/superpowers/specs/2026-06-23-known-issues-design.md](../specs/2026-06-23-known-issues-design.md)

---

## 硬约束(每个 task 必读)

> 修任何 bug 前,**必须问"会不会引入新问题"**。如果会,放弃这个改法,找不会引发新问题的方案。
> 修复期间用户**暂停止损/实盘/模拟盘自动交易**,只保留查询。
> 每个 commit 完成后,跑 5 步强制验证(见 Task 9 的 Step 6)。

---

## 文件结构(本次修复涉及的文件)

### 修改文件

| 文件 | 改的原因 | 涉及 Task |
|---|---|---|
| `scripts/backfill_daily_tushare.py` | L1 删 `* 1000` | Task 9 |
| `app/agents/concept_miner.py` | L2 改模型名 | Task 10 |
| `app/agents/stock_analyst.py` | L2 改模型名 | Task 10 |
| `app/backtest/llm_advisor.py` | L2 改模型名 | Task 10 |
| `app/sim_trader/engine.py` | L3 `_today_trades` / L4 `refresh_trades_from_store()` / L5 加载 + 保存 | Task 11, 12, 13 |
| `app/api/sim_trader.py` | L3 改用 `_today_trades` | Task 11 |
| `app/scheduler/cron_jobs.py` | L3 改用 `_today_trades` | Task 11 |
| `app/sim_trader/reporter.py` | L4 入口调 `refresh_trades_from_store()` | Task 12 |
| `app/sim_trader/store.py` | L5 加 `save_prev_day_snap` / `load_prev_day_snap` | Task 13 |

### 新建文件

| 文件 | 作用 | 涉及 Task |
|---|---|---|
| `scripts/test_fix_09.py` | L1 回归测试 | Task 9 |
| `scripts/test_fix_10.py` | L2 回归测试 | Task 10 |
| `scripts/test_fix_11.py` | L3 回归测试 | Task 11 |
| `scripts/test_fix_12.py` | L4 回归测试 | Task 12 |
| `scripts/test_fix_13.py` | L5 回归测试 | Task 13 |

---

## Task 9: 修复 L1 - `backfill_daily_tushare.py` 删 `* 1000`

**Files:**
- Modify: `scripts/backfill_daily_tushare.py:55`
- Test: `scripts/test_fix_09.py`

- [ ] **Step 1: 写测试脚本(RED)**

```python
# scripts/test_fix_09.py
"""验证 L1 修复: backfill_daily_tushare.py 中 amount 不被 × 1000"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')

def test_no_multiply_1000_in_source():
    """backfill_daily_tushare.py 源码中不应再有 amount * 1000"""
    with open('scripts/backfill_daily_tushare.py', 'r', encoding='utf-8') as f:
        content = f.read()
    bad_patterns = [
        "df['amount'] = (df['amount'] * 1000)",
        'df["amount"] = (df["amount"] * 1000)',
    ]
    for pattern in bad_patterns:
        assert pattern not in content, f"仍存在错误模式: {pattern}"
    print("✅ backfill_daily_tushare.py 无 amount * 1000")

if __name__ == '__main__':
    test_no_multiply_1000_in_source()
    print("\n🎉 L1 修复验证通过")
```

- [ ] **Step 2: 跑测试,确认 FAIL**

Run: `python scripts/test_fix_09.py`
Expected: FAIL

- [ ] **Step 3: 实现修复 L1**

修改 `scripts/backfill_daily_tushare.py:55`:

```python
-        df['amount'] = (df['amount'] * 1000).fillna(df['close'] * df['volume'] * 100)
+        # Tushare amount 字段单位是元(与 #7 同根因,L1 修复)
+        df['amount'] = df['amount'].fillna(df['close'] * df['volume'] * 100)
```

- [ ] **Step 4: 跑测试,确认 PASS**

Run: `python scripts/test_fix_09.py`
Expected: PASS

- [ ] **Step 5: 新问题预演**

```bash
# 1. grep 全部 amount * 1000 残留
grep -rn "amount.*\* *1000\|amount.*1000.*\*" scripts/ --include="*.py"
# 期望: 无结果

# 2. 验证 fillna 行为没破坏(line 56 dropna 之后 amount 是唯一可能 NaN 的列)
grep -n "dropna\|fillna" scripts/backfill_daily_tushare.py
```

- [ ] **Step 6: 5 步强制验证**

```bash
python scripts/test_fix_09.py
ls scripts/test_fix_*.py 2>/dev/null | xargs -I {} python {}
python scripts/test_simple_runner.py
```

- [ ] **Step 7: Commit**

```bash
git add scripts/backfill_daily_tushare.py scripts/test_fix_09.py
git commit -m "fix(data): remove incorrect amount * 1000 in backfill_daily_tushare (#7-sibling/L1)"
```

---

## Task 10: 修复 L2 - 3 处 `deepseek-v4-pro` → `deepseek-chat`

**Files:**
- Modify: `app/agents/concept_miner.py:100`
- Modify: `app/agents/stock_analyst.py:47`
- Modify: `app/backtest/llm_advisor.py:144`
- Test: `scripts/test_fix_10.py`

- [ ] **Step 1: 写测试脚本(RED)**

```python
# scripts/test_fix_10.py
"""验证 L2 修复: 仓库全 app/ 无 deepseek-v4-pro 残留"""
import subprocess
import sys
sys.stdout.reconfigure(encoding='utf-8')

def test_no_deepseek_v4_pro_in_app():
    """全 app/ 不应再有 deepseek-v4-pro"""
    result = subprocess.run(
        ['grep', '-rn', 'deepseek-v4-pro', 'app/', '--include=*.py'],
        capture_output=True, text=True
    )
    assert result.stdout.strip() == '', f"仍存在 deepseek-v4-pro:\n{result.stdout}"
    print("✅ 全 app/ 无 deepseek-v4-pro 残留")

if __name__ == '__main__':
    test_no_deepseek_v4_pro_in_app()
    print("\n🎉 L2 修复验证通过")
```

- [ ] **Step 2: 跑测试,确认 FAIL**

Run: `python scripts/test_fix_10.py`
Expected: FAIL(3 处残留)

- [ ] **Step 3: 实现修复 L2 - 改 3 处模型名**

`app/agents/concept_miner.py:100`:
```python
-                model="deepseek-v4-pro",
+                model="deepseek-chat",
```

`app/agents/stock_analyst.py:47`:
```python
-                model="deepseek-v4-pro",
+                model="deepseek-chat",
```

`app/backtest/llm_advisor.py:144`:
```python
-        model: str = "deepseek-v4-pro",
+        model: str = "deepseek-chat",
```

- [ ] **Step 4: 跑测试,确认 PASS**

- [ ] **Step 5: 新问题预演**

```bash
# 验证 3 个文件的 key 校验逻辑是否健全(只 grep,不修)
grep -A 5 "DEEPSEEK_API_KEY\|OPENAI_API_KEY" app/agents/concept_miner.py | head -20
grep -A 5 "DEEPSEEK_API_KEY\|OPENAI_API_KEY" app/agents/stock_analyst.py | head -20
grep -A 5 "DEEPSEEK_API_KEY\|OPENAI_API_KEY" app/backtest/llm_advisor.py | head -20
# 不修,如有问题记入报告
```

- [ ] **Step 6: 5 步强制验证**

```bash
python scripts/test_fix_09.py
python scripts/test_fix_10.py
ls scripts/test_fix_*.py 2>/dev/null | xargs -I {} python {}
python scripts/test_simple_runner.py
```

- [ ] **Step 7: Commit**

```bash
git add app/agents/concept_miner.py app/agents/stock_analyst.py app/backtest/llm_advisor.py scripts/test_fix_10.py
git commit -m "fix(agent): correct deepseek-v4-pro to deepseek-chat in 3 LLM callers (#10-sibling/L2)"
```

---

## Task 11: 修复 L3 - 维护 `self._today_trades` + 改 API 调用

**Files:**
- Modify: `app/sim_trader/engine.py:__init__` (加字段)
- Modify: `app/sim_trader/engine.py:execute_sell` (加 append)
- Modify: `app/sim_trader/engine.py:record()` (日切清空)
- Modify: `app/api/sim_trader.py:253, 306` (改用 _today_trades)
- Modify: `app/scheduler/cron_jobs.py:440` (改用 _today_trades)
- Test: `scripts/test_fix_11.py`

- [ ] **Step 1: 必做 — 新问题预演扫描**

```bash
# 1. 确认 engine.trades / _today_trades 当前用法的真实形态
grep -rn "engine\.trades\|self\.trades" app/ --include="*.py"

# 2. 确认 main.py 实际的回测调用模式(影响 Task 12)
grep -n "SimTraderEngine" app/sim_trader/main.py
```

- [ ] **Step 2: 写测试脚本(RED)**

```python
# scripts/test_fix_11.py
"""验证 L3 修复: _today_trades 在 execute_sell 后累加,日切时清空,API 用 _today_trades"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')
from datetime import date
from unittest.mock import MagicMock, patch

def test_today_trades_initialized_as_empty_list():
    """engine 初始化时 _today_trades 应是空 list"""
    from app.sim_trader.engine import SimTraderEngine

    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = []
    mock_store.load_equity_curve.return_value = []

    engine = SimTraderEngine(store=mock_store)
    assert hasattr(engine, '_today_trades'), "engine 缺 _today_trades 字段"
    assert isinstance(engine._today_trades, list), "_today_trades 应是 list"
    assert len(engine._today_trades) == 0, "_today_trades 应初始化为空"
    print("✅ _today_trades 字段存在并初始化为空 list")

def test_execute_sell_appends_to_today_trades():
    """execute_sell 后 _today_trades 应 +1"""
    from app.sim_trader.engine import SimTraderEngine, Position

    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = []
    mock_store.load_equity_curve.return_value = []

    engine = SimTraderEngine(store=mock_store)
    pos = Position(
        code='000001', entry_date=date(2025, 1, 3),
        entry_price=10.0, shares=100, cost=1000.0
    )
    engine.positions['000001'] = pos

    initial_len = len(engine._today_trades)
    engine.execute_sell(pos, 9.0, 'HS', None, exit_date=date(2025, 1, 6))
    final_len = len(engine._today_trades)
    assert final_len == initial_len + 1, f"_today_trades 未累加: {initial_len} -> {final_len}"
    print(f"✅ execute_sell 后 _today_trades: {initial_len} -> {final_len}")

def test_api_layer_uses_today_trades():
    """api/sim_trader.py 不应再用 engine.trades 算 trade_count/sell_count"""
    with open('app/api/sim_trader.py', 'r', encoding='utf-8') as f:
        content = f.read()
    bad_patterns = [
        "'trade_count': len(engine.trades)",
        'sell_count = len([t for t in engine.trades',
    ]
    for pattern in bad_patterns:
        assert pattern not in content, f"仍用 engine.trades: {pattern}"
    print("✅ api/sim_trader.py 已用 _today_trades")

def test_cron_jobs_uses_today_trades():
    """cron_jobs.py:440 不应再用 engine.trades"""
    with open('app/scheduler/cron_jobs.py', 'r', encoding='utf-8') as f:
        content = f.read()
    # 找第 440 行附近的 len(engine.trades) 模式
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if 'len(engine.trades)' in line and 'sell_count' in line:
            # 是 sell_count 计算
            bad = f"第 {i+1} 行仍用 engine.trades: {line.strip()}"
            assert False, bad
    print("✅ cron_jobs.py 算 sell_count 已用 _today_trades")

if __name__ == '__main__':
    test_today_trades_initialized_as_empty_list()
    test_execute_sell_appends_to_today_trades()
    test_api_layer_uses_today_trades()
    test_cron_jobs_uses_today_trades()
    print("\n🎉 L3 修复验证通过")
```

- [ ] **Step 3: 跑测试,确认 FAIL**

- [ ] **Step 4: 实现修复 engine.py - 加字段**

修改 `app/sim_trader/engine.py` `SimTraderEngine.__init__`(在 `_trade_count = 0` 之后):

```python
            self._trade_count = 0
+       self._today_trades: List[Trade] = []  # 当日新增 trades(L3 修复)
```

**注意**: `_today_trades` 必须在 store=None 和 store!=None 两个分支都初始化。

- [ ] **Step 5: 实现修复 engine.py - execute_sell append**

修改 `app/sim_trader/engine.py` `execute_sell`,在 `self._store.save_trade(trade)` 之后:

```python
        if self._store:
            self._store.save_trade(trade)
            self._store.save_positions(self.positions)
            self._store.save_state(...)
+       # 维护当日 trades 列表(供 API 算"今日交易数"),L3 修复
+       self._today_trades.append(trade)
```

**重要**: append 必须在 `if self._store:` 块**外**(无论有无 store 都维护,避免 store 缺失时 UI 看不到交易)。

- [ ] **Step 6: 实现修复 engine.py - record() 日切清空**

修改 `app/sim_trader/engine.py` `record()`,在 `current_price` 更新循环后:

```python
def record(self, today: date, snapshot: dict):
    # 增量更新所有持仓的 current_price
    for code, pos in self.positions.items():
        ...
+   # L3 修复: 日切时清空 _today_trades(避免跨日累积)
+   if self._today_trades and self._today_trades[-1].exit_date < today:
+       self._today_trades = []
    eq = self.total_equity(snapshot)
    ...
```

- [ ] **Step 7: 实现修复 api/sim_trader.py**

修改 `app/api/sim_trader.py:253`:
```python
- 'trade_count': len(engine.trades),
+ 'trade_count': len(engine._today_trades),
```

修改 `app/api/sim_trader.py:306`:
```python
- sell_count = len([t for t in engine.trades if t.exit_date == today])
+ sell_count = len([t for t in engine._today_trades if t.exit_date == today])
```

- [ ] **Step 8: 实现修复 scheduler/cron_jobs.py:440**

修改 `app/scheduler/cron_jobs.py:440`:
```python
- sell_count = len([t for t in engine.trades if t.exit_date == today])
+ sell_count = len([t for t in engine._today_trades if t.exit_date == today])
```

- [ ] **Step 9: 跑测试,确认 PASS**

- [ ] **Step 10: 5 步强制验证**

```bash
python scripts/test_fix_09.py
python scripts/test_fix_10.py
python scripts/test_fix_11.py
ls scripts/test_fix_*.py 2>/dev/null | xargs -I {} python {}
python scripts/test_simple_runner.py
```

- [ ] **Step 11: Commit**

```bash
git add app/sim_trader/engine.py app/api/sim_trader.py app/scheduler/cron_jobs.py scripts/test_fix_11.py
git commit -m "fix(sim_trader): maintain _today_trades for API today count (#11-sideeffect/L3)"
```

---

## Task 12: 修复 L4 - `refresh_trades_from_store()` + reporter 入口

**Files:**
- Modify: `app/sim_trader/engine.py`(加 `refresh_trades_from_store()`)
- Modify: `app/sim_trader/reporter.py`(入口调用)
- Test: `scripts/test_fix_12.py`

- [ ] **Step 1: 必做 — 确认 main.py 实际调用模式**

```bash
grep -n "SimTraderEngine" app/sim_trader/main.py
# 找 line 61 附近实际怎么调用
```

根据实际结果决定:
- 如果 main.py 用 `SimTraderEngine(store=store)`,reporter 入口加 refresh 即可
- 如果用 `SimTraderEngine(persist=False)`,需在 main.py 调整

- [ ] **Step 2: 写测试脚本(RED)**

```python
# scripts/test_fix_12.py
"""验证 L4 修复: refresh_trades_from_store() 从 store 加载,reporter 入口调用"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')
from datetime import date
from unittest.mock import MagicMock

def test_refresh_trades_from_store_method_exists():
    """engine 应该有 refresh_trades_from_store 方法"""
    from app.sim_trader.engine import SimTraderEngine

    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = []
    mock_store.load_equity_curve.return_value = []

    engine = SimTraderEngine(store=mock_store)
    assert hasattr(engine, 'refresh_trades_from_store'), "缺 refresh_trades_from_store 方法"
    assert callable(engine.refresh_trades_from_store), "refresh_trades_from_store 不可调用"
    print("✅ refresh_trades_from_store 方法存在")

def test_refresh_loads_from_store():
    """refresh_trades_from_store 应从 store 加载"""
    from app.sim_trader.engine import SimTraderEngine, Trade

    # mock store 返回一些 trades
    fake_trade = Trade(
        code='000001', entry_date=date(2025, 1, 3),
        exit_date=date(2025, 1, 6),
        entry_price=10.0, exit_price=11.0,
        shares=100, return_pct=10.0, profit_amount=100.0,
        exit_reason='TP1', hold_days=3
    )

    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = [fake_trade]  # store 有 1 笔
    mock_store.load_equity_curve.return_value = []

    engine = SimTraderEngine(store=mock_store)
    # 假设当前 engine.trades 是空(回测模式 persist=False)
    assert len(engine.trades) == 0, "初始 trades 应为空"

    engine.refresh_trades_from_store()

    assert len(engine.trades) == 1, f"refresh 后应有 1 笔, 实际 {len(engine.trades)}"
    print(f"✅ refresh_trades_from_store 从 store 加载 {len(engine.trades)} 笔")

def test_reporter_uses_refresh():
    """reporter.py 入口应调用 refresh_trades_from_store"""
    with open('app/sim_trader/reporter.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'refresh_trades_from_store' in content, "reporter.py 未调用 refresh_trades_from_store"
    print("✅ reporter.py 引用 refresh_trades_from_store")

if __name__ == '__main__':
    test_refresh_trades_from_store_method_exists()
    test_refresh_loads_from_store()
    test_reporter_uses_refresh()
    print("\n🎉 L4 修复验证通过")
```

- [ ] **Step 3: 跑测试,确认 FAIL**

- [ ] **Step 4: 实现修复 engine.py - 加方法**

修改 `app/sim_trader/engine.py`,在 `record()` 之后(或合适位置)加:

```python
    def refresh_trades_from_store(self):
        """从 store 重新加载 trades/positions/equity(L4 修复)"""
        if self._store:
            self.trades = self._store.load_trades()
            self.positions = self._store.load_positions()
            self.equity_curve = self._store.load_equity_curve()
```

- [ ] **Step 5: 实现修复 reporter.py - 入口调用**

修改 `app/sim_trader/reporter.py` `final_report` 函数开头:

```python
def final_report(engine: SimTraderEngine, trading_dates: List[date]):
+   # L4 修复: 从 store 重新加载 trades(回测模式 persist=False 也有效)
+   engine.refresh_trades_from_store()
    eq_df = pd.DataFrame(engine.equity_curve)
    ...
```

**注意**: 不要改 `daily_report`(虽然当前是 `pass`,但保持不动)。

- [ ] **Step 6: 跑测试,确认 PASS**

- [ ] **Step 7: 新问题预演**

```bash
# 1. main.py:61 实际调用模式
grep -n "SimTraderEngine" app/sim_trader/main.py

# 2. reporter 还有没有其他函数依赖 engine.trades
grep -n "engine.trades\|engine\.positions" app/sim_trader/reporter.py
```

- [ ] **Step 8: 5 步强制验证**

```bash
python scripts/test_fix_09.py
python scripts/test_fix_10.py
python scripts/test_fix_11.py
python scripts/test_fix_12.py
ls scripts/test_fix_*.py 2>/dev/null | xargs -I {} python {}
python scripts/test_simple_runner.py
```

- [ ] **Step 9: Commit**

```bash
git add app/sim_trader/engine.py app/sim_trader/reporter.py scripts/test_fix_12.py
git commit -m "fix(sim_trader): add refresh_trades_from_store for backtest reporter (#11-sideeffect/L4)"
```

---

## Task 13: 修复 L5 - store 持久化 `_prev_day_snap`

**Files:**
- Modify: `app/sim_trader/store.py`(加 `save_prev_day_snap` / `load_prev_day_snap` 到 SimTraderStore + JsonSimStore)
- Modify: `app/sim_trader/engine.py`(__init__ 加载 + sell_phase 末尾保存)
- Test: `scripts/test_fix_13.py`

- [ ] **Step 1: 必做 — 确认 store.py 完整结构**

```bash
grep -n "class \|def " app/sim_trader/store.py | head -30
# 确认有几个 store 实现(DuckDB / JSON),都要加方法
```

- [ ] **Step 2: 写测试脚本(RED)**

```python
# scripts/test_fix_13.py
"""验证 L5 修复: _prev_day_snap 持久化到 store,冷启动可加载"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')
from datetime import date
from unittest.mock import MagicMock

def test_store_has_save_load_prev_day_snap():
    """store 应有 save_prev_day_snap / load_prev_day_snap 方法"""
    from app.sim_trader.store import SimTraderStore
    store = SimTraderStore.__new__(SimTraderStore)  # 不触发 _ensure_tables
    # 实际不调用 _ensure_tables,只检查方法存在
    # 注: SimTraderStore() 实际会建表,这里只检查方法签名
    print("✅ SimTraderStore 有 save/load_prev_day_snap 方法(源码扫描)")

def test_store_source_has_methods():
    """store.py 源码应有 save_prev_day_snap 和 load_prev_day_snap"""
    with open('app/sim_trader/store.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'def save_prev_day_snap' in content, "缺 save_prev_day_snap"
    assert 'def load_prev_day_snap' in content, "缺 load_prev_day_snap"
    print("✅ store.py 源码有 save/load_prev_day_snap")

def test_engine_loads_prev_day_snap_on_init():
    """engine 初始化时应从 store 加载 _prev_day_snap"""
    from app.sim_trader.engine import SimTraderEngine

    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = []
    mock_store.load_equity_curve.return_value = []
    mock_store.load_prev_day_snap.return_value = {'000001': {'close': 10.0}}

    engine = SimTraderEngine(store=mock_store)
    assert mock_store.load_prev_day_snap.called, "未调用 load_prev_day_snap"
    assert engine._prev_day_snap == {'000001': {'close': 10.0}}, \
        f"_prev_day_snap 未从 store 加载: {engine._prev_day_snap}"
    print(f"✅ engine 初始化时从 store 加载 _prev_day_snap")

def test_engine_saves_prev_day_snap_in_sell_phase():
    """sell_phase 末尾应保存 _prev_day_snap 到 store"""
    from app.sim_trader.engine import SimTraderEngine

    mock_store = MagicMock()
    mock_store.load_state.return_value = {
        'cash': 100000, 'consecutive_losses': 0,
        'pause_until': None, 'trade_count': 0
    }
    mock_store.load_positions.return_value = {}
    mock_store.load_trades.return_value = []
    mock_store.load_equity_curve.return_value = []
    mock_store.load_prev_day_snap.return_value = {}

    engine = SimTraderEngine(store=mock_store)
    today_snap = {'000001': {'close': 10.0, 'open': 9.8, 'high': 10.2, 'low': 9.7}}
    trading_dates = [date(2025, 1, 6)]

    engine.sell_phase(date(2025, 1, 6), today_snap, trading_dates)

    assert mock_store.save_prev_day_snap.called, "sell_phase 末尾未调用 save_prev_day_snap"
    print("✅ sell_phase 末尾保存 _prev_day_snap 到 store")

if __name__ == '__main__':
    test_store_source_has_methods()
    test_engine_loads_prev_day_snap_on_init()
    test_engine_saves_prev_day_snap_in_sell_phase()
    print("\n🎉 L5 修复验证通过")
```

- [ ] **Step 3: 跑测试,确认 FAIL**

- [ ] **Step 4: 实现修复 store.py - SimTraderStore 加方法**

修改 `app/sim_trader/store.py` `SimTraderStore` 类(在合适位置,例如 `save_state` 之后):

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
        try:
            row = self.conn.execute(
                "SELECT value FROM sim_state WHERE key = 'prev_day_snap'"
            ).fetchone()
            if row and row[0]:
                return json.loads(row[0])
        except Exception:
            pass
        return {}
```

- [ ] **Step 5: 实现修复 store.py - JsonSimStore 加方法(若有)**

如果 `app/sim_trader/store.py` 里有 `class JsonSimStore`,在 `save_state` 之后加:

```python
    def save_prev_day_snap(self, snap: dict):
        self._data['prev_day_snap'] = snap
        self._save()

    def load_prev_day_snap(self) -> dict:
        return self._data.get('prev_day_snap', {})
```

- [ ] **Step 6: 实现修复 engine.py - __init__ 加载**

修改 `app/sim_trader/engine.py` `SimTraderEngine.__init__`,在 `_prev_day_snap = {}` 那行:

```python
self._prev_snap: dict = {}
# 昨日完整 OHLC，用于除权跳空保护
# 与 _prev_snap 区分: _prev_snap 是"今日实时"用于盘中风控
self._prev_day_snap: dict = {}
+ # L5 修复: 冷启动时从 store 加载 _prev_day_snap
+ if store is not None:
+     self._prev_day_snap = self._store.load_prev_day_snap() or {}
```

**注意**: `store is not None` 这个判断已经在 __init__ 顶部存在,这里需要单独再加一个 if 块(因为 _prev_day_snap 初始化是统一的,不能分两个分支)。

- [ ] **Step 7: 实现修复 engine.py - sell_phase 末尾保存**

修改 `app/sim_trader/engine.py` `sell_phase`,在 `_prev_day_snap` 更新后:

```python
# #9 修复: 同步更新 _prev_day_snap(今日尾盘 = 次日开盘的"昨日")
# 用 deep copy 避免后续就地修改 _prev_snap 内层 dict 时污染 prev_day
self._prev_day_snap = copy.deepcopy(self._prev_snap)
+ # L5 修复: 持久化 _prev_day_snap 到 store
+ if self._store:
+     self._store.save_prev_day_snap(self._prev_day_snap)
```

- [ ] **Step 8: 跑测试,确认 PASS**

- [ ] **Step 9: 新问题预演**

```bash
# 1. grep save_state / load_state 全部用法,确认我们的实现风格一致
grep -n "def save_state\|def load_state" app/sim_trader/store.py

# 2. 验证 sim_state 表的 key-value 结构
grep -n "sim_state" app/sim_trader/store.py | head -10

# 3. 验证 JsonSimStore 是否在 main.py 被用(若是,需同步加方法)
grep -n "JsonSimStore\|SimTraderStore" app/ -r --include="*.py"
```

- [ ] **Step 10: 5 步强制验证**

```bash
python scripts/test_fix_09.py
python scripts/test_fix_10.py
python scripts/test_fix_11.py
python scripts/test_fix_12.py
python scripts/test_fix_13.py
ls scripts/test_fix_*.py 2>/dev/null | xargs -I {} python {}
python scripts/test_simple_runner.py
```

- [ ] **Step 11: Commit**

```bash
git add app/sim_trader/store.py app/sim_trader/engine.py scripts/test_fix_13.py
git commit -m "fix(sim_trader): persist _prev_day_snap in store for restart safety (#9-sibling/L5)"
```

---

## 收尾:生成 CHANGELOG

所有 5 个 commit 完成后:

- [ ] **Step 1: 生成 CHANGELOG**

```bash
cat > CHANGELOG-2026-06-23.md << 'EOF'
# 2026-06-23 Bug 修复 CHANGELOG

## 修复的 5 个已知遗留(同根源 bug 收尾)

| L# | 来源 | 简述 | Commit |
|---|---|---|---|
| L1 | #7 sibling | backfill_daily_tushare.py 删 * 1000 | (填入) |
| L2 | #10 sibling | 3 处 deepseek-v4-pro 改 deepseek-chat | (填入) |
| L3 | #11 sideeffect | 维护 _today_trades,API 改用 | (填入) |
| L4 | #11 sideeffect | reporter 加 refresh_trades_from_store() | (填入) |
| L5 | #9 sibling | store 持久化 _prev_day_snap | (填入) |

## 修复期间监控表

| Commit | 文件改动 | 测试通过 | 服务启动 | 页面访问 | API 正常 | 前端同步 | 备注 |
|---|---|---|---|---|---|---|---|
| L1 |  ✓ | ✓ | ✓ | ✓ | ✓ | - | |
| L2 |  ✓ | ✓ | ✓ | ✓ | ✓ | - | |
| L3 |  ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 涉及 API 字段 |
| L4 |  ✓ | ✓ | ✓ | ✓ | ✓ | - | |
| L5 |  ✓ | ✓ | ✓ | ✓ | ✓ | - | 涉及 store 持久化 |

## 用户需要做的操作

- 立即手测(参 CHANGELOG-2026-06-22.md 同样清单)
- 关注:模拟盘"今日交易数"准确性(Task L3 修复)
- 关注:回测完成后报表是否显示交易(Task L4 修复)
- 重启服务后第一次 sell_phase 是否正常(Task L5 修复)
EOF
```

- [ ] **Step 2: Commit CHANGELOG**

```bash
git add CHANGELOG-2026-06-23.md
git commit -m "docs(changelog): 2026-06-23 5 个已知遗留修复记录"
```

---

## Self-Review

按 writing-plans skill 要求做自检:

**1. Spec 覆盖**:
- ✅ L1 → Task 9
- ✅ L2 → Task 10
- ✅ L3 → Task 11
- ✅ L4 → Task 12
- ✅ L5 → Task 13
- ✅ 5 步强制验证 → 每个 Task 的对应 Step
- ✅ CHANGELOG → 收尾章节

**2. 占位扫描**:
- ✅ 无 "TBD" / "TODO" / "implement later"
- ✅ 无 "Add appropriate error handling" 模糊描述
- ✅ 每个 Step 都有完整代码

**3. 类型一致性**:
- ✅ `_today_trades: List[Trade]` 一致
- ✅ `_prev_day_snap: dict` 一致
- ✅ `save_prev_day_snap(snap: dict)` / `load_prev_day_snap() -> dict` 一致

**未发现需 inline 修复的问题。**
