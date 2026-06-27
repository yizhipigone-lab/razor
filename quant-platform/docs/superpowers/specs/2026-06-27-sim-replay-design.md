# 模拟盘"历史回放"功能 Spec

> 日期:2026-06-27
> 项目:quant-platform 交易控制 TAB
> 范围:前端"历史回放"按钮 → API → TDX 5m 回测 → 结果注入 SimTraderStore

---

## 0. 上下文

### 现有能力

| 组件 | 状态 | 说明 |
|------|------|------|
| `app/backtest/tdx_runner.run_tdx_backtest()` | ✅ 可用 | QUANTQQ + 5m/1m/daily 全市场回测 |
| `scripts/run_quantqq_backtest.py` | ✅ 可用 | CLI 调用 run_tdx_backtest,输出 JSON/CSV |
| `app/sim_trader/` | ✅ 可用 | 模拟盘引擎（实时逐日交易，尾盘 14:52→14:54） |
| `app/sim_trader/main.py` | ✅ 可用 | CLI 历史回放（但用"盘整突破"+日线） |
| `app/api/sim_trader.py` | ✅ 可用 | 前端 API（status/trades/equity/execute/reset/config） |
| `app/sim_trader/store.py` | ✅ 可用 | DuckDB/JSON 双模式持久化 |
| 前端"交易控制"TAB | ✅ 可用 | 净值图/持仓/交易记录/日历/盈亏分析/日志 |
| 前端"历史回放"按钮 | ❌ 缺失 | **本次要加** |

### 用户需求

- 在"交易控制"TAB 中加一个按钮
- 点击后从 2026.1.1 到今天的完整历史交易回放
- 策略用 QUANTQQ（通达信公式），精度用 5 分钟线
- 一键跑完，自动展示结果
- 结果复用现有 UI（净值图/交易记录/持仓等）

---

## 1. 方案：API 桥接

选中方案 A —— 新增 `/api/sim-trader/replay` API 端点，桥接 `run_tdx_backtest` 到 `SimTraderStore`。

### 架构

```
前端"交易控制"TAB
  ├─ [⏪ 历史回放] 按钮 (新增)
  │   └─ POST /api/sim-trader/replay
  │       → 后台线程 run_tdx_backtest()
  │       → 结果写入 SimTraderStore
  │       → WebSocket 进度推送
  ├─ [进度条] (新增，完成后自动隐藏)
  ├─ [净值图]  ← GET /api/sim-trader/equity (已有，不变)
  ├─ [持仓表]  ← GET /api/sim-trader/status (已有，不变)
  ├─ [交易记录] ← GET /api/sim-trader/trades (已有，不变)
  └─ [日历/盈亏分析/日志] (已有，不变)
```

### 不修改的模块

- `app/backtest/tdx_runner.py` — 零改动，纯调用
- `app/backtest/simple_runner.py` — 零改动
- `app/backtest/execution.py` — 零改动
- `app/backtest/exit_rules.py` — 零改动
- `app/sim_trader/engine.py` — 零改动
- `app/sim_trader/config.py` — 零改动（参数从它读）
- `app/sim_trader/data_loader.py` — 零改动
- `app/sim_trader/intraday_monitor.py` — 零改动
- 前端 UI 布局/样式 — 零改动（只在按钮行加一个按钮 + 进度条）

---

## 2. 详细设计

### 2.1 后端：新增 API 端点

**文件**: `app/api/sim_trader.py`

**新增端点**: `POST /api/sim-trader/replay`

```python
@router.post("/api/sim-trader/replay")
async def sim_trader_replay(body: dict):
    """
    启动历史回放任务（后台线程）
    body: {
        start_date: "2026-01-01",  // 可选，默认 2026-01-01
        end_date: "2026-06-27",    // 可选，默认今天
        strategy: "QUANTQQ",       // 可选，默认 "QUANTQQ"
        period: "5m",              // 可选，默认 "5m"
    }
    返回: { status: "started", task_id: "..." }
    """
```

**状态管理**：
- 新增模块级变量 `_replay_state = {"running": False, "progress": 0, "stage": "", "task_id": ""}`
- 如果已有回放任务在跑，返回 `{ status: "busy" }`
- 后台线程 daemon=True，不阻塞 API

**后台线程流程**：
1. 设置 `_replay_state = {"running": True, "progress": 0}`
2. 通过 `sync_broadcast` 推送 `replay_start`
3. 从 `app.sim_trader.config` 读取所有风控参数（与现有 `run_quantqq_backtest.py` 完全一致的方式）
4. 调用 `run_tdx_backtest(params, progress_cb=replay_progress_cb, stop_event=None)`
5. 得 `result = { status, summary, trades, equity, indices }`
6. 写入 `SimTraderStore`：
   - `store.clear_all()` 清空旧数据
   - 逐笔 `store.save_trade()`（格式转换见 2.2 节）
   - 逐日 `store.save_equity_point()`
   - `store.save_state()` 写终态
7. 替换 `_engine`: 创建新的 SimTraderEngine 从 store 恢复
8. 推送 `replay_complete`（含 summary）
9. 设置 `_replay_state = {"running": False}`

**进度回调** (`replay_progress_cb`):
```python
def replay_progress_cb(stage, total, msg):
    _replay_state["progress"] = int(stage / total * 100)
    _replay_state["stage"] = msg
    sync_broadcast({
        "type": "replay_progress",
        "percent": _replay_state["progress"],
        "stage": msg,
        "task_id": _replay_state["task_id"],
    })
```

**新增端点**: `GET /api/sim-trader/replay-status`
```python
@router.get("/api/sim-trader/replay-status")
async def sim_trader_replay_status():
    return {"status": "ok", "replay": _replay_state}
```

### 2.2 数据格式转换

`run_tdx_backtest` 返回的 `Trade` 格式（simple_runner.Trade, `__slots__`）→ `SimTraderEngine.Trade`（dataclass）：

| 字段 | tdx_runner 来源 | sim_trader 目标 |
|------|----------------|-----------------|
| code | `t.code` | `t.code` |
| entry_date | `t.entry_date` | `t.entry_date` |
| exit_date | `t.exit_date` | `t.exit_date` |
| entry_price | `t.entry_px` | `t.entry_price` |
| exit_price | `t.exit_px` | `t.exit_price` |
| shares | `t.shares` | `t.shares` |
| return_pct | `t.ret` | `t.return_pct` |
| profit_amount | `t.profit` | `t.profit_amount` |
| exit_reason | `t.reason` | `t.exit_reason` |
| hold_days | `t.hold` | `t.hold_days` |
| entry_reason | — | `"QUANTQQ"` |
| exit_timing | — | `"close"` (默认) |
| entry_time | `t.entry_time` | `t.entry_time` |
| exit_time | `t.exit_time` | `t.exit_time` |

**净值数据**（tdx_runner 已生成 `equity[]` 含 date/equity/cash/pos，可直接写入 `sim_equity` 表）。

### 2.3 前端：HTML

**文件**: `static/index.html`

在"当前持仓"卡片的按钮行（line 1314-1317）新增一个按钮：

```html
<button class="btn btn-accent btn-sm" id="btn-sim-replay"
        onclick="startReplay()">⏪ 历史回放</button>
```

在"当前持仓"卡片上方新增进度条（默认隐藏）：

```html
<div id="replay-progress" style="display:none; margin-bottom:12px;
    padding:10px 14px; background:var(--card-bg);
    border-radius:8px; border:1px solid var(--border)">
  <div style="display:flex; justify-content:space-between; margin-bottom:4px">
    <span style="font-size:13px; font-weight:bold">⏳ 历史回放进行中...</span>
    <span id="replay-pct" style="font-size:12px; color:var(--text2)">0%</span>
  </div>
  <div style="background:var(--border); border-radius:4px; height:8px; overflow:hidden">
    <div id="replay-bar" style="background:var(--accent); height:100%;
        width:0%; transition:width 0.3s"></div>
  </div>
  <div id="replay-stage" style="font-size:11px; color:var(--text2); margin-top:4px">准备中...</div>
</div>
```

### 2.4 前端：JS

**文件**: `static/js/main.js`

**新增函数**:

```javascript
// ── 历史回放 ──────────────────────────────────────
function startReplay() {
  if (typeof ws !== 'undefined' && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'start_replay', payload: {
      start_date: '2026-01-01',
      end_date: '2026-06-27',
      strategy: 'QUANTQQ',
      period: '5m',
    }}));
  } else {
    // WebSocket 未连接则直接 POST
    fetch('/api/sim-trader/replay', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_date: '2026-01-01',
        end_date: '2026-06-27',
        strategy: 'QUANTQQ',
        period: '5m',
      })
    }).then(r => r.json()).then(d => {
      if (d.status === 'started') {
        showReplayProgress();
      }
    });
  }
  // 禁用按钮防止重复点击
  document.getElementById('btn-sim-replay').disabled = true;
  showReplayProgress();
}

function showReplayProgress() {
  document.getElementById('replay-progress').style.display = 'block';
}

function hideReplayProgress() {
  document.getElementById('replay-progress').style.display = 'none';
  document.getElementById('btn-sim-replay').disabled = false;
}
```

**修改 WebSocket 消息处理器**（在现有 `onmessage` 回调中增加）：

```javascript
// 回放进度
if (msg.type === 'replay_progress') {
  document.getElementById('replay-bar').style.width = msg.percent + '%';
  document.getElementById('replay-pct').textContent = msg.percent + '%';
  document.getElementById('replay-stage').textContent = msg.stage;
}
// 回放完成
if (msg.type === 'replay_complete') {
  hideReplayProgress();
  // 刷新所有 UI 组件
  loadSimTraderStatus();
  renderSimEquityChart();
  loadSimTrades();
  renderSimCalendar();
  renderSimStockAnalysis();
  showToast('回放完成: ' + msg.summary.total_return + '% 收益, '
    + msg.summary.trades + ' 笔交易');
}
```

### 2.5 WebSocket 消息协议

新增消息类型：

| type | 方向 | payload | 说明 |
|------|------|---------|------|
| `replay_start` | S→C | `{task_id}` | 回放开始 |
| `replay_progress` | S→C | `{percent, stage, task_id}` | 进度更新 |
| `replay_complete` | S→C | `{task_id, summary}` | 回放完成，summary 与 run_tdx_backtest 返回一致 |
| `start_replay` | C→S | `{start_date, end_date, strategy, period}` | 前端触发回放（WebSocket 通道） |

### 2.6 后端 WebSocket 处理

**文件**: 检查 `server/websocket/manager.py`，在其 `on_message` 中新增对 `start_replay` 的处理，转发到 `sim_trader_replay` 逻辑。

如果 WebSocket 框架不支持客户端发自定义消息，降级方案：前端先走 HTTP POST `/api/sim-trader/replay`，WebSocket 只用于服务端→客户端推送（progress/complete）。

---

## 3. 错误处理

| 场景 | 处理 |
|------|------|
| TDX 连接失败（bridge 不可用） | `run_tdx_backtest` 返回 `{status:"error", message:"..."}`, API 返回 500 + 错误信息前端展示 |
| 5m 数据不可用 | tdx_runner 内部自动降级 1m→5m→daily，最终报错才终止 |
| 已有回放任务在跑 | 返回 `{status:"busy"}`，前端提示"请等待当前回放完成" |
| 区间内无信号 | `run_tdx_backtest` 返回空结果，store 写入 0 条交易，前端正常展示 |
| 回放线程崩溃 | `except Exception` 捕获，推送 `replay_error` + 错误信息，重置 `_replay_state` |
| DuckDB 写入失败 | 尝试回退 `JsonSimStore`，均失败则推送错误 |

---

## 4. 文件改动清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `app/api/sim_trader.py` | 修改 | 新增 `replay` + `replay-status` 端点，约 80 行 |
| `static/index.html` | 修改 | 新增回放按钮 + 进度条 HTML，约 20 行 |
| `static/js/main.js` | 修改 | 新增 `startReplay` + WS 消息处理，约 40 行 |
| `app/sim_trader/store.py` | 可选修改 | 如果 `clear_all` 需要更细粒度（先不动） |

**不改动的文件（零风险）**：
- `app/backtest/tdx_runner.py`
- `app/sim_trader/engine.py`
- `app/sim_trader/config.py`
- 前端所有现有 UI 结构

---

## 5. 测试验证

### 手动验证步骤

1. **正常回放**: 打开"交易控制"TAB → 点击"⏪ 历史回放" → 观察进度条变化 → 完成后净值图/交易表自动刷新
2. **重复点击**: 回放中再次点击 → 应提示"请等待当前回放完成"
3. **数据一致性**: 回放完成后，交易记录数与 `scripts/run_quantqq_backtest.py` CLI 跑的一致
4. **刷新恢复**: 回放完成后刷新页面 → 净值图/交易记录仍然存在（已持久化到 DuckDB）
5. **不影响实时**: 回放运行期间，其他 TAB（选股/行情等）正常工作

### 自动化测试（后续补）

- `tests/test_api_sim_trader.py::test_replay_start` — 验证 API 返回 status=started
- `tests/test_api_sim_trader.py::test_replay_busy` — 验证重复调用的 busy 状态

---

## 6. 风险与边界

| 风险 | 缓解 |
|------|------|
| TDX 桥接依赖本地通达信客户端 | bridge 不可用时报错提示"请启动通达信" |
| 5m 全市场回测耗时长（3-10 分钟） | 进度条实时更新，用户可见 |
| 回放结果覆盖当前模拟盘实时持仓 | 这是预期行为（一键跑完看结果），未来可加"保存/恢复快照" |
| WebSocket 断连 | 进度丢失但后台继续跑，重连后调 status API 查看 |
