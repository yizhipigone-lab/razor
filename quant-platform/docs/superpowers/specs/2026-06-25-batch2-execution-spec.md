# 批 2:引擎统一 Spec

> 日期:2026-06-25
> 作者:Claude
> 项目:quant-platform 全面优化(3 批拆分)
> 批 2 范围:4 项引擎统一,P1 风险,约 5-7 天
> 状态:待用户 review,批准后写 plan,执行
> 上批成果:批 1 已完成(commit a3f9bb8 等),4 项地基修复

---

## 0. 上下文与硬约束

### 0.1 批 1 做了什么
- 真相源统一(删 9+8+6=23 个假默认值)
- AI 目标函数改真风险调整(Sharpe 简化)
- 新建 AST 沙箱(strategy_coder 防 prompt 注入)
- .gitignore 清理 70621 行日志入库

### 0.2 批 2 要修的(来自复盘报告 P1)
1. **A. 4 引擎成交执行层不统一**:成本/T+1/涨停各写各的
2. **F. event_engine 假异步 + DuckDB 连接泄漏 + Parquet 写竞态**
3. **G. hold_days 口径不一致**(tdx 用日历日,其他用交易日)
4. **H. 净值口径用成本价**(回撤被低估)

### 0.3 硬约束(继承)
- 批间必须 merge 才能继续(用户原话)
- 修改代码不能破坏原有功能(用户记忆 feedback_safe_modification.md)
- 0 报错 0 崩溃
- config.py 唯一真相源
- 严格冻结: 批 2 期间不动 4 个回测引擎、core/event_engine.py、duckdb_manager.py、sim_trader

### 0.4 批 2 范围外
- pytest 测试体系(批 3)
- AI 样本外协议(批 3)
- 模拟盘参数源对齐(批 3)
- 实盘网关(全部排除)

---

## 1. 目标

5 个 commit 内完成 4 项引擎统一,实现:
- 4 引擎结果可比(用同一成交执行层)
- 回测不再内存泄漏、连接不回收、文件无锁
- 持仓 N 天判定口径一致(交易日)
- 净值/回撤用真实市值(非成本价)

---

## 2. 设计

### 2.1 项 A 统一成交执行层

**当前问题**(已确认):
- `app/backtest/engine.py:258-268` 有涨停买入过滤,simple/tdx/strict_runner 都没有
- `strict_runner.py:124-164` 有完整成本(滑点+佣金+印花),simple/tdx 完全零成本
- `engine.py:622-629` 仅 5min 路径扣成本,日线路径零成本
- T+1 约束只有 strict_runner 实现(`sellable_date`),tdx 日内 T+0

**目标**:新建 `app/backtest/execution.py` 统一 4 件事:
1. **涨停过滤**(`can_buy(code, prev_close, today_open, today_high) -> bool`)
2. **T+1 约束**(`can_sell_today(pos, today) -> bool`)
3. **买入成本**(`calc_buy_cost(price, shares, cfg) -> (cost, fee)`)
4. **卖出成本**(`calc_sell_revenue(price, shares, cfg) -> (revenue, tax, commission)`)

**改动设计**:

```python
# 新建 app/backtest/execution.py

# 涨幅表:代码前缀 → 涨停幅度
LIMIT_UP_MAP = {
    '300': 0.20, '301': 0.20, '688': 0.20,  # 创业/科创 ±20%
    '8': 0.30, '4': 0.30,                    # 北交所 ±30%
}
DEFAULT_LIMIT_UP = 0.10  # 主板 ±10%

def get_limit_up_pct(code: str) -> float:
    prefix = code[:3] if code.startswith(('300', '301', '688')) else code[:1]
    return LIMIT_UP_MAP.get(prefix, DEFAULT_LIMIT_UP)


def can_buy(code: str, prev_close: float, today_high: float) -> tuple[bool, str]:
    """一字板/涨停封板不能买入"""
    if prev_close <= 0 or today_high <= 0:
        return True, "OK"
    change = (today_high - prev_close) / prev_close
    if change >= get_limit_up_pct(code) - 0.005:  # 0.5% 容差
        return False, f"涨停封板({change*100:.1f}%)"
    return True, "OK"


def can_sell_today(entry_date, today) -> bool:
    """T+1: 买入当天不能卖"""
    return today > entry_date  # 严格大于


# 默认成本配置(可被各引擎覆盖)
DEFAULT_COST_CFG = {
    'commission_rate': 0.00025,   # 万2.5
    'min_commission': 5.0,         # 最低 5 元
    'stamp_tax_rate': 0.0005,     # 千0.5(卖时)
    'slippage_rate': 0.001,        # 万10 双边
}


def calc_buy_cost(price: float, shares: int, cfg: dict = None) -> dict:
    cfg = {**DEFAULT_COST_CFG, **(cfg or {})}
    gross = price * shares
    commission = max(gross * cfg['commission_rate'], cfg['min_commission'])
    slippage = gross * cfg['slippage_rate']
    return {
        'gross': gross,
        'commission': commission,
        'slippage': slippage,
        'total': gross + commission + slippage,
    }


def calc_sell_revenue(price: float, shares: int, cfg: dict = None) -> dict:
    cfg = {**DEFAULT_COST_CFG, **(cfg or {})}
    gross = price * shares
    commission = max(gross * cfg['commission_rate'], cfg['min_commission'])
    stamp_tax = gross * cfg['stamp_tax_rate']
    slippage = gross * cfg['slippage_rate']
    return {
        'gross': gross,
        'commission': commission,
        'stamp_tax': stamp_tax,
        'slippage': slippage,
        'total': gross - commission - stamp_tax - slippage,
    }
```

**改动文件**:
- 新建 `app/backtest/execution.py`
- 改 `app/backtest/engine.py` 在买卖点调用 `can_buy` / `can_sell_today` / `calc_buy_cost` / `calc_sell_revenue`
- 改 `app/backtest/simple_runner.py` `buy()` / `sell()` 加 `can_buy` 和成本
- 改 `app/backtest/tdx_runner.py` 日内买入前 `can_buy` 校验
- 改 `app/backtest/strict_runner.py` 成本计算改用 `calc_buy_cost` / `calc_sell_revenue`(行为等价)

**风险**:
- 高。改 4 引擎可能影响回测数字
- 必须:每个 commit 跑回归 `test_simple_runner.py` 验证数字变化

### 2.2 项 F event_engine + DuckDB + Parquet 写

**当前问题**(已确认):
- `core/event_engine.py:67-72` `_queue.append` 只进不出,内存泄漏;"异步"是假象
- `database/duckdb_manager.py:65-85` 按 `thread.ident` 缓存连接,永不回收
- `duckdb_manager.py:815` Parquet 写 tmp 后缀不一致,无文件锁

**目标**:
- event_engine 删除未消费的 `_queue`,或改为真正的后台消费
- DuckDB 连接用 `threading.local()` + 线程退出回调
- Parquet 写加文件锁,统一 tmp 后缀为 `.parquet.tmp`

**改动设计**:

**F1. event_engine 修复**:
```python
# core/event_engine.py
# 删除 self._queue(只进不出)
# put() 直接同步调用 _process
# 或者:真正的后台消费线程 + bounded queue
```

**F2. DuckDB 连接回收**:
```python
# database/duckdb_manager.py
import atexit
import weakref

def _cleanup_connection(conn):
    try: conn.close()
    except: pass

class DatabaseManager:
    def _get_connection(self):
        tid = threading.get_ident()
        if tid not in self._connections:
            conn = duckdb.connect(str(DB_PATH))
            self._connections[tid] = conn
            # 注册线程退出回调
            weakref.finalize(threading.current_thread(), _cleanup_connection, conn)
        return self._connections[tid]
```

**F3. Parquet 写锁**:
```python
# database/duckdb_manager.py
import filelock

def save_bars(self, code: str, df: pd.DataFrame):
    lock_path = self.daily_dir / f".locks/{code}.lock"
    with filelock.FileLock(str(lock_path), timeout=30):
        # 现有 write 逻辑
        ...
```

**改动文件**:
- 改 `core/event_engine.py` 删除 `_queue` 或加 bounded queue
- 改 `database/duckdb_manager.py` 加 `threading.local` 和文件锁

**风险**:
- 中。event_engine 是其他模块用的基础设施,改了可能影响其他模块
- 必须:`grep` 所有 `_queue` / `put` / `emit` 调用方,确保不破坏

### 2.3 项 G hold_days 统一为交易日

**当前问题**(已确认):
- `tdx_runner.py:408,470` 用 `datetime.days`(日历日)
- 其他用 `_td()`(交易日计数)
- 影响:`rule_time_force` / `rule_first_day_exit` / `rule_time_condition` 触发时点不一致

**目标**:
- 抽取 `app/backtest/trading_calendar.py` 统一交易日计算
- 4 引擎都用同一个函数

**改动设计**:

```python
# 新建 app/backtest/trading_calendar.py
from datetime import date

class TradingCalendar:
    def __init__(self, trading_dates: list[date]):
        self._dates = sorted(trading_dates)

    def trading_days_between(self, d1: date, d2: date) -> int:
        """d1 到 d2 之间的交易日数(含两端)"""
        return sum(1 for d in self._dates if d1 <= d <= d2)

    def is_trading_day(self, d: date) -> bool:
        return d in self._dates
```

**改动文件**:
- 新建 `app/backtest/trading_calendar.py`
- 改 `app/backtest/tdx_runner.py:408,470` 用 TradingCalendar
- 改 `app/backtest/simple_runner.py:73,158,353` 用 TradingCalendar(如果可以)
- 改 `app/backtest/strict_runner.py` 用 TradingCalendar

**风险**:
- 中。改 4 引擎的 hold_days 计算
- 测试:test_simple_runner 必须返回相同结果(否则数字会变)

### 2.4 项 H 净值口径改市值

**当前问题**(已确认):
- `engine.py:480,492` 组合模式用 `invested_capital`(买入成本)算 NAV
- `tdx_runner.py:534-535` 终值用 `shares * entry_price`
- 影响:持仓段净值不反映浮动盈亏,最大回撤被低估

**目标**:
- 净值 = cash + sum(shares * latest_close)

**改动设计**:

```python
# 通用 helper
def compute_equity(cash: float, positions: dict, prices: dict) -> float:
    """positions: {code: {shares, ...}}, prices: {code: close}"""
    market_value = sum(p['shares'] * prices.get(code, p.get('entry_price', 0))
                       for code, p in positions.items() if p.get('active', True))
    return cash + market_value
```

**改动文件**:
- 改 `app/backtest/engine.py:480,492` 用市价
- 改 `app/backtest/tdx_runner.py:534-535` 用市价
- 改 `app/backtest/simple_runner.py:442-444` 已经用 close,可能需要确认
- 改 `app/backtest/strict_runner.py` 同样

**风险**:
- 中。改净值计算,回测的最大回撤数字会变(变大,更真实)
- 测试:test_simple_runner 的 max_drawdown 会变(预期 2.1% → 更大)

---

## 3. 5 个 commit 划分

```
C2-1: 新建 app/backtest/execution.py(统一涨停/T+1/成本) + 改 engine.py 调用
C2-2: 改 simple_runner.py 和 strict_runner.py 用 execution.py
C2-3: 改 tdx_runner.py 用 execution.py(加 T+1 和涨停,核心修复)
C2-4: 新建 app/backtest/trading_calendar.py + 改 4 引擎用交易日
C2-5: 修 event_engine 队列 + DuckDB 连接回收 + Parquet 写锁 + 改 4 引擎净值用市值
```

实际可能调整顺序,看依赖关系。

---

## 4. 验证清单(每个 commit 必跑)

- [ ] test_simple_runner.py 通过(数字可能变,但 0 报错 0 崩溃)
- [ ] 所有 test_fix_*.py 通过(20+ 个)
- [ ] event_engine 内存不再泄漏(长跑测试)
- [ ] DuckDB 连接正确回收(threading.local)
- [ ] Parquet 写有文件锁(并发测试)
- [ ] 涨停买入过滤在 4 引擎都生效
- [ ] T+1 约束在 4 引擎都生效
- [ ] 成本计算在 4 引擎一致

---

## 5. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 改 4 引擎可能影响回测数字 | 每个 commit 跑回归,数字变化需记录 |
| event_engine 改可能影响其他模块 | grep 所有调用方,先小改测试 |
| DuckDB 连接回收可能影响多线程 | 弱引用 + atexit 双重保险 |
| Parquet 写锁可能死锁 | filelock 默认 timeout=30s,失败抛错 |

---

## 6. 范围外(批 3)

- pytest 测试体系
- AI 样本外协议
- 模拟盘参数源对齐

---

## 7. 状态

待用户 review。
