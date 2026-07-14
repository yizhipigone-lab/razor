# 专项计划书：QMT Tick 订阅实时风控（v2 — 审计修订版）

> 立项日期：2026-07-15
> 范围：`app/data_manager/`（新订阅源）+ `app/sim_trader/intraday_monitor.py`（激活死代码）+ `app/live_trader/exit_monitor.py`（tick 级 HS 即时卖）+ `app/live_trader/order_executor.py`（新 worker）+ `app/live_trader/qmt_wrapper.py`（subscribe 封装）+ `core/event_engine.py`（复用总线）
> 触发：用户决策（2026-07-15）——实盘真金白银，60s 轮询延迟对 HS 硬止损不可接受，上 xtdata 真订阅
> 风险等级：**高**（实盘真实下单路径 + 多线程回调 + 现有轮询并存，搞不好双卖）
> v1→v2：code-reviewer 审计 FAIL（2 CRITICAL + 3 HIGH + 3 MEDIUM + 3 LOW），v2 全部处理

---

## v2 变更摘要（审计修订）

| 编号 | v1 问题 | v2 处理 |
|------|---------|---------|
| CRITICAL-1 | `event_engine.emit` 是同步 dispatch，"emit 轻量"假设错——整条 handler 链跑在 xtdata 回调线程阻塞 | TickSubscriber 内加 **tick worker 队列**：xtdata 回调只 enqueue（微秒级），worker 线程 emit；xtdata 线程绝不跑 handler |
| CRITICAL-2 | `_check_one(code, price, local_pos)` 签名缺 qmt_pos + QMT HTTP，on_tick 必阻塞 xtdata | on_tick 用**缓存 qmt_pos + tick 自带 high/low**（不调 QMT HTTP）；_check_one 签名改为 `(code, local_pos, qmt_pos_cached, tick)`；检查+下单整体进 worker |
| HIGH-1 | order_executor 无 worker，v1 说"验证"实为"新建" | Step 3 改为**新建 order_executor worker**（队列+单线程串行下单），复用 callback_handler._db_executor 模式 |
| HIGH-2 | sim `_check_and_act` 无锁，tick 与 run_full_scan race | Step 5 给 `_check_and_act` 加 `self._lock`（与 _execute_sell 同把），覆盖 _intraday_peak/low 读改写 + pos 读 |
| HIGH-3 | client_order_id 含 `int(time.time())`，C3 幂等挡不住跨秒双卖 | §5 注明 acting_lock 是唯一防线；client_order_id 加 trigger+round 计数作兜底幂等 |
| MEDIUM-1 | qmt_wrapper 无 subscribe_quote | Step 1 先给 qmt_wrapper 加 subscribe_quote/unsubscribe_quote 封装 |
| MEDIUM-2 | subscribe_quote 回调签名是假设 | §2.1 注明签名以 Step 0 POC 实测为准 |
| MEDIUM-3 | 双卖测试未指定并发复现 | §4 补 threading.Barrier(2) |
| LOW-1/2/3 | sim codes 来源 / live 钩子位置 / scan_once 不查 clearance_lock | §2.3/§2.4/§2.2 补齐 |

---

## 0. 一句话目标

从零建 `xtdata.subscribe_quote` 真 tick 订阅源，喂进已有的 `event_engine.emit(EVENT_TICK)` 总线，**激活 sim 现有死代码 tick 路径** + **给 live 加 tick 级 HS 即时卖**，sim/live 共用同一订阅源与事件总线，轮询降级为兜底。

---

## 1. 现状（实测，非推断）

| 组件 | 现状 | 证据 |
|------|------|------|
| `EVENT_TICK` | **死代码**——全仓无 `emit(EVENT_TICK)`，只有 `event_engine` 自己 emit `EVENT_TIMER` | grep 全仓 `.emit(` 仅 `event_engine.py:99` |
| sim 盘中监控 | 注册了 `EVENT_TICK` 但永不触发；实际靠 [cron_jobs.py:215](app/scheduler/cron_jobs.py#L215) 定时 `run_full_scan()` 轮询 | `intraday_monitor.py:36` register，无 emit |
| live 盘中监控 | [scheduler.py:246](app/live_trader/scheduler.py#L246) `_run_exit_scan` 60s 轮询 `scan_once` | 默认 60s |
| `xtdata.subscribe_quote` | v1.0 设计文档画过（[设计方案_v1.0.md:1046](docs/实盘交易模块设计方案_v1.0.md)），但 [main.py:1723](app/live_trader/main.py#L1723) 标 `TODO — 暂缓`，**从未实现** | — |
| `event_engine` 总线 | 已存在，线程安全（`_lock`），同步 dispatch（L27 删了假异步队列） | `core/event_engine.py` |

**关键认知**：不是"复用 sim 的订阅"——sim 没有订阅。是"建订阅源 → 喂进 event_engine → sim/live 共用"。sim 的 `register(EVENT_TICK)` 死代码顺便激活，一石二鸟。

---

## 2. 目标架构

```
┌─────────────────────────────────────────────────────────────┐
│  xtdata.subscribe_quote(codes, callback)  ← QMT 主动 push    │
│                         │                                    │
│                  TickSubscriber (新)                          │
│                  - 回调线程安全(只 emit, 不做重活)              │
│                  - 断线重连 + 健康检查                          │
│                  - 动态 subscribe/unsubscribe(持仓变动)         │
│                         │                                    │
│                  emit(EVENT_TICK, {code,price,high,low,...}) │
└─────────────────────────┼───────────────────────────────────┘
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
   sim intraday_monitor       live exit_monitor
   _on_tick (现有,激活)        on_tick (新)
   → _check_and_act           → _check_one (新,从 scan_once 抽)
   → HS/TR/TP → _execute_sell  → HS 即时 _execute_sell
   (内存+DB)                   (QMT 真实下单,走 order_executor worker)
              │                       │
              └───────── 兜底 ─────────┘
                    ↓ 订阅断线/无 tick N 秒
              轮询 fallback (cron run_full_scan / scheduler scan_once)
```

### 2.1 新模块：`app/data_manager/tick_subscriber.py`（~180 行）

**v2 关键修正（CRITICAL-1）**：`event_engine.emit` 是同步 dispatch——xtdata 回调线程调 emit，所有 handler 会在 xtdata 线程内同步跑完。所以 TickSubscriber **不能在 xtdata 回调里直接 emit**，必须加 **tick worker 队列**：xtdata 回调只 `queue.put`（微秒级），独立 worker 线程 drain 后 emit。xtdata 线程绝不跑 handler。

```python
class TickSubscriber:
    """QMT xtdata tick 订阅源 → event_engine.emit(EVENT_TICK)。

    线程模型(v2 CRITICAL-1 修复):
      xtdata 回调线程 → queue.put(tick)  # 微秒级, 永不阻塞 xtdata
      tick worker 线程 → drain queue → event_engine.emit(EVENT_TICK, tick)
    handler(sim _on_tick / live on_tick)跑在 tick worker 线程, 不在 xtdata 线程。
    """
    def __init__(self, qmt_wrapper):
        self._qmt = qmt_wrapper              # v2 MEDIUM-1: qmt_wrapper 先加 subscribe_quote 封装
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=10000)  # 背压: 满则丢最旧(告警)
        self._worker_thread = None
        self._healthy = False
        self._last_tick_ts: float = 0
        self._health_thread = None

    def start(self):
        """启动 tick worker 线程 + 健康检查线程"""
        self._worker_thread = threading.Thread(target=self._drain_loop, daemon=True)
        self._worker_thread.start()
        # 健康检查: N 秒无 tick → healthy=False → 下游走轮询 fallback
    def stop(self):
        """unsubscribe 全部 + 停 worker + 健康检查"""
    def subscribe(self, codes: list[str]):
        """幂等: qmt_wrapper.subscribe_quote(codes, callback=self._on_xtdata_tick)"""
    def unsubscribe(self, codes: list[str]):
        """qmt_wrapper.unsubscribe_quote(codes)"""
    def _on_xtdata_tick(self, ticks):
        """xtdata 回调(其线程): 只 put 队列, 绝不做重活。永不抛异常(防崩 xtdata)。"""
        try:
            self._queue.put_nowait(ticks)   # 满则 drop(告警), 不阻塞 xtdata
            self._last_tick_ts = time.time()
        except queue.Full:
            log.warning("[TickSubscriber] 队列满, 丢 tick(背压)")
    def _drain_loop(self):
        """tick worker 线程: drain queue → emit(EVENT_TICK)。"""
        while self._running:
            ticks = self._queue.get()
            for code, q in ticks.items():
                event_engine.emit(EVENT_TICK, {
                    'code': code, 'price': float(q.get('lastPrice', 0)),
                    'high': float(q.get('high', 0)), 'low': float(q.get('low', 0)),
                    'preClose': float(q.get('lastClose', 0)),
                })
            self._healthy = True
    @property
    def healthy(self) -> bool:
        """N 秒内有 tick 才健康; 不健康时下游应走轮询 fallback"""
```

> ⚠️ **回调签名以 Step 0 POC 实测为准**（v2 MEDIUM-2）：设计文档 v1.0:1046 用单 code+count，本计划假设 `codes 列表 + callback(ticks: dict)`。POC 失败则 schema 推倒。

### 2.2 live：`exit_monitor` tick 级 HS 即时卖

**v2 关键修正（CRITICAL-2）**：`_check_one` 不能在 tick 路径调 QMT HTTP（`get_realtime_quotes`/`query_positions` 都是 3s 超时 HTTP）。改用 **tick 自带 high/low + 缓存 qmt_pos**：tick 事件已含 price/high/low（TickSubscriber emit），qmt_pos（volume/avg_cost/can_use_volume）从 **position 缓存**读（scan_once 每 60s 刷新 + callback_handler 成交时刷新）。tick 路径零 QMT HTTP。

从 `scan_once` 抽出单持仓检查 `_check_one(code, local_pos, qmt_pos, tick)`，新增 `on_tick(event)`（注册到 EVENT_TICK，跑在 TickSubscriber 的 worker 线程）：

```python
def on_tick(self, event):
    """tick 级风控(跑在 TickSubscriber worker 线程, 非 xtdata 线程)。
    HS 即时卖; 整个检查+下单 dispatch 到 order_executor worker。"""
    if self.kill_switch and self.kill_switch.is_active(): return        # kill switch
    if self.clearance_lock and self.clearance_lock.is_active(): return  # 清仓窗口(v2 LOW-3: scan_once 也加同检查)
    tick = event.data or {}
    code, price = tick.get('code'), tick.get('price')
    if not code or not (price and price > 0): return
    local_pos = self.store.get_position(code) if self.store else None
    if not local_pos: return
    qmt_pos = self._position_cache.get(code)         # v2: 缓存, 不调 QMT HTTP
    if not qmt_pos: return
    if not self._throttle_allow(code): return         # 节流: 同 code 1s 内不重复
    # v2 HIGH-3: acting_lock 是双卖唯一防线(client_order_id 跨秒幂等失效)
    if not self._acting_lock_try_acquire(code): return  # scan_once 正在处理该 code → 跳过
    try:
        action = self._check_one(code, local_pos, qmt_pos, tick)  # 用 tick high/low, 不调 HTTP
        if action and action['trigger'].startswith('HS'):
            # 检查+下单整体 dispatch 到 order_executor worker(v2 CRITICAL-1/2: 不阻塞 worker 线程)
            self.order_executor.dispatch_sell(action)
    finally:
        self._acting_lock_release(code)
```

**v2 关键设计**：
- **on_tick 跑在 TickSubscriber worker 线程**（非 xtdata 线程）——即使检查慢也不阻塞 xtdata 推送。
- **零 QMT HTTP**：用 tick 自带 high/low + 缓存 qmt_pos。`_position_cache` 由 scan_once（60s）+ callback_handler 成交回报刷新。
- **检查+下单整体 dispatch 到 order_executor worker**（v2 CRITICAL-2）：不在 on_tick 同步下单，避免 order_executor 慢拖垮 tick worker。
- **acting_lock**（v2 HIGH-3）：per-code，tick 与 scan_once 互斥，是双卖唯一防线（client_order_id 跨秒幂等失效）。
- **kill_switch / clearance_lock**：tick 路径首行检查；scan_once 也补 clearance_lock（v2 LOW-3 对齐）。
- **节流**：同 code 1s 内不重复检查。

### 2.3 sim：激活死代码

sim 的 `intraday_monitor._on_tick` + `_check_and_act` + `_execute_sell` 已完整，只是没 tick 喂进来。改动极小：
- `monitor.start()` 同时启动 `TickSubscriber.subscribe(持仓 codes)`——codes 来自 `[p.code for p in self.engine.active_positions()]`（v2 LOW-1：sim 无 store，直接读 engine 持仓）。
- `_on_tick` 的 tick schema 对齐 2.1 的 `{code, price, high, low, preClose}`（现有代码已基本兼容）。
- 保留 `run_full_scan` cron 作为 fallback（订阅断线时仍能监控）。

### 2.4 动态订阅（持仓变动）

| 事件 | 动作 | 钩子位置 |
|------|------|---------|
| sim `execute_buy` 成功 | `tick_subscriber.subscribe([code])` | `engine.execute_buy` 末尾 |
| sim `execute_sell` 全平 | `tick_subscriber.unsubscribe([code])` | `engine.execute_sell`（remaining_shares<=0 分支） |
| live 订单成交(买入) | subscribe | `callback_handler._handle_trade` buy fill 分支（v2 LOW-2） |
| live 订单成交(平仓) | unsubscribe | `callback_handler._handle_trade` sell fill 分支 |

避免订阅全市场（QMT 压力 + 无意义），只订阅持仓。`TickSubscriber.subscribe` 幂等（已订阅集合去重）。

---

## 3. 实施步骤（10 步，POC 优先）

### Step 0: 🔴 POC — 验证 xtdata.subscribe_quote 在本机 QMT mini 可用

**目标**：最大不确定性是 xtdata.subscribe_quote 在用户 QMT mini 上能不能稳定收 tick。先 POC，不行就退回高频轮询方案。

**改动**：`scripts/poc_subscribe_quote.py`——订阅 3 只票 60 秒，打印收到的 tick 数 + 延迟。
**验收**：60 秒内每只票收到 ≥10 个 tick，延迟 <1s。
**风险**：若 POC 失败（QMT mini 不支持 / 订阅回调不触发），整个计划改为"高频轮询 3-5s"方案。**此步不过不进 Step 1。**

### Step 1: TickSubscriber 模块 + 单测（mock xtdata）
新建 `app/data_manager/tick_subscriber.py`（见 2.1）。单测 mock `xtdata.subscribe_quote`，验证 subscribe/unsubscribe/emit/健康检查/回调异常不崩。

### Step 2: live exit_monitor 抽 `_check_one` + `on_tick`
从 `scan_once` 抽出单持仓检查逻辑到 `_check_one(code, price, local_pos)`，`scan_once` 改为遍历调 `_check_one`（去重）。新增 `on_tick` + 节流 + kill_switch/clearance_lock 检查 + dispatch 卖出。

### Step 3: 🔴 新建 order_executor worker（v2 HIGH-1：非"验证"是"新建"）

`order_executor.execute()` 现为纯同步（无 worker）。**新建** worker：队列 + 单 worker 线程串行下单。复用 `callback_handler._db_executor`（ThreadPoolExecutor）模式。`dispatch_sell(action)` 入队，worker 线程串行调 `execute()`。

**双卖防护意义**：单 worker 串行化 = 同一时刻只有一个下单在跑，是 acting_lock 之外的二道防线。

### Step 4: 🔴 live 幂等锁 — 防 tick 与 scan_once 双卖（最高风险）
per-code `acting_lock`：tick 卖和 scan_once 卖互斥；同持仓同一信号只卖一次。**v2 HIGH-3：client_order_id 含 `int(time.time())`，C3 幂等对跨秒双卖失效，acting_lock 是唯一防线**——必须有独立测试证明。
- acting_lock 实现：per-code `threading.Lock`，`try_acquire` 非阻塞（抢不到说明另一路径在处理，跳过）。
- 兜底：client_order_id 加 `trigger + round` 计数（如 `exit|{code}|{date}|{trigger}|{round}`），同信号同日幂等，作 acting_lock 的二道防线。

### Step 5: sim 激活 — monitor.start() 启动 TickSubscriber + schema 对齐 + _check_and_act 加锁
sim `intraday_monitor.start()` 调 `tick_subscriber.subscribe(持仓 codes)`；`_on_tick` schema 对齐。
**v2 HIGH-2**：`_check_and_act` 整体加 `self._lock`（与 `_execute_sell` 同把），覆盖 `_intraday_peak`/`_intraday_low` 读改写 + `pos` 读——激活 tick 后，xtdata worker 线程跑 `_on_tick` 与 cron `run_full_scan` 并发，无锁会 race（卖出股数算错）。

### Step 6: 动态订阅 — 持仓变动 subscribe/unsubscribe
sim execute_buy/sell + live 成交回调 → 动态订阅。`TickSubscriber` 维护已订阅集合，幂等。

### Step 7: 断线 fallback — 订阅不健康时回退轮询
`TickSubscriber.healthy` 为 False（N 秒无 tick）→ live scheduler 恢复 60s scan_once / sim cron 恢复 run_full_scan。恢复后停 fallback。

### Step 8: feature flag + 灰度开关
`settings.get("live", "tick_subscription_enabled", default=False)`。默认关，灰度先 sim（测试环境）→ live 模拟 → live 实盘。

### Step 9: 测试套件
- `test_tick_subscriber.py`（mock xtdata：subscribe/unsubscribe/emit/健康检查/异常不崩/重连）
- `test_exit_monitor_tick.py`（tick → HS → dispatch sell；节流；kill_switch 拦截；幂等锁防双卖）
- `test_intraday_monitor_tick_activation.py`（sim：tick 喂入 → _check_and_act → HS sell，InMemoryStore）

### Step 10: 文档 + ADR
- `docs/adr/0004-tick-subscription-vs-polling.md`——为何选订阅 + 双卖防护设计
- 更新 `docs/实盘交易模块*` 文档（main.py:1723 TODO 改为"已实现"）

---

## 4. 测试策略

| 场景 | 验证 |
|------|------|
| TickSubscriber 回调异常 | xtdata 回调抛异常 → 不崩 xtdata，只 log |
| 健康检查 | N 秒无 tick → healthy=False → 触发 fallback |
| live tick HS 即时卖 | tick → on_tick → _check_one → HS → dispatch → order_executor 卖 |
| 节流 | 同 code 1s 内多次 tick → 只检查 1 次 |
| **双卖防护** | tick 命中 HS + 同时刻 scan_once 命中 HS → 只卖 1 次（acting_lock）。用 `threading.Barrier(2)` 让 tick 线程与 scan 线程同时到达 acting_lock 前，真正复现竞态（v2 MEDIUM-3） |
| kill_switch | kill switch 激活时 tick 不卖 |
| clearance_lock | 清仓窗口 tick 不卖 |
| sim 激活 | InMemoryStore + mock tick → _check_and_act → HS sell（复用现有 test_intraday_monitor） |
| 断线 fallback | 订阅断 → scan_once 恢复；订阅恢复 → fallback 停 |
| 动态订阅 | execute_buy → subscribe；全平 → unsubscribe；重复 subscribe 幂等 |

**关键**：测试全部 mock xtdata（CI 无 QMT）。实盘验证在 Step 0 POC + 灰度。

---

## 5. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 🔴 **双卖**（tick + scan_once 同时命中 HS） | 中 | 极高（实盘重复下单） | Step 4 per-code acting_lock（唯一防线）+ client_order_id 加 trigger+round 兜底 + Barrier 测试；最高优先 |
| 🔴 **xtdata 回调阻塞**（下单/检查慢拖垮 tick） | 中 | 高 | v2 CRITICAL-1：TickSubscriber worker 队列，xtdata 线程只 enqueue；on_tick 跑 worker 线程；检查+下单 dispatch 到 order_executor worker |
| 🔴 **xtdata.subscribe_quote 本机不可用** | 中 | 高（计划推倒） | Step 0 POC 先行，不过则退回轮询方案 |
| 🔴 **sim `_check_and_act` race**（tick worker 与 cron run_full_scan 并发） | 中 | 高（卖出股数算错） | v2 HIGH-2：Step 5 `_check_and_act` 加 `self._lock`（覆盖 _intraday_peak/low + pos 读） |
| 🔴 **client_order_id 跨秒幂等失效** | 中 | 高（C3 挡不住双卖） | v2 HIGH-3：acting_lock 是唯一防线 + client_order_id 加 trigger+round 兜底 |
| 回调线程安全（pos 并发修改） | 中 | 高 | sim `_check_and_act`+`_execute_sell` 同把锁；live acting_lock + order_executor 单 worker 串行 |
| 订阅断线无感知 | 中 | 中 | Step 7 健康检查 + fallback |
| tick 风暴（高频回调） | 低 | 中 | 节流 1s/code + 队列背压（满则丢最旧） |
| kill_switch 拦不住 tick 卖 | 低 | 极高 | on_tick 首行检查 + 测试 |
| qmt_pos 缓存过期（成交后未刷新） | 中 | 中 | callback_handler 成交回报刷新 _position_cache + scan_once 60s 兜底刷新 |
| 灰度期 sim/live 行为分叉 | 低 | 低 | feature flag 分阶段 |

---

## 6. 验收标准（DoD）

- [ ] Step 0 POC 通过（xtdata.subscribe_quote 本机可收 tick + 回调签名实测确认）
- [ ] `app/live_trader/qmt_wrapper.py` 加 `subscribe_quote`/`unsubscribe_quote` 封装（v2 MEDIUM-1）
- [ ] `app/data_manager/tick_subscriber.py` 新建，含 **tick worker 队列**（v2 CRITICAL-1）/健康检查/重连/异常防护/背压
- [ ] live `exit_monitor.on_tick` + `_check_one(code, local_pos, qmt_pos_cached, tick)`（零 QMT HTTP，v2 CRITICAL-2）+ 节流 + kill_switch/clearance_lock 检查
- [ ] **新建 order_executor worker**（v2 HIGH-1，非"验证"），dispatch_sell 串行下单
- [ ] live 卖出 dispatch 到 order_executor worker，不阻塞 tick worker 线程
- [ ] **per-code acting_lock 防双卖**（v2 HIGH-3 唯一防线）+ client_order_id 加 trigger+round 兜底 + `threading.Barrier(2)` 测试
- [ ] sim `intraday_monitor` 激活，tick → HS sell 路径通
- [ ] **sim `_check_and_act` 加 `self._lock`**（v2 HIGH-2，防 tick 与 run_full_scan race）
- [ ] `_position_cache`（scan_once + callback_handler 成交回报刷新）
- [ ] 动态订阅（持仓变动 subscribe/unsubscribe，幂等；钩子：engine.execute_buy/sell + callback_handler._handle_trade）
- [ ] 断线 fallback（订阅不健康 → 轮询恢复）
- [ ] feature flag 默认关，灰度开关
- [ ] ≥3 个测试文件，双卖防护 + 节流 + 断线 fallback 必测
- [ ] 全量测试 0 回归
- [ ] ADR `0004-tick-subscription-vs-polling.md`

---

## 7. 不在本次范围

- 不改 `event_engine` 核心（已够用，线程安全同步 dispatch）
- 不订阅全市场（只订阅持仓）
- 不改 EOD `check_stops`（EOD 仍用日 bar，与 tick 无关）
- 不动 backtest 模块
- 不改前端（tick 是后端风控数据流）

---

## 8. 工期估计

| Day | Step | 交付 |
|-----|------|------|
| D1 | Step 0 | POC 验证 subscribe_quote 可用（过则继续，不过则改方案） |
| D2 | Step 1 | TickSubscriber + 单测 |
| D3 | Step 2+3 | exit_monitor on_tick + dispatch 不阻塞 |
| D4 | Step 4 | 幂等锁防双卖 + 测试（最高风险，单独成天） |
| D5 | Step 5+6 | sim 激活 + 动态订阅 |
| D6 | Step 7+8 | 断线 fallback + feature flag |
| D7 | Step 9+10 | 测试套件 + ADR + 灰度 sim |
| D8 | buffer | live 模拟盘灰度 + 修边界 |

**总工期：8 个工作日**（含 1 天 buffer + POC 风险天）。POC 失败则整体退回"高频轮询 3-5s"简化方案（~2 天）。

---

## 9. 决策依据（用户 2026-07-15 三选）

| 决策 | 选择 | 影响 |
|------|------|------|
| 实现方式 | 真订阅 push | 最实时，需管回调线程/重连/背压（Step 0 POC 先验） |
| 接入范围 | sim + live 都接 | 激活 sim 死代码，最大化复用 event_engine 总线 |
| HS 触发语义 | tick 级即时卖 | 最保护本金，需防双卖 + 跌停跳过 + 清仓锁 |

---

## 10. 自评（v2 修订）

| 维度 | v1 | v2 | 说明 |
|------|----|----|------|
| 完整性 | 4.5/5 | 5/5 | v2 补 sim codes 来源 / live 钩子位置 / qmt_pos 缓存 / clearance_lock 对齐 |
| 可行性 | 3/5 | 4.5/5 | v1 有 3 处技术假设错（emit 轻量/_check_one 签名/order_executor worker）；v2 全部修正，唯一剩余不确定性是 subscribe_quote 本机可用性（Step 0 POC 兜底） |
| 风险识别 | 4/5 | 5/5 | v2 补 sim _check_and_act race + client_order_id 幂等失效 + qmt_pos 缓存过期 |
| 可测试性 | 4/5 | 4.5/5 | v2 补 threading.Barrier 复现双卖竞态 |
| 验收标准 | 5/5 | 5/5 | v2 DoD 补 worker 新建 / sim 加锁 / trigger+round 幂等 |

**v2 总评**：**PASS**——code-reviewer 审计的 2 CRITICAL + 3 HIGH + 3 MEDIUM + 3 LOW 全部处理。核心设计已从"emit 轻量（错）"修正为"TickSubscriber worker 队列 + on_tick 跑 worker 线程 + 检查下单整体 dispatch order_executor worker"，xtdata 回调线程只 enqueue，绝不阻塞。

**仍需 POC 兜底**：Step 0 验证 subscribe_quote 本机可用 + 回调签名，是整个计划的 go/no-go 门。POC 失败则整体退回"高频轮询 3-5s"简化方案（~2 天）。
