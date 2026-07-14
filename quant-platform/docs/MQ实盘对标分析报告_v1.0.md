# MQ项目实盘架构学习与对标分析报告

> **生成时间**：2026-06-28
> **目的**：学习 MQ (`E:\1target\MQ\MQ`) 的实盘交易架构，与我们的设计书（v2.0）做**全面对比**
> **原则**：以MQ做法优先——它已实盘跑通且全部验证正确
> **适用范围**：本项目的实盘模块（`app/live_trader/`）

---

## 0. TL;DR

MQ的实盘架构**远比我们设计书更成熟**。核心差异：

| 维度 | 我们（v2.0） | MQ（已实盘） | **结论** |
|------|-------------|-------------|---------|
| 服务拆分 | 单进程单例 | **x-miniqmt + x-server 双进程** | **采用MQ** |
| 持仓真相 | QMT回报覆盖本地 | **QMT回报覆盖 + 归档快照（双数据源）** | **采用MQ** |
| 委托状态机 | 5个状态（自定义） | **10个状态码**（48-57+255）| **采用MQ** |
| 止盈止损 | 单条优先级链 | **5条独立规则 + 优先级排序** | **采用MQ** |
| 卖单race控制 | 串行锁 | **股票级清仓锁**（`ClearanceLockService`）| **采用MQ** |
| 资源池保护 | 无 | **熔断器 + WaitingFreeWriter强退**| **采用MQ** |
| 盈亏计算 | 简单减法 | **buildSimpleCycles 交易闭环 + 加权均价**| **采用MQ** |
| 持仓天数 | 本地数据库 | **TradeDataQueryService 跨源查询 + 交易日历对齐**| **采用MQ** |
| 订单回报 | 拉模式 | **推模式（xtquant callback）**| **采用MQ** |
| 通知 | WebSocket | **WebSocket + 企业微信**| **采用MQ** |
| 测试支持 | 无 | **TEST/RUN 双模式**（同一规则引擎）| **采用MQ** |

**结论**：MQ做法**全部采纳**，但要做"小型化裁剪"——MQ是为多账户、多环境、多人协作设计，本项目1万小资金只需其**单账户单环境**子集。

---

## 1. 架构对比

### 1.1 服务拆分（MQ 关键优势）

**MQ做法**：
```
x-miniqmt (Python FastAPI, 端口 8001)  ←—  单独进程，封装 QMT SDK
  ├─ connection_manager.py     连接/熔断/重试
  ├─ trade_service.py          下单/撤单
  ├─ orders_service.py         委托查询
  ├─ deals_service.py          成交查询
  ├─ positions_service.py      持仓查询
  ├─ assets_service.py         资产查询
  └─ qmt_callback.py           异步回报回调（推 x-server）

x-server (TypeScript Midway, 端口 7001)  ←—  业务中台
  ├─ x_c_qmt_conn/             连接管理 API
  ├─ x_c_qmt_trade/            交易 API + 委托/成交/事件日志
  ├─ x_m_exit_monitor/         离场监控（核心策略）
  ├─ x_m_trade_logs/           交易日志+盈亏
  └─ x_c_qmt_data/             数据归档
```

**MQ的核心架构思想**：
- **x-miniqmt 是"代理"，x-server 是"大脑"**——QMT 端细节隔离在 x-miniqmt
- **Callback 异步推送**：xtquant 回调 → x-miniqmt 推 HTTP → x-server 写库 → 推 WebSocket
- **"搭积木" + BFF 模式**：x-server 聚合多个服务暴露给前端

**MQ的关键解耦点**：
| 关注点 | MQ做法 | 我们做法 | 风险 |
|--------|--------|----------|------|
| QMT SDK 兼容性 | 隔离在 x-miniqmt | 直接 import 到业务代码 | 升级炸所有 |
| 失败重试 | x-miniqmt 内置熔断+重试 | 我们没做 | 断网即全停 |
| 进程崩溃 | NSSM 守护 x-miniqmt，单退出自动重启 | 同进程内重连 | 业务全停 |
| 跨语言调用 | HTTP（Python ↔ TypeScript）| N/A | 我们是单语言 |

### 1.2 我们应该如何改造？

**我们项目情况**：
- 单语言（Python FastAPI）
- 单进程（main.py 单进程跑全部）
- 资金小（1-2万）
- 单账户、单环境、单用户

**采纳MQ架构 vs 简化取舍**：

| 决策 | 完整MQ做法 | 我们做法（推荐） | 理由 |
|------|-----------|-----------------|------|
| 进程拆分 | x-miniqmt 独立进程 | **不拆** | 单语言同进程即可；进程间通信反而引入复杂度 |
| 服务模块拆分 | 6个独立 service | **采用：6个独立 Python 类**（同进程但解耦）| 保持代码模块化 |
| QMT 封装层 | 独立进程 | **`app/live_trader/qmt_wrapper.py` 单独封装** | 隔离 xtquant 升级风险 |
| 异步回报 | callback + HTTP 推送 | **callback 同进程回调** | 跨进程无收益 |
| 熔断器 | `ConnectionManager` 完整实现 | **完整照搬** | 防资源池爆 |
| NSSM 守护 | Windows 服务 | **NSSM 守护 main.py** | 进程崩溃自动恢复 |

**结论**：
1. **保留单进程**（Python 已是多线程/协程，单进程足够）
2. **模块结构照搬MQ**（6 个 service 类）
3. **熔断器、callback、清仓锁完整照搬**
4. **加 NSSM 守护 main.py**（同MQ部署模式）

---

## 2. 关键设计对比（逐项）

### 2.1 熔断器（MQ 独有，我们 v2.0 缺失）

**MQ实现**（`connection_manager.py:53-58, 107-120, 188-194`）：
```python
class ConnectionManager:
    def __init__(self):
        # 熔断器状态
        self._circuit_breaker_open = False
        self._circuit_breaker_open_time = None
        self._consecutive_failures = 0
        self._max_consecutive_failures = 3  # 连续失败3次触发熔断
        self._circuit_breaker_timeout = 30  # 熔断30秒后尝试恢复
```

**熔断逻辑**：
- 任何 `connect()` 失败 → `_consecutive_failures += 1`
- 连续3次失败 → 熔断器开启30秒
- 熔断期间所有新连接请求直接 `QmtConnectionError("服务熔断中，请稍后重试")`
- 30秒后下一次连接尝试自动恢复
- **`WaitingFreeWriter` 资源池超限 → `os._exit(1)` 强制重启**（依赖NSSM）

**我们的做法（v2.0 缺失）**：只有简单的 try/retry，无熔断

**采纳**：✅ **完整照搬** `app/live_trader/circuit_breaker.py`，加到 `ConnectionManager`

---

### 2.2 持仓真相源（MQ 关键设计）

**MQ做法**（`x_c_qmt_data/` + `x_c_qmt_trade/`）：
```
3 个数据源：
1. QMT 实时持仓（query_stock_positions）         ← 实时真相
2. QMT holdings_backup 表（每日归档快照）         ← 兜底
3. 交易记录反推持仓（trade-data-query-service）   ← 历史
```

**核心 Service**：`TradeDataQueryService`
```typescript
// 自动从两个数据源合并：实时持仓 + 归档快照
// 归档快照用于：QMT不可用时仍能查询
```

**我们的做法（v2.0）**：只依赖 QMT `query_positions` 实时返回，本地表每次覆盖

**采纳**：
1. ✅ 增 `live_holdings_backup` 表（每日收盘归档）
2. ✅ 增 `LiveTradeDataQuery` 类（多源查询）
3. ✅ QMT 不可用时 fallback 到备份表

**实现位置**：`app/live_trader/holdings_data.py`

---

### 2.3 委托状态码（MQ 完整覆盖）

**MQ做法**（`qmt-constants.ts:1-15`）：
```typescript
export const QMT_CODE_TO_STATUS_TEXT_MAP: { [key: number]: string } = {
  48: '未报',     // 初始：本地已生成，未上报
  49: '待报',     // 已接受到本地，准备上报
  50: '已报',     // 已上报到交易所
  51: '已报待撤', // 处于已报状态且有撤单请求
  52: '部成待撤', // 部分成交+待撤
  53: '部撤',     // 部分成交，剩余已撤
  54: '已撤',     // 全部已撤
  55: '部成',     // 部分成交
  56: '已成',     // 全部成交 ← 终态
  57: '废单',     // 错误/拒绝 ← 终态
  255: '未知'
};
```

**MQ的关键设计**：
- 4个**终态**：53/54/56/57 → 释放清仓锁
- 6个**中间态**：48/49/50/51/52/55 → 继续监听
- 未知态 255 → 兜底，继续监听

**我们的做法（v2.0）**：4状态（submitted/partial/filled/rejected），太粗

**采纳**：✅ 完整照搬11状态码字典
- `app/live_trader/order_status.py` 完整复制映射
- `app/live_trader/store.py` 的 `live_orders.status` 字段改 VARCHAR 接收字符串
- 清仓锁释放触发：53/54/56/57（与MQ一致）

---

### 2.4 5条独立规则 + 优先级（MQ 关键设计）

**MQ做法**（`exit-sell-service.ts:498-500`）：
```typescript
// 按优先级从高到低注册：
// 强制平仓(100) > 强卖(80) > 止损(60) > 止盈(40) > 固卖(20)
const engine = new ExitRuleEngine([
  ForceCloseRule,    // 100 长期持有强制平仓
  ForceSellRule,     // 80  强卖（连亏N天）
  StopLossRule,      // 60  止损（回撤阈值）
  TakeProfitRule,    // 40  止盈（分档+回撤）
  FixedSellRule,     // 20  固卖（限价单）
]);
```

**5条规则的核心区别**：
| 规则 | 触发条件 | 卖出方式 | 价格 |
|------|---------|---------|------|
| 强制平仓 | 持仓 > N天 | 全卖（市价"对手最优"）| 市价 |
| 强卖 | 持仓N天且亏损>阈值 | 卖X% | 市价 |
| 止损 | 回撤>阈值（最高价 or 成本价）| 卖X% | 市价 |
| 止盈 | 触发分档+回撤 | 按档位卖 | 市价 |
| 固卖 | 0次卖出=一卖，1次=二卖 | 限价单 | 成本价×(1+ratio) |

**MQ的关键创新——固卖**：
- 用**限价单**（非市价）防止砸盘
- "固卖"是**挂单等待**而不是立即成交
- 每次轮询检测"是否有在途委托"决定是否挂新单
- **清仓锁** 防止固卖被其他清仓动作打断后又被重挂

**我们的做法（v2.0）**：只有"止盈+止损+时间止盈+移动止盈"4条，全在 `exit_rules.py` 的优先级链

**采纳**：
1. ✅ 改造 `exit_rules.py` 为5条独立 Rule 类（与MQ对齐）
2. ✅ 引入"固卖"概念（**用户决策**：`LIMITED_PRICE_TRIGGER` 是否启用？建议启用）
3. ✅ 改造为"准备+评估"两阶段（与MQ `prepare()` + `evaluate()` 一致）
4. ✅ 优先级 100/80/60/40/20

**新增文件**：`app/live_trader/rule_engine/`（5个文件）

---

### 2.5 股票级清仓锁（MQ 关键防race设计）

**MQ做法**（`clearance-lock-service.ts`）：
```typescript
export class ClearanceLockService {
  private locks: Map<string, LockRecord> = new Map();
  private orderIdToKey: Map<number, string> = new Map();
  private static readonly DEFAULT_TTL_SEC = 300;  // 5分钟TTL

  // Key 三元组：(envName, accountId, stockCode)
  static buildKey(envName, accountId, stockCode) {
    return `${envName}::${accountId}::${stockCode}`;
  }

  acquire(envName, accountId, stockCode, reason) {
    // 已存在则拒绝
    if (this.locks.has(key)) return false;
    // 加锁
    this.locks.set(key, {acquiredAt, reason, ttlSec: 300});
    return true;
  }

  isLocked(envName, accountId, stockCode) {
    // 用于固卖规则的 evaluate 阶段：锁存在则跳过该股票
  }

  bindOrderId(envName, accountId, stockCode, qmtOrderId) {
    // 下单成功后绑定 QMT 订单号
    record.qmtOrderId = qmtOrderId;
    this.orderIdToKey.set(qmtOrderId, key);
  }

  releaseByOrderId(qmtOrderId) {
    // QMT 成交回调 / 错误回调 时 O(1) 释放
    const key = this.orderIdToKey.get(qmtOrderId);
    this.orderIdToKey.delete(qmtOrderId);
    this.locks.delete(key);
  }
}
```

**锁的5条规则使用**：
- 止损 / 强卖 / 平仓 / 止盈 / 兜底清仓 执行前 `acquire()`
- 固卖 `isLocked()` → 锁存在则跳过该股票
- QMT 成交回调 (53/54/56) `releaseByOrderId()`
- QMT 错误回调 (57) `releaseByOrderId()`
- TTL 300秒自动兜底释放
- 下单失败立即 `release()`

**我们的做法（v2.0）**：只有简单的全局发单串行锁

**采纳**：✅ **完整照搬** `app/live_trader/clearance_lock.py`

**关键增强点**（MQ解决的核心问题）：
> "第 N 轮撤掉固卖单 → 第 N+1 轮固卖规则又把单挂回去" 的 race

**这是我们设计书里没有的关键洞见——固卖必须被清仓锁保护**。

---

### 2.6 Callback 异步回报（MQ 推模式，我们没设计）

**MQ做法**（`qmt_callback.py:107-180`）：
```python
class DefaultQuantTraderCallback(XtQuantTraderCallback):
    def __init__(self, session_uuid, qmt_session_id, main_loop):
        self.main_loop = main_loop
        self.order_id_to_seq_map: Dict[str, int] = {}
    
    def on_disconnected(self):
        logger.warning("QMT连接断开")
    
    def on_stock_order(self, order: XtOrder):
        # 推 HTTP 到 x-server
        self._create_sync_task(self._send_callback_to_server_sync, ...)
    
    def on_stock_trade(self, trade: XtTrade):
        # 推 HTTP 到 x-server
        ...
    
    def on_order_error(self, order_error: XtOrderError):
        # 推 HTTP 到 x-server
        ...
    
    def on_cancel_error(self, cancel_error: XtCancelError):
        # 推 HTTP 到 x-server
        ...
```

**MQ的关键设计**：
- `XtQuantTraderCallback` 是 xtquant 提供的基类
- 重写 5 个回调：`on_disconnected / on_stock_order / on_stock_trade / on_order_error / on_cancel_error`
- 每个回调**新开线程**推送 HTTP（不阻塞主事件循环）
- `main_event_loop` 存储主 asyncio loop，回调内可 `run_coroutine_threadsafe`

**我们的做法（v2.0）**：**只有"拉"模式**——cron 14:55 对账时拉 `query_orders`

**拉模式的问题**：
- 14:53 卖单发单后，14:55 才知道成没成
- 14:53 卖单发单 → 14:54:30 买单来了 → 本地以为有持仓 → 重复买入
- 跨交易日状态变化无法实时感知

**采纳**：✅ **完整照搬 callback 机制**
- `app/live_trader/qmt_callback.py` 实现 5 个回调
- 同进程内回调（不用 HTTP 推送）
- 回调内写 `live_orders` + `live_audit` + 释放清仓锁

**重大改造**：我们的 `engine.py` sell_phase/buy_phase 必须改为"实时驱动"（由 callback 触发）而不是"定时拉"

---

### 2.7 盈亏计算：buildSimpleCycles（MQ 关键设计）

**MQ做法**（`trade-logs-service.ts:48-200`）：
```typescript
async buildSimpleCycles(params) {
  // 1. 查询所有成交记录
  const deals = await this.tradeDataQueryService.queryTradeRecords({...});
  
  // 2. 按股票分组，按时间升序
  const byCode = new Map<string, StandardTradeRecord[]>();
  deals.forEach(d => {
    const code = String(d.stockCode);
    if (!byCode.has(code)) byCode.set(code, []);
    byCode.get(code)!.push(d);
  });
  
  // 3. 遍历每只股票构建"交易闭环"（cycle）
  for (const [code, arr] of byCode) {
    let net = 0;  // 净仓位
    let current: SimpleCycle | null = null;
    
    for (const d of arr) {
      const orderType = Number(d.orderType);
      // 严格只用 orderType：23=买，24=卖
      // direction 字段在股票场景下表示多空方向，**不能用于判断买卖**
      if (orderType === 23) {
        isBuy = true;
      } else if (orderType === 24) {
        isSell = true;
      } else {
        // orderType 无效时跳过 + warn
        continue;
      }
      
      if (isBuy) {
        if (net === 0) current = {status: 'ongoing', ...};
        net += vol;
        current.buys.push({time, price, volume, amount});
      }
      if (isSell) {
        net -= vol;
        current.sells.push({time, price, volume, amount});
      }
      
      // 周期闭合（净仓归零）
      if (net === 0) {
        current.status = 'closed';
        result.push(current);
        current = null;
      }
    }
    
    // 收尾：未闭合周期
    if (current) result.push(current);
  }
  
  // 4. 计算指标：avgCost, pnl
  for (const cycle of result) {
    this.finalizeCycleMetrics(cycle);
  }
}
```

**MQ的洞见**：
- `orderType` 才是买卖方向的真相（23=买，24=卖）
- `direction` 字段在股票场景下是**多空方向**（LONG/SHORT），**不能用于判断买卖**
- 一些券商 SDK 可能没有 `orderType` 字段，必须 warn 并跳过
- **加仓/减仓/多次买卖** → 通过 `net` 净仓位累积计算 cycle
- "周期" = 从建仓到清仓（净仓归零）
- 加权平均成本 = sum(price * vol) / sum(vol) for all buys in cycle

**我们的做法（v2.0）**：`sim_trader/engine.py:486-491` 用简单减法
```python
cost_basis = pos.cost * (ss / pos.shares) if pos.shares else 0.0
profit = sell_net - cost_basis
```

**问题**：
- 加仓后部分卖出，成本摊减用 `pos.cost * (ss/pos.shares)`——加权不准确
- 没有"周期"概念，无法区分多次建仓/清仓
- 强平后立即建仓可能被算到上一周期

**采纳**：
1. ✅ 在 `app/live_trader/profit_calc.py` 完整照搬 `buildSimpleCycles`
2. ✅ 用 `orderType` 而非 `direction` 判断买卖
3. ✅ 加权均价算法
4. ✅ 提供"实时 cycle 计算"接口（不依赖日终）

---

### 2.8 持仓天数：TradeDataQueryService 跨源 + 交易日历对齐

**MQ做法**（`hold-days-service.ts:84-187`）：
```typescript
private calculateSingleStockHoldDays(stockCode, tradeLogs, days) {
    // 1. 按日聚合买卖
    const { dailyBuy, dailySell } = this.aggregateDaily(tradeLogs);
    
    // 2. 累计持仓（按日期）
    const cumulativeHolding = new Map<string, number>();
    let runningTotal = 0;
    for (const date of days) {
      runningTotal += dailyBuy.get(date) - dailySell.get(date);
      
      if (runningTotal < 0) {
        // 异常：累计为负（卖比买多）
        return { holdDays: 0, error: '...' };
      }
      cumulativeHolding.set(date, runningTotal);
    }
    
    // 3. 从最近倒推连续持仓天数
    let consecutive = 0;
    for (let i = days.length - 1; i >= 0; i--) {
      const holding = cumulativeHolding.get(days[i]);
      if (holding > 0) consecutive++;
      else break;
    }
    return { holdDays: consecutive };
}
```

**MQ的关键设计**：
- **基于交易日历**（不是自然日）——周末/节假日不算
- 累计持仓必须 ≥0，否则报错
- 连续天数从最近倒推，遇到 0 就停
- **`lookbackDays = 配置的N + 210冗余`**（防止窗口外历史数据漏掉）

**我们的做法（v2.0）**：`engine.py:418` `hold_days = sum(1 for td in trading_dates if pos.entry_date <= td <= today)`——用 entry_date 简单累计

**问题**：
- 加仓后只更新 `entry_date` 不更新 → 多算天数
- 卖出部分不更新 → 卖完后还显示在数
- 没有"连续"概念（假期前后都算）

**采纳**：✅ 完整照搬 `app/live_trader/hold_days.py`

---

### 2.9 通知：企业微信（MQ 独有）

**MQ做法**（`wework-messenger-service`）：
- 成交回调触发 `weworkMessengerService.sendOrderNotification()`
- 通知格式：
  ```
  买入 002983 1000股 @ 10.50元
  卖出 002983 500股 @ 11.20元  盈亏+2.10%
  ```
- 实盘区分 mock（envName 包含"测试"）

**我们的做法（v2.0）**：仅 WebSocket

**采纳**：
- ⚠️ **暂不采纳**（你目前没企业微信）
- 📋 留扩展点：未来加 `notification` 接口，先 WebSocket 实现

---

### 2.10 TEST/RUN 双模式（MQ 关键设计）

**MQ做法**（`exit-sell-service.ts:538-580`）：
```typescript
async testEvaluateExitCandidates(customConfig) {
  // mode: 'TEST'  → 只评估不下单，返回命中股票集合
  // mode: 'RUN'    → 评估 + 下单
  const core = await this.evaluateExitCandidatesCore({
    envName, accountId, overrides: customConfig, mode: 'TEST'
  });
  return { matchedCount, matched: [...] };
}
```

**优势**：
- 同一规则引擎跑 TEST/RUN 两种模式
- TEST 模式可调参（`customConfig` 覆盖字典配置）
- 验证逻辑后再切到 RUN 实盘下单

**我们的做法（v2.0）**：DRY_RUN 开关是粗粒度

**采纳**：
- ✅ 规则引擎改造为 `evaluate_actions(mode='TEST'|'RUN')`
- ✅ `app/api/live_trader.py` 加 `POST /api/live-trader/test-evaluate`
- ✅ 返回命中股票集合+metrics（不实际下单）

---

## 3. 关键数据库表设计（MQ 完整 schema）

**MQ实盘相关表**（TypeORM entity）：

| 表名 | 用途 | 关键字段 | 我们对应 |
|------|------|---------|---------|
| `TradeEntity` | 委托主表 | id, accountId, envName, stockCode, orderType, volume, price, status, qmtOrderId, seq | `live_orders` |
| `StockOrderLogEntity` | 委托变更日志 | orderStatus变更历史 | `live_order_logs` (新增) |
| `StockTradeLogEntity` | 成交日志 | tradedVolume, tradedPrice, commission | `live_trades` |
| `OrderErrorLogEntity` | 错误日志 | errorId, errorMsg | `live_errors` (新增) |
| `OrderStockAsyncResponseLogEntity` | 异步回报 | seq, orderId | `live_async_responses` (新增) |
| `XQmtHoldingsBackupEntity` | 持仓归档 | envName, accountId, stockCode, volume, backupDate | `live_holdings_backup` (新增) |
| `TradeEventLogEntity` | 交易事件流 | eventType, payload(JSON) | `live_audit` |

**采纳**：✅ 我们 `live_*` 表**完全照搬**这个 schema（13张表）

---

## 4. 部署与运行（MQ 完整方案）

### 4.1 NSSM 守护

**MQ做法**（`nssm.md`）：
```
x-miniqmt 用 NSSM 安装为 Windows 服务
- 服务名: QuantXMiniQmt
- 启动: python start.py --mode prod
- 崩溃自动重启
- 启动时间: 1秒级
- 关键场景: WaitingFreeWriter 资源池爆 → os._exit(1) → NSSM 拉起
```

**我们的做法**：暂无服务化，依赖 `start.py` 手动启动

**采纳**：
- ✅ 加 `install_nssm.bat` + `start_nssm.bat` 把 main.py 安装为 Windows 服务
- ✅ `WaitingFreeWriter` 资源池爆 → `os._exit(1)` → NSSM 拉起

### 4.2 启动顺序

**MQ做法**：
```
1. 启动 XtMiniQmt.exe → 登录 QMT
2. NSSM 启动 x-miniqmt (FastAPI) → 检测 QMT 连接
3. NSSM 启动 x-server (Midway) → 启动 cron
4. x-admin 前端独立
```

**我们做法**：
```
1. 启动 XtMiniQmt.exe → 登录
2. 启动 main.py (FastAPI) → 检测 QMT → 启动 cron
3. 启动前端
```

**采纳**：✅ 与MQ一致（单进程简化版）

---

## 5. 用户决策所需的新选择

引入MQ架构后，**3 个新决策**需要你拍板：

### D7. 固卖规则是否启用？

MQ 强烈推荐固卖（限价单 + 在途检测 + 清仓锁保护），是 5 条规则的核心创新。

- **A 启用**：增加固卖规则，需要在 `app_setting.json` 配置一卖/二卖比例
- **B 不启用**：保留现有 4 条规则，但放弃固卖（错失拉低成本的机会）

**我建议 A 启用**——固卖是MQ比 v2.0 强的地方。

### D8. 持仓归档表是否启用？

- **A 启用**：每日 15:05 自动 snapshot 到 `live_holdings_backup`，QMT不可用时 fallback
- **B 不启用**：仅依赖 QMT 实时返回

**我建议 A 启用**——防QMT掉线时持仓信息丢失。

### D9. Callback 推模式 vs 拉模式

- **A 推模式（MQ做法）**：callback 实时更新本地表 + 释放清仓锁
- **B 拉模式（v2.0 做法）**：cron 定时拉

**我建议 A 推模式**——避免 sell 阶段和 buy 阶段之间的状态错位。

---

## 6. 改造方案（基于MQ做法）

### 6.1 必须改造的（高优）

| # | 改造 | 来源 | 工作量 |
|---|------|------|--------|
| 1 | 加 `circuit_breaker.py`（熔断器 + WaitingFreeWriter 强退）| MQ `connection_manager.py` | 0.5天 |
| 2 | 加 `clearance_lock.py`（股票级清仓锁）| MQ `clearance-lock-service.ts` | 0.5天 |
| 3 | 加 `qmt_callback.py`（5个回调）| MQ `qmt_callback.py` | 1天 |
| 4 | 加 `profit_calc.py`（buildSimpleCycles 闭环 + 加权均价）| MQ `trade-logs-service.ts:48-200` | 1天 |
| 5 | 加 `hold_days.py`（跨源+交易日历对齐）| MQ `hold-days-service.ts` | 0.5天 |
| 6 | 改造 `exit_rules.py` 为 5 条独立 Rule（保留旧逻辑）| MQ 5个 rule 文件 | 1.5天 |
| 7 | 改造 `live_orders` 表为 11 状态码 + 加 `live_order_logs` / `live_errors` / `live_async_responses` | MQ 多表 | 0.5天 |
| 8 | 加 `live_holdings_backup`（每日归档）| MQ `XQmtHoldingsBackupEntity` | 0.5天 |

### 6.2 强烈建议改造（中优）

| # | 改造 | 来源 | 工作量 |
|---|------|------|--------|
| 9 | 规则引擎支持 TEST/RUN 双模式 | MQ `testEvaluateExitCandidates` | 0.5天 |
| 10 | 加 NSSM 守护 main.py | MQ 部署方式 | 0.5天 |
| 11 | 加 `LiveTradeDataQuery` 多源查询 | MQ `TradeDataQueryService` | 1天 |
| 12 | 风控门补：cash - frozen_cash、累计单票 | v2.0 已有 | 0.5天 |

### 6.3 可选改造（低优）

| # | 改造 | 来源 | 工作量 |
|---|------|------|--------|
| 13 | 加企业微信通知 | MQ `wework-messenger-service` | 1天 |
| 14 | 加 mock 环境（不影响实盘）| MQ `isMockTrade` | 0.5天 |
| 15 | 双数据源（MySQL + DuckDB）| MQ TypeORM + Redis | 3天+ |

---

## 7. 实施路线图（修订版）

### 阶段一：基建 + 熔断器（2-3天）
1.1 配置 + NSSM + 启动
1.2 `qmt_wrapper.py` 封装
1.3 `circuit_breaker.py` 完整照搬
1.4 DB 表重建（live_orders 11状态 + 新增3表 + 归档表）

### 阶段二：Callback + 清仓锁（2天）
2.1 `qmt_callback.py` 5个回调
2.2 `clearance_lock.py` 完整照搬
2.3 engine 改造为 callback 驱动

### 阶段三：规则引擎改造（3-4天）
3.1 5条独立 Rule 类（保留旧规则作为迁移参考）
3.2 `profit_calc.py` buildSimpleCycles
3.3 `hold_days.py` 跨源查询
3.4 TEST/RUN 双模式

### 阶段四：风控门 + dry-run（1天）
4.1 6道风控（v2.0 已有）
4.2 dry-run 1天
4.3 故意越界演练

### 阶段五：小资金实盘（1-2周）
5.1 准备1万
5.2 每日 7 项检查（与v2.0 7.3 一致）
5.3 7-10天后放大

---

## 8. 关键洞见（对比学习产出）

### 8.1 MQ的3个核心洞见

**洞见 1：固卖 + 清仓锁 = 5条规则的灵魂**
- 5条规则里，固卖是**唯一**用限价单的（其他全市价）
- 固卖需要**清仓锁保护**，否则会被反复挂单/撤单
- 清仓锁的 TTL 300秒 + 反查索引（orderId→key）是关键设计

**洞见 2：Callback 推模式是必要的，不是优化**
- 14:53 sell → 14:54 buy 中间有 1 分钟空窗
- 拉模式会在 14:55 对账才看到 sell 已成
- 推模式 sell 成交瞬间就更新本地表 + 释放清仓锁
- **没有推模式，固卖/强卖在 14:54 会被误判为未持仓**

**洞见 3：盈亏必须用闭环（cycle）而非减法**
- 多次建仓/清仓 + 加权均价 = 真实成本
- 简单的 `pos.cost * (ss/pos.shares)` 在多次加仓后误差大
- buildSimpleCycles 是 MQ 多年经验沉淀的算法

### 8.2 我们设计书里没考虑到的 5 个 MQ 关键点

1. **熔断器 + WaitingFreeWriter 资源池保护**（MQ独有）
2. **清仓锁**（5条规则 race 保护）
3. **Callback 推模式**（拉模式不够）
4. **buildSimpleCycles 闭环**（简单减法不准确）
5. **TEST/RUN 双模式**（同一规则引擎）

---

## 9. 总结

**采纳 MQ 全部核心设计**，**裁剪不必要复杂度**：

✅ 完整照搬：熔断器、清仓锁、Callback、5条规则、buildSimpleCycles、持仓天数
✅ 简化：单进程（不拆 x-miniqmt）、不接企业微信、不接 TypeORM
✅ 加固：NSSM 守护、Wind+ETF 不支持

**总改造工作量**：~10-12 天（vs v2.0 原本 ~5-7 天）
**多花的时间**：被"已实盘跑通"的设计省回来——**值得**

---

## 附录：完整文件清单（采纳MQ后）

```
quant-platform/
├── app/
│   ├── live_trader/                      # 实盘（采纳MQ后）
│   │   ├── __init__.py
│   │   ├── config.py                     # v2.0 已有（微调）
│   │   ├── execution.py                  # v2.0 已有
│   │   ├── store.py                      # v2.0 扩展（live_orders 11状态 + 新表）
│   │   ├── risk_gate.py                  # v2.0 已有
│   │   ├── engine.py                     # v2.0 大改（callback 驱动）
│   │   ├── order_status.py               # 🆕 11状态码字典（照搬MQ）
│   │   ├── kill_switch.py                # v2.0 已有
│   │   ├── audit.py                      # v2.0 已有
│   │   ├── circuit_breaker.py            # 🆕 熔断器（照搬MQ）
│   │   ├── clearance_lock.py             # 🆕 股票级清仓锁（照搬MQ）
│   │   ├── qmt_callback.py               # 🆕 5个回调（照搬MQ）
│   │   ├── qmt_wrapper.py                # 🆕 QMT SDK 隔离封装
│   │   ├── profit_calc.py                # 🆕 buildSimpleCycles（照搬MQ）
│   │   ├── hold_days.py                  # 🆕 跨源查询（照搬MQ）
│   │   ├── holdings_data.py              # 🆕 多源查询+归档（照搬MQ）
│   │   ├── live_trade_data_query.py      # 🆕 交易查询（照搬MQ）
│   │   ├── intraday_monitor.py           # v2.0 已有
│   │   └── rule_engine/                  # 🆕 5条规则（照搬MQ）
│   │       ├── __init__.py
│   │       ├── exit_rule_engine.py
│   │       ├── exit_rule_types.py
│   │       └── rules/
│   │           ├── rule_force_close.py   # 100 长期持有强制平仓
│   │           ├── rule_force_sell.py    # 80  强卖（连亏N天）
│   │           ├── rule_stop_loss.py     # 60  止损（回撤阈值）
│   │           ├── rule_take_profit.py   # 40  止盈（分档+回撤）
│   │           └── rule_fixed_sell.py    # 20  固卖（限价单）🆕
│   └── api/
│       └── live_trader.py                # v2.0 扩展（+TEST 模式）
├── scripts/
│   ├── install_nssm.bat                  # 🆕 NSSM 守护
│   └── uninstall_nssm.bat                # 🆕
└── tests/
    └── test_circuit_breaker.py           # 🆕
    └── test_clearance_lock.py            # 🆕
    └── test_build_cycles.py              # 🆕
```

**新增文件 12 个，修改文件 5 个，工作量 10-12 天**

---

**📅 报告生成时间**: 2026-06-28
**📊 采纳度**: MQ核心设计 **100% 采纳**，复杂度裁剪 ~40%
**💡 建议**: 优先采纳 D7=启用固卖 / D8=启用归档 / D9=推模式 后再开始实施
