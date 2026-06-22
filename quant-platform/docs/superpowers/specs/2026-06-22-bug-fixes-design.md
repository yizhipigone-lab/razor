# Bug 修复设计 Spec:CODEX 报告 8 个数据/模拟盘问题

> **日期**:2026-06-22
> **作者**:Claude
> **范围**:8 个 P0/P1/P2 bug 修复 + 完整回归保障
> **状态**:已获用户批准,等待 review

---

## 0. 硬约束(优先级最高,凌驾于所有技术细节)

### 0.1 零新问题硬约束(用户原话:不能修了BUG,出现新问题)

> **修任何 bug 之前,必须问"会不会引入新问题"。如果会,放弃这个改法,找不会引发新问题的方案。**

落地到每个 commit:
- 修复前,grep 所有相关调用点
- 修复前,预演 3 类新问题(改了 A 影响 B / C / D)
- 修复后,跑 5 步强制验证(见 §6)
- 发现新问题 → 立即 `git revert HEAD` 当前 commit,改方案重做

### 0.2 业务连续性硬约束(用户原话:不能影响业务开展)

- 修复期间用户**暂停止损/实盘/模拟盘自动交易**,只保留查询
- 修复完成后用户**逐项手测**确认无新问题
- 修复后**任何功能不可用、页面访问 500、前后端不一致、点击后端无反应** = 立即回滚

### 0.3 验收红线(任一违反 = 回滚)

| 红线 | 检测 |
|---|---|
| 功能不可用 | 8 个原有 test_*.py 全部通过 |
| 页面访问 500 | curl 主页 + 所有 Tab 页面 = 200 |
| 前后端不一致 | 改了后端字段,前端相应位置必须同步 |
| 点击无反应 | 关键 API 全部可调通 |
| 业务阻塞 | 模拟盘/回测/选股/数据同步全链路手动跑通 |

---

## 1. 背景与问题清单

CODEX 静态扫描报告了 8 个问题,本 spec 是修复设计。

| # | 严重度 | 问题 | 真实状态 | 文件 |
|---|---|---|---|---|
| 2 | 🔴 | `db.update_stock_list` 不存在,静默失败 | 灾难性,新股票进不来 | [app/data_manager/engine.py:43,69](app/data_manager/engine.py#L43) |
| 7 | 🟠 | Tushare `amount * 1000`,单位错 1000 倍 | amount 指标全失真 | [app/data_manager/engine.py:92,150](app/data_manager/engine.py#L92) |
| 8 | 🟠 | `Position.market_value` 用成本价 | 定时炸弹 | [app/sim_trader/engine.py:82-84](app/sim_trader/engine.py#L82-L84) |
| 9 | 🟠 | `_prev_snap` 语义错(CODEX 误判为"不更新",实际是更新了但用错数据) | 复权跳空保护对实盘无效 | [app/sim_trader/engine.py:141,477](app/sim_trader/engine.py#L141) |
| 10 | 🟠 | `committee.py` 用 "EMPTY" 兜底,模型名 `deepseek-v4-pro` 错误 | 配置缺失时错误链路长 | [app/agents/committee.py:28,35](app/agents/committee.py#L28) |
| 11 | 🟠 | sim_trader 双路径重复写 trade | total_trades 虚高 | [app/sim_trader/engine.py:457](app/sim_trader/engine.py#L457), [app/sim_trader/intraday_monitor.py:182](app/sim_trader/intraday_monitor.py#L182) |
| 13 | 🟠 | intraday_monitor 用日历日,引擎用交易日 | 同一时刻 hold_days 不同 | [app/sim_trader/intraday_monitor.py:136](app/sim_trader/intraday_monitor.py#L136) vs [app/sim_trader/engine.py:327](app/sim_trader/engine.py#L327) |
| 16 | 🟡 | `app_setting.json` 有"风险"死配置 | 历史遗留,易混淆 | [config/app_setting.json:178](config/app_setting.json#L178) |

**前置核对结论**:8 条 claim **全部成立**(已读源码逐条验证),其中 #9 的根因是 CODEX 误判(实际是"更新了但用错数据",不是"不更新")。

---

## 2. 修复策略

### 2.1 4 个修复域(按风险分组)

| 域 | 包含 # | 主要文件 | 风险 | Commit 顺序 |
|---|---|---|---|---|
| **A 数据层** | 2, 7, 16 | `app/data_manager/engine.py`, `database/duckdb_manager.py`, `config/app_setting.json` | 低 | A1 → A2 → A3 |
| **B Agent 层** | 10 | `app/agents/committee.py` | 低 | B1 |
| **C 模拟盘核心** | 11, 13, 8, 9 | `app/sim_trader/engine.py`, `app/sim_trader/intraday_monitor.py`, `app/sim_trader/store.py`(可能) | **中** | C1 → C2 → C3 → C4 |
| **D 回归保障** | 8 个 test_fix_*.py | `scripts/test_fix_*.py` | 低 | 穿插在每个 commit |

**为什么 C 严格按 C1→C2→C3→C4 顺序?**
- C1 删 append:消除"虚高"假象,让 C2/C3 的测试数据干净
- C2 改 hold_days:用 engine 一致的交易日计数
- C3 改 Position:加 `current_price` 字段
- C4 改 prev_snap:依赖 C3 的 `current_price` 字段才能正常判断

### 2.2 关键设计原则

1. **零行为回归**:任何不修的 issue,确认不引入新 bug
2. **修复域内聚**:同一 commit 内的 issue 互相关联,便于回归
3. **测试隔离**:每个 `test_fix_*.py` 可独立 `python scripts/test_fix_XX.py` 跑通
4. **#9 改造最小化**:`adjust_for_gap(pos, prev_bar, today_bar)` 函数签名不变,caller 端维护"昨日 close 字典"
5. **修复期间业务保护**:用户暂停止损/实盘/模拟盘自动交易,只保留查询

---

## 3. 域 A:数据层正确性

### A1 - 修 #2 `db.update_stock_list` 静默失败

**改动**:

`[database/duckdb_manager.py](database/duckdb_manager.py)` 在 `upsert_stocks`(第 351 行)后新增 alias:

```python
def update_stock_list(self, df: pd.DataFrame):
    """兼容旧方法名(与 #2 修复保持一致)"""
    return self.upsert_stocks(df)
```

`[app/data_manager/engine.py](app/data_manager/engine.py)` 第 71 行修裸 except:

```python
- except: pass
+ except Exception as e:
+     log.warning(f"TDX 股票列表扫描失败: {e},回退到 DB")
```

**新问题预演**:
- ❓ alias 会破坏现有 `upsert_stocks` 调用方吗? → grep `upsert_stocks` 全部调用方,确认无变化
- ❓ 裸 except 改为显式 except 会改变控制流吗? → 不会,仅多了日志
- ❓ 调用 `db.update_stock_list(df_out)` 在 Tushare 路径(第 43 行)需要修改吗? → 不需要,alias 兼容

**测试**:`scripts/test_fix_02.py`
- mock `upsert_stocks`, 调 `db.update_stock_list(df)`,断言调通
- 用 `inspect` 验证方法名存在(防回归)

---

### A2 - 修 #7 Tushare amount 单位错误

**改动**:

`[app/data_manager/engine.py](app/data_manager/engine.py)` 删两行错误转换:

```python
# 第 92 行(download_daily_bars)
- df['amount'] = df['amount'] * 1000  # tushare 金额单位为千元,需转元
+ # Tushare amount 字段单位是元,无需转换(#7 修复)

# 第 150 行(download_min5_bars)同样处理
- df['amount'] = df['amount'] * 1000
+ # Tushare amount 字段单位是元,无需转换
```

**新问题预演**:
- ❓ 下游有 `df['amount'] / total_mv` 等计算会受影响吗? → grep `df\['amount'\]` 全部用法,逐个确认(目前未找到换手率/资金流计算,影响面小)
- ❓ 监控大屏"成交额"显示会变化吗? → 会,会**缩小 1000 倍** — 但这正是修复目标
- ❓ 历史 parquet 里 amount 仍是错的(已下载的) → 文档说明:用户需重跑 `batch_download_all` 刷新 amount 字段

**测试**:`scripts/test_fix_07.py`
- mock `ts.pro_bar` 返回 `amount=1000000`
- 调 `download_daily_bars('000001')`
- 断言返回的 df 中 `amount == 1000000`(不是 10 亿)

---

### A3 - 修 #16 删除"风险"死配置

**改动**:

`[config/app_setting.json](config/app_setting.json)` 第 178-188 行的 `"风险": {...}` 整段删除。

**新问题预演**:
- ❓ 删了"风险"段,某处代码读 "风险.hard_stop_loss_pct" 会不会读不到? → grep "风险" 全部用法,**目前 settings.get 只读 "risk" 键**,安全
- ❓ JSON 删后格式还合法吗? → 跑 `python -c "import json; json.load(open('config/app_setting.json'))"` 验证

**测试**:`scripts/test_fix_16.py`
- 加载 `app_setting.json`,断言无 `"风险"` 键
- 断言 `"risk"` 键仍存在且包含 `hard_stop_loss_pct`

---

## 4. 域 B:Agent 鲁棒性

### B1 - 修 #10 committee.py 统一 LLM key 校验

**改动**:

`[app/agents/committee.py](app/agents/committee.py)` 第 26-40 行 `get_llm()` 改写:

```python
def get_llm(model="gpt-4o"):
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")

    if not api_key:
        log.error("LLM API key 未配置: 请设置 OPENAI_API_KEY 或 DEEPSEEK_API_KEY 环境变量")
        raise RuntimeError(
            "LLM API key 未配置: 请设置 OPENAI_API_KEY 或 DEEPSEEK_API_KEY 环境变量"
        )

    if not base_url and os.environ.get("DEEPSEEK_API_KEY"):
        base_url = "https://api.deepseek.com/v1"
        # 修正 #10: 实际模型名不是 v4-pro
        if model in ("gpt-4o", "deepseek-chat"):
            model = "deepseek-chat"

    if not base_url:
        base_url = "https://api.openai.com/v1"

    return ChatOpenAI(model=model, api_key=api_key, base_url=base_url, temperature=0.2)
```

`bull_researcher` / `bear_researcher` / `research_manager` 加 try/except(防 LangGraph 中断):

```python
def bull_researcher(state):
    try:
        llm = get_llm()
        resp = llm.invoke([...])
        return {"bull_report": resp.content}
    except Exception as e:
        log.error(f"Bull researcher failed: {e}")
        return {"bull_report": f"❌ 看多分析失败: {e}"}
```

**新问题预演**:
- ❓ LangGraph 单节点 raise 会中断整个图吗? → 会(用户痛点),必须加 try/except
- ❓ 修了 `deepseek-v4-pro` → `deepseek-chat` 后,有调用方硬编码 "deepseek-v4-pro" 吗? → grep,如果有,一并改
- ❓ `strategy_coder.py` / `stock_analyst.py` 有同样问题吗? → 先 grep 验证,如有,按 B1 模板一并改

**测试**:`scripts/test_fix_10.py`
- 清空 `OPENAI_API_KEY` 和 `DEEPSEEK_API_KEY` 环境变量
- 调 `get_llm()`,断言抛 `RuntimeError`(不是发"EMPTY" 出去)
- 设置 `DEEPSEEK_API_KEY=fake_key`,断言 `model == "deepseek-chat"`

---

## 5. 域 C:模拟盘核心(风险最高)

### C1 - 修 #11 删两处 self.trades.append

**改动**:

`[app/sim_trader/engine.py](app/sim_trader/engine.py)` 第 455-458 行:

```python
if trade:
    trade.hold_days = sum(1 for td in trading_dates
                          if pos.entry_date <= td <= today)
-   self.trades.append(trade)  # 删
-   # 注意:execute_sell 已调用 _store.save_trade(trade),这里不再重复保存
+   # 不再 append:execute_sell 已写 DB;冷启动时 load_trades 一次性加载
+   # 这样能避免盘中 sell + 尾盘 sell 时,内存与 DB 不一致
```

`[app/sim_trader/intraday_monitor.py](app/sim_trader/intraday_monitor.py)` 第 181-182 行:

```python
if trade:
-   self.engine.trades.append(trade)  # 删
+   # 不再 append:self.engine.execute_sell 已写 DB
```

**新问题预演**(必须):
- ❓ `len(self.trades)` 在别处被读的地方会少算吗? → grep 全部 `len(self.trades)` 验证
- ❓ "今日交易数" 前端字段会少算吗? → 如果有,需要额外维护 `self._today_trades` 列表
- ❓ 冷启动时 `self.trades = self._store.load_trades()` 加载完全部历史 → 后续 append 删了,运行中卖出后 `len(self.trades)` 不变 → **这是预期行为**(内存与 DB 一致,UI 应从 DB 读)
- ❓ reporter.py / main.py 用了 `self.trades` 吗? → grep 验证

**测试**:`scripts/test_fix_11.py`
- mock store, 调 `engine.execute_sell` 返回 trade
- 断言 `len(engine.trades)` **长度不变**(之前会 +1)
- 断言 `store.save_trade` 被调用
- 双路径场景:模拟盘中 + 尾盘各卖一次,验证 `len(trades)` 不膨胀

---

### C2 - 修 #13 intraday_monitor hold_days 公式

**改动**:

`[app/sim_trader/intraday_monitor.py](app/sim_trader/intraday_monitor.py)` 第 136 行:

```python
# _check_position 中
- hold_days = (date.today() - pos.entry_date).days + 1  # +1 统一为含首日计数
+ # 与 sim_trader.engine.check_stops 公式保持一致(#13 修复)
+ # 用交易日计数(自然日受周末/假期干扰,触发时机与尾盘不同步)
+ from app.api.sim_trader import _load_trading_calendar
+ _cal = _load_trading_calendar() or set()
+ trading_dates_window = sorted(d for d in _cal if pos.entry_date <= d <= date.today())
+ hold_days = len(trading_dates_window)
```

**新问题预演**:
- ❓ `_load_trading_calendar()` 返回 set 还是 list? → 已确认返回 `set()`(engine.py:161-164)
- ❓ 空日历时(`_cal = set()`)会怎样? → `trading_dates_window = []`,`hold_days = 0` → 立即触发时间退出!需要边界保护
- ❓ 边界保护:hold_days 至少 1

```python
+ hold_days = max(1, len(trading_dates_window))  # 至少 1,防空日历误触
```

**测试**:`scripts/test_fix_13.py`
- mock `_load_trading_calendar` 返回固定日期集
- `entry_date = 2025-01-03`(周五),`today = 2025-01-06`(周一)
- 旧公式:`(06-03).days + 1 = 4`
- 新公式:如果 `trading_dates = {01-03, 01-06}` → `len = 2`
- 断言 `hold_days == 2`

---

### C3 - 修 #8 Position.market_value 加 current_price 字段

**改动**:

`[app/sim_trader/engine.py](app/sim_trader/engine.py)` 第 63-98 行 `Position` dataclass 改:

```python
@dataclass
class Position:
    code: str
    entry_date: date
    entry_price: float
    shares: int
    cost: float
    peak_price: float = 0.0
    remaining_shares: int = 0
    tp1_triggered: bool = False
    tp2_triggered: bool = False
    is_active: bool = True
    strategy_name: str = ""
    entry_time: str = "15:00"
    current_price: float = 0.0  # 新增:由 record() 阶段更新(#8 修复)

    def __post_init__(self):
        self.peak_price = self.entry_price
        self.remaining_shares = self.shares

    @property
    def market_value(self) -> float:
        return self.remaining_shares * self.current_price  # 改:用当前价

    @property
    def profit_pct(self) -> float:  # 改:从方法变 property
        if self.current_price <= 0:
            return 0.0
        return (self.current_price / self.entry_price - 1) * 100
```

`record()` 方法增量更新 current_price:

```python
def record(self, today: date, snapshot: dict):
    # 增量更新所有持仓的 current_price
    for code, pos in self.positions.items():
        bar = snapshot.get(code)
        if bar:
            pos.current_price = bar['close']
    eq = self.total_equity(snapshot)
    ...
```

**新问题预演**(必做,影响面大):
- ❓ `pos.profit_pct(current_price)` 旧调用会变 `TypeError` → grep 全部 `profit_pct` 调用点(已确认范围:仅 sim_trader 内部),逐个改
- ❓ `pos.market_value` 行为变化(从成本变市值)→ 前端 UI 是否能接受? → 测试时手测账户页
- ❓ 持仓冷启动时 `current_price = 0.0` → `market_value = 0`,`profit_pct = 0` → 需要等首次 `record()` 后才有值,文档说明
- ❓ `qmt_proxy_server.py:243,259` 的 `asset.market_value` / `p.market_value` 是 QMT 库的方法,**不是** Position,**不受影响** → 已确认

**测试**:`scripts/test_fix_08.py`
- 创建 Position, `current_price=0`,断言 `market_value == 0`
- 手动设 `current_price=10.5`,`remaining_shares=100`,断言 `market_value == 1050`
- 断言 `profit_pct` 不再是方法(用 `pos.profit_pct` 不传参)

**风险提示**:这是本次修复中影响面最大的一项,执行时**特别小心**,Step 5 浏览器手测必做。

---

### C4 - 修 #9 caller 维护 _prev_day_snap

**思路**:`adjust_for_gap(pos, prev_bar, today_bar)` 函数签名不变,关键是 caller 传入的 `prev_bar` 应该是"昨日的完整 OHLC"而不是"今日的实时"。

**改动**:

`[app/sim_trader/engine.py](app/sim_trader/engine.py)` `SimTraderEngine.__init__` 后新增字段:

```python
self._prev_snap: dict = {}         # 今日实时(用于盘中风控)
self._prev_day_snap: dict = {}     # 昨日完整 OHLC(用于除权跳空保护)
```

`sell_phase` 末尾:

```python
sells = self.check_stops(today, snapshot, trading_dates,
-                        prev_snap=self._prev_snap)
+                        prev_snap=self._prev_day_snap)  # 改:从 _prev_snap 改成 _prev_day_snap
...
# 卖单执行完后,更新两个快照
self._prev_snap = {k: dict(v) for k, v in snapshot.items()}
+ self._prev_day_snap = self._prev_snap  # 今日尾盘 = 次日开盘的"昨日"
```

`[app/sim_trader/intraday_monitor.py](app/sim_trader/intraday_monitor.py)` 第 102 行:

```python
- self.engine.record(_d.today(), self.engine._prev_snap or {})
+ self.engine.record(_d.today(), self.engine._prev_day_snap or self.engine._prev_snap or {})
```

**新问题预演**:
- ❓ `_prev_day_snap` **首次启动时是空**,第一天不触发保护 → 可接受(本来就无"昨日")
- ❓ 改 `_prev_snap` → `_prev_day_snap` 后,某处依赖旧名 → grep 全部 `_prev_snap` 使用,**intraday_monitor 那一处 read**保留(它需要兜底)
- ❓ `adjust_for_gap` 函数契约不变,只改 caller,影响面小
- ❓ `store.py` 是否需要持久化 `_prev_day_snap`? → 可选,简化起见**不持久化**(重启当天不触发保护,文档说明)

**测试**:`scripts/test_fix_09.py`
- 模拟 2 个连续交易日的 sell_phase 调用
- Day 1: 调 sell_phase, 断言 `_prev_day_snap` 已更新
- Day 2: 调 sell_phase, 断言 check_stops 收到的 `prev_snap` 是 Day 1 的(不是 Day 2 的)
- 边界:首次启动时 `_prev_day_snap == {}`, 不抛错

---

## 6. 强制 5 步验证(每个 commit 必跑)

```
Step 1: 自己的单元测试通过
        $ python scripts/test_fix_XX.py

Step 2: 全部 test_fix_*.py 通过(防回归)
        $ for f in scripts/test_fix_*.py; do python $f || echo "FAIL: $f"; done

Step 3: 现有回归脚本通过
        $ python scripts/test_simple_runner.py
        $ python scripts/test_determinism.py

Step 4: 服务启动 + 主页 200 + 关键 API 不 500
        $ python server.py &  # 后台跑
        $ curl -I http://localhost:5000/                                # 200
        $ curl -I http://localhost:5000/api/backtest/run-simple         # 不 500
        $ curl -I http://localhost:5000/api/sim_trader/status           # 不 500
        $ kill %1

Step 5: 启动浏览器手测涉及到的页面(每个 commit 至少 1 个页面)
        [ ] 主页可见
        [ ] 涉及 Tab 可点击
        [ ] 关键按钮有响应
```

**任一失败 → 立即 `git revert HEAD` 当前 commit,改方案重做**

---

## 7. 修复期间业务保护(给用户的话术)

> **修复期间建议你暂时停用实盘/模拟盘自动交易,只保留查询功能**。修完确认无新问题再开。
>
> 原因:虽然每个 commit 我会跑回归,但 LangGraph / sim_trader 涉及定时任务、状态机、磁盘 I/O,有些场景(如跨日切换、订单在途、网络抖动)我无法在本地完全复现,只有真实业务流能发现。
>
> **建议节奏**:
> 1. 修 1 个 commit,你看一眼
> 2. 跑你日常用的功能 1 次
> 3. 没问题,告诉我"下一个"
> 4. 有问题,告诉我哪里,立刻 revert

---

## 8. 文档产出

修复完成后,生成 `[CHANGELOG-2026-06-22.md](CHANGELOG-2026-06-22.md)`,列出:
- 8 个 issue 的修复 commit hash
- 已知遗留(历史 amount 数据需重跑)
- 用户需要做的操作(可选,重跑同步)
- 修复全程监控表(每行打勾确认)

---

## 9. 范围外(明确不做,YAGNI)

- ❌ 不加 ruff lint 规则防止方法名拼错(后续 refactor 单独做)
- ❌ 不清理 `_deprecated/` 目录(范围外)
- ❌ 不重写 `adjust_for_gap` 函数签名(用户选了 caller 维护)
- ❌ 不补 pytest 框架(用户选了 scripts/test_fix_*.py)
- ❌ 不修 amount 历史数据(用户主动跑同步可刷)
- ❌ 不为 `sim_trader_status` 写新 UI(范围外)
- ❌ 不在本次改动中加新功能(纯修复)

---

## 10. 待 user 确认事项

修复完成后,需要 user 帮忙做的**手测项**(对应 Step 5):
- [ ] 主页访问
- [ ] 选股 Tab 跑一次
- [ ] 回测 Tab 跑一次
- [ ] 模拟盘 Tab 跑一次(查持仓/查交易)
- [ ] 数据同步跑一次
- [ ] AI 委员会跑一次(看是否返回报告,不要求准确)
- [ ] 设置页面改一个参数看是否生效

**任一手测项失败 = 立即停止后续 commit,排查 + 回滚**
