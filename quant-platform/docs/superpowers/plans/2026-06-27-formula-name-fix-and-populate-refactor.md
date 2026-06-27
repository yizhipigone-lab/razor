# 通用回放脚本 + formula_name 端到端修复 + 详细日志 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
1. 把 `scripts/populate_sim_quantqq.py` 重命名为通用脚本 `scripts/populate_sim_trader.py`，支持命令行传任意公式
2. 修复 formula_name 端到端传递（前端 → API → bridge → TDX → 写库）
3. 给回放脚本加详细日志（数据获取/信号/仿真进度/退出原因/资金轨迹）

**Architecture:**
- 通用化脚本：CLI 参数化（`--strategy` `--period` `--start-date` `--end-date`），去掉硬编码 QUANTQQ 字样
- formula_name 链路：`bridge.py` 加参数 → `api/tqsdk.py` 读 body+settings → 前端加 UI 设置面板
- 详细日志：在脚本里包一层 `print_xxx()` 函数，从 result 数据计算更丰富的统计

**Tech Stack:** Python 3 + FastAPI + 原生 JS

---

### Task 1: 通用化 populate 脚本（重命名 + 参数化）

**Files:**
- Rename: `scripts/populate_sim_quantqq.py` → `scripts/populate_sim_trader.py`
- Modify: `scripts/populate_sim_trader.py` 全文

- [ ] **Step 1: 重命名文件 + 改文件内容（CLI 参数化）**

```bash
git mv scripts/populate_sim_quantqq.py scripts/populate_sim_trader.py
```

新文件内容（替换原 `main()` 函数 + 添加 argparse）：

```python
"""
一次性脚本：灌入 TDX 公式回测结果到模拟盘 JSON Store

用法:
  python scripts/populate_sim_trader.py --strategy QUANTQQ
  python scripts/populate_sim_trader.py --strategy gs_1_GUPIAO_011 --period 5m
  python scripts/populate_sim_trader.py --help

依赖: 通达信客户端已启动
"""
import argparse
import sys, json
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def parse_args():
    p = argparse.ArgumentParser(description="灌入 TDX 公式回测结果到模拟盘")
    p.add_argument("--strategy", default="QUANTQQ",
                   help="TDX 公式名（默认: QUANTQQ）")
    p.add_argument("--period", default="5m", choices=["5m", "1m", "daily"],
                   help="回测精度（默认: 5m）")
    p.add_argument("--start-date", default="2026-01-01",
                   help="开始日期 YYYY-MM-DD（默认: 2026-01-01）")
    p.add_argument("--end-date", default=None,
                   help="结束日期 YYYY-MM-DD（默认: 今天）")
    p.add_argument("--output", default=None,
                   help="输出 JSON 路径（默认: output/sim_trader/{strategy}_state.json）")
    return p.parse_args()


def main():
    args = parse_args()
    from app.sim_trader.config import (
        INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
        HARD_STOP, TAKE_PROFIT_TIERS, TRAIL_ACTIVATE, TRAIL_DD,
        TIME_EXIT_DAYS, TIME_EXIT_PROFIT, TIME_FORCE_DAYS,
        LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE, PAUSE_DAYS,
        SAME_STOCK_COOLDOWN, USE_ATR_TRAIL, ATR_TRAIL_MULTIPLIER,
        FIRST_DAY_EXIT_MIN_PROFIT, FIRST_DAY_EXIT_DAYS,
    )

    STRATEGY = args.strategy
    PERIOD = args.period
    START_DATE = date.fromisoformat(args.start_date)
    END_DATE = date.fromisoformat(args.end_date) if args.end_date else date.today()

    # 默认输出路径：output/sim_trader/{strategy}_state.json
    OUTPUT_PATH = Path(args.output) if args.output else \
                  ROOT / "output" / "sim_trader" / f"{STRATEGY}_state.json"

    params = {
        "strategy_name": STRATEGY,
        "strategy_type": "tdx",
        "intraday_freq": PERIOD,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "initial_capital": INITIAL_CAPITAL,
        "position_size": POSITION_SIZE,
        "min_buy_amt": MIN_BUY_AMT,
        "hard_stop": HARD_STOP,
        "take_profit_tiers": [tier.copy() for tier in TAKE_PROFIT_TIERS],
        "trail_activate": TRAIL_ACTIVATE,
        "trail_dd": TRAIL_DD,
        "time_exit_days": TIME_EXIT_DAYS,
        "time_exit_profit": TIME_EXIT_PROFIT,
        "time_force_days": TIME_FORCE_DAYS,
        "loss_streak_halve": LOSS_STREAK_HALVE,
        "loss_streak_pause": LOSS_STREAK_PAUSE,
        "pause_days": PAUSE_DAYS,
        "same_stock_cooldown": SAME_STOCK_COOLDOWN,
        "use_atr_trail": USE_ATR_TRAIL,
        "atr_trail_multiplier": ATR_TRAIL_MULTIPLIER,
        "first_day_exit_min_profit": FIRST_DAY_EXIT_MIN_PROFIT,
        "first_day_exit_days": FIRST_DAY_EXIT_DAYS,
        "signal_params": {},
    }

    print("=" * 64)
    print(f"  TDX 模拟盘数据灌入")
    print(f"  策略:   {STRATEGY}")
    print(f"  精度:   {PERIOD}")
    print(f"  区间:   {START_DATE} ~ {END_DATE}")
    print(f"  输出:   {OUTPUT_PATH}")
    print("=" * 64)

    # ── 运行 TDX 回测 ──────────────────────────
    print("\n[1/4] 运行 TDX 回测 ...")
    from app.backtest.tdx_runner import run_tdx_backtest

    def _progress(stage, total, msg):
        print(f"  [{stage}/{total}] {msg}", flush=True)

    import time
    t0 = time.time()
    result = run_tdx_backtest(params, progress_cb=_progress)
    t1 = time.time()
    print(f"\n  TDX 回测耗时: {t1-t0:.1f}秒")

    if result is None or result.get("status") != "ok":
        msg = result.get("message", str(result)) if result is not None else "No result returned"
        print(f"\n[ERROR] 回测失败: {msg}")
        sys.exit(1)

    # ── 写入 JSON Store ───────────────────────
    print("\n[2/4] 转换格式并写入 state.json ...")
    write_to_json_store(result, params['initial_capital'], OUTPUT_PATH)

    # ── 打印详细摘要 ─────────────────────────
    print("\n[3/4] 详细统计:")
    print_detailed_stats(result)

    # ── 打印基本摘要 ─────────────────────────
    print("\n[4/4] 完成:")
    s = result["summary"]
    print(f"  策略:       {STRATEGY}")
    print(f"  精度:       {PERIOD}")
    print(f"  区间:       {s.get('start_date')} ~ {s.get('end_date')}")
    print(f"  交易笔数:   {len(result['trades'])}/{s.get('signals','?')}")
    print(f"  收益率:     {s['total_return']:+.2f}%  胜率: {s['win_rate']:.1f}%")
    print(f"  最大回撤:   {s['max_drawdown']:.2f}%  夏普: {s['sharpe']}")
    print(f"  数据源:     {s.get('data_source', '?')}")
    print(f"\n  → 已写入: {OUTPUT_PATH}")
    print(f"  → 刷新前端交易控制 TAB 即可查看")


def write_to_json_store(result: dict, initial_capital: float, output_path: Path):
    """将 run_tdx_backtest 结果写入 JsonSimStore 格式"""
    from app.sim_trader.store import JsonSimStore

    store = JsonSimStore(path=str(output_path))
    store._data = {}

    trades_src = result["trades"]
    equity_src = result["equity"]

    # 1) 交易记录
    trades_out = []
    for t in trades_src:
        trades_out.append({
            "code": t["code"],
            "entry_date": str(t["entry_date"]),
            "exit_date": str(t["exit_date"]),
            "entry_price": float(t["entry_px"]),
            "exit_price": float(t["exit_px"]),
            "shares": int(t["shares"]),
            "ret_pct": float(t["ret_pct"]),
            "profit": float(t["profit"]),
            "reason": t["reason"],
            "hold_days": int(t["hold_days"]),
            "entry_time": str(t.get("entry_time", "09:30")),
            "exit_time": str(t.get("exit_time", "15:00")),
        })
    store._data["trades"] = trades_out

    # 2) 净值曲线
    equity_out = []
    for e in equity_src:
        equity_out.append({
            "date": str(e["date"]),
            "equity": float(e["equity"]),
            "cash": float(e["cash"]),
            "pos": int(e["pos"]),
        })
    store._data["equity_curve"] = equity_out

    # 3) 终态
    cash_end = float(equity_src[-1]["cash"]) if equity_src else float(initial_capital)
    store._data["state"] = {
        "cash": cash_end,
        "consecutive_losses": 0,
        "pause_until": None,
        "trade_count": len(trades_out),
    }

    # 4) 持仓 + 5) snap
    store._data["positions"] = {}
    store._data["prev_day_snap"] = {}

    store._save()

    # 校验
    stored = json.loads(json.dumps(store._data, default=str))
    loaded_trades = stored.get("trades", [])
    loaded_equity = stored.get("equity_curve", [])
    loaded_state = stored.get("state", {})
    assert len(loaded_trades) == len(trades_src), \
        f"交易数不一致: saved={len(loaded_trades)} src={len(trades_src)}"
    assert len(loaded_equity) == len(equity_src), \
        f"净值点数不一致: saved={len(loaded_equity)} src={len(equity_src)}"
    assert abs(loaded_state.get("cash", 0) - cash_end) < 2.0, \
        f"终值现金不一致: saved={loaded_state.get('cash')} expected={cash_end}"
    print(f"  校验通过: {len(loaded_trades)}笔交易, {len(loaded_equity)}T净值")


def print_detailed_stats(result: dict):
    """从 result 计算并打印丰富的统计信息"""
    from collections import Counter

    trades = result["trades"]
    equity = result["equity"]
    summary = result["summary"]

    if not trades:
        print("  无交易记录")
        return

    # ── 信号 → 交易转化率 ──
    total_signals = summary.get("signals", summary.get("buy_signals", 0))
    if total_signals:
        rate = len(trades) / total_signals * 100
        print(f"  信号转化率:     {len(trades)}/{total_signals} = {rate:.2f}%")

    # ── 退出原因分布 ──
    reasons = Counter(t["reason"] for t in trades)
    print(f"  退出原因分布:")
    for r, c in reasons.most_common():
        pct = c / len(trades) * 100
        print(f"    {r:<12} {c:>5} 笔  {pct:>5.1f}%")

    # ── 持仓时长分布 ──
    hold_days = [int(t["hold_days"]) for t in trades]
    hold_dist = Counter(hold_days)
    print(f"  持仓天数分布:")
    for d in sorted(hold_dist.keys()):
        c = hold_dist[d]
        bar = "█" * min(40, c * 40 // max(hold_dist.values()))
        print(f"    {d:>2}天: {c:>4} 笔 {bar}")

    # ── 月度收益 ──
    monthly = {}
    for t in trades:
        key = str(t["exit_date"])[:7]
        monthly[key] = monthly.get(key, 0) + float(t["profit"])
    print(f"  月度盈亏:")
    for m in sorted(monthly.keys()):
        p = monthly[m]
        sign = "+" if p >= 0 else ""
        print(f"    {m}: {sign}{p:>10,.0f}")

    # ── 单笔最大盈亏 ──
    sorted_by_profit = sorted(trades, key=lambda t: float(t["profit"]), reverse=True)
    print(f"  Top 3 单笔盈利:")
    for t in sorted_by_profit[:3]:
        print(f"    {t['code']:<6} {t['entry_date']} -> {t['exit_date']}  "
              f"+{float(t['profit']):>8,.0f} (+{float(t['ret_pct']):.1f}%)  {t['reason']}")
    print(f"  Worst 3 单笔:")
    for t in sorted_by_profit[-3:]:
        print(f"    {t['code']:<6} {t['entry_date']} -> {t['exit_date']}  "
              f"{float(t['profit']):>9,.0f} ({float(t['ret_pct']):.1f}%)  {t['reason']}")

    # ── 资金轨迹（最大/最小净值日）──
    if equity:
        max_eq = max(equity, key=lambda e: float(e["equity"]))
        min_eq = min(equity, key=lambda e: float(e["equity"]))
        print(f"  资金轨迹:")
        print(f"    最高净值日: {max_eq['date']}  {float(max_eq['equity']):>14,.0f}")
        print(f"    最低净值日: {min_eq['date']}  {float(min_eq['equity']):>14,.0f}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 验证脚本可执行**

Run: `python scripts/populate_sim_trader.py --help`
Expected: 显示 argparse 帮助信息

Run: `python scripts/populate_sim_trader.py --strategy QUANTQQ`
Expected: 跑回测（通达信可用时）或 fallback 到现有数据路径

- [ ] **Step 3: 提交**

```bash
git add scripts/populate_sim_trader.py
git rm scripts/populate_sim_quantqq.py
git commit -m "refactor(scripts): rename populate script to support any strategy via --strategy flag"
```

---

### Task 2: bridge.py 支持 formula_name 参数

**Files:**
- Modify: `app/tqsdk/bridge.py:18-110`

- [ ] **Step 1: 改 `_get_formula_name` 接受可选参数（向后兼容）**

```python
def _get_formula_name(override: str = None) -> str:
    """获取公式名。优先级: override > settings > QUANTQQ"""
    if override and override.strip():
        return override.strip()
    try:
        from core.settings import settings
        name = settings.get("tqsdk", "formula_name", default="QUANTQQ")
        return name if name else "QUANTQQ"
    except Exception:
        return "QUANTQQ"
```

- [ ] **Step 2: 给三个 execute 方法加 `formula_name` 参数**

在 `execute_screen` 加参数：
```python
def execute_screen(self, end_time: str, stock_list_override: list = None,
                   lookback_days: int = 30, formula_name: str = None):
    task = {
        "task_type": "screen",
        "formula_name": _get_formula_name(formula_name),
        "formula_arg": "",
        "output_var_name": OUTPUT_VAR,
        "match_value": MATCH_VALUE,
        "end_time": end_time,
        "stock_list_override": stock_list_override,
        "lookback_days": lookback_days,
        "return_date": False,
    }
    return self._run_worker(task)
```

在 `execute_screen_range` 加参数：
```python
def execute_screen_range(self, end_time: str, kline_count: int,
                         start_time: str = "", formula_name: str = None,
                         stock_list_override: list = None,
                         return_count: int = None):
    task = {
        "task_type": "range",
        "formula_name": _get_formula_name(formula_name),
        "formula_arg": "",
        "output_var_name": OUTPUT_VAR,
        "match_value": MATCH_VALUE,
        "end_time": end_time,
        "start_time": start_time,
        "stock_list_override": stock_list_override,
        "kline_count": kline_count,
        "return_count": return_count,
    }
    return self._run_worker(task, timeout_multiplier=max(2, kline_count // 50))
```

在 `execute_screen_range_intraday` 加参数：
```python
def execute_screen_range_intraday(self, end_time: str, kline_count: int,
                                  start_date: str = None,
                                  return_count: int = None,
                                  stock_list_override: list = None,
                                  start_time: str = "",
                                  signal_start: str = "",
                                  formula_name: str = None,
                                  period: str = "5m"):
    # ... 同结构，formula_name: _get_formula_name(formula_name)
```

- [ ] **Step 3: 验证不破坏现有调用（向后兼容）**

现有调用 `bridge.execute_screen(end_time=...)` 不传 formula_name → 默认 None → 走 settings → fallback QUANTQQ → 行为不变。

```bash
python -c "from app.tqsdk.bridge import TdxBridge; b = TdxBridge(); print('OK')"
```

- [ ] **Step 4: 提交**

```bash
git add app/tqsdk/bridge.py
git commit -m "feat(tqsdk): bridge accepts formula_name override parameter"
```

---

### Task 3: api/tqsdk.py 接收 formula_name 并传给 bridge

**Files:**
- Modify: `app/api/tqsdk.py:40-180, 234-340`

- [ ] **Step 1: `start_screen` 接收 formula_name + 写库用真实公式名**

```python
@router.post("/api/tqsdk/screen")
async def start_screen(body: dict):
    end_time = body.get("end_date", "")
    start_time = body.get("start_date", "")
    stock_list_override = body.get("stock_list_override")
    formula_name = (body.get("formula_name") or "").strip()  # 新增

    # ... 现有 _fmt_date 不变

    with _stop_lock:
        if "tqsdk" in _stop_events:
            return {"status": "error", "message": "已有选股任务在运行"}
        _stop_events["tqsdk"] = threading.Event()

    # 提前决定最终用的公式名（用于持久化）
    def _resolve_formula_name():
        if formula_name:
            return formula_name
        from core.settings import settings
        return settings.get("tqsdk", "formula_name", default="QUANTQQ") or "QUANTQQ"

    def _run():
        nonlocal formula_name  # 用于持久化
        try:
            from app.tqsdk.bridge import TdxBridge
            bridge = TdxBridge()

            # ... 现有 scan_dates 逻辑不变

            for i, d in enumerate(scan_dates):
                # ... 现有 stop 逻辑
                result = bridge.execute_screen(
                    end_time=d_str,
                    stock_list_override=stock_list_override,
                    lookback_days=500,
                    formula_name=formula_name,  # 新增
                )
                # ...

            # 持久化（用真实公式名，不硬编码）
            history_id = db.save_tqsdk_screen_history(
                formula_name=formula_name or _resolve_formula_name(),  # 改
                formula_arg="",
                start_date=db_start,
                end_date=db_end,
                stock_count=len(matched),
                stock_codes=[c.split(".")[0] for c in matched],
                stock_details=stock_details,
            )
            # ... 现有 WS 广播
```

- [ ] **Step 2: `run_tqsdk_bt` 同样支持 formula_name**

在 `run_tqsdk_bt` 里接受 `formula_name` 参数（body.get('formula_name')），传给 `run_backtest` params 或作为 settings 临时覆盖：

```python
@router.post("/api/tqsdk/backtest")
async def run_tqsdk_bt(body: dict):
    # ... 现有逻辑
    formula_name = (body.get("formula_name") or "").strip()

    # ... 现有 history_id / stock_list 加载

    if formula_name:
        # 临时覆盖 settings，让 bridge 知道
        from core.settings import settings
        if "tqsdk" not in settings._data:
            settings._data["tqsdk"] = {}
        settings._data["tqsdk"]["formula_name"] = formula_name
        # params 也带上
        params["strategy_name"] = formula_name
```

- [ ] **Step 3: 提交**

```bash
git add app/api/tqsdk.py
git commit -m "feat(api): tqsdk endpoints accept formula_name from body, persist real name"
```

---

### Task 4: 前端 formula_name 设置面板

**Files:**
- Modify: `static/index.html:1449-1465`
- Modify: `static/js/main.js:4788-4815`

- [ ] **Step 1: HTML 加公式名输入框**

在 `<div id="tab-tqsdk">` 第一张卡片标题下加：

```html
<div class="card" style="margin-bottom: 12px;">
  <h3>📡 通达信公式选股</h3>
  <div class="form-row" style="flex-wrap: wrap; gap: 8px; align-items: center;">
    <label>公式名</label>
    <input type="text" id="tqsdk-formula" style="width: 160px;"
           placeholder="QUANTQQ" />
    <span id="tqsdk-formula-msg" style="font-size:11px;color:var(--text2)"></span>
    <button class="btn btn-ghost btn-sm" onclick="saveTqsdkFormula()" style="font-size:11px">保存</button>
  </div>
  <div class="form-row" style="flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 8px;">
    <label>开始日期</label>
    <input type="date" id="tqsdk-start" style="width: 140px;" />
    <label>结束日期</label>
    <input type="date" id="tqsdk-end" style="width: 140px;" />
    <button class="btn btn-primary" id="tqsdk-run-btn" onclick="runTqsdkScreen()">开始选股</button>
    <button class="btn btn-danger" id="tqsdk-stop-btn" onclick="stopTqsdkScreen()" style="display:none;">停止</button>
  </div>
  <div class="progress-wrap" id="tqsdk-progress" style="display:none; margin-top: 8px;">
    <div class="progress-bar"><div class="progress-fill" id="tqsdk-progress-fill" style="width:0%;"></div></div>
    <span id="tqsdk-progress-msg" style="font-size: 12px; color: var(--text2);"></span>
  </div>
</div>
```

- [ ] **Step 2: JS 加 load/save 公式名 + 选股时传 body**

在 `main.js` 中：

```javascript
async function loadTqsdkFormula() {
  try {
    const r = await fetch('/api/settings/tqsdk-formula').then(r => r.json());
    const el = document.getElementById('tqsdk-formula');
    if (el && r.formula_name) el.value = r.formula_name;
  } catch(e) {}
}

async function saveTqsdkFormula() {
  const el = document.getElementById('tqsdk-formula');
  const msg = document.getElementById('tqsdk-formula-msg');
  if (!el || !el.value.trim()) {
    if (msg) { msg.textContent = '公式名不能为空'; msg.style.color = 'var(--red)'; }
    return;
  }
  try {
    const r = await fetch('/api/settings/tqsdk-formula', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ formula_name: el.value.trim() }),
    }).then(r => r.json());
    if (r.status === 'ok') {
      if (msg) { msg.textContent = '已保存'; msg.style.color = 'var(--up)'; }
      setTimeout(() => { if (msg) msg.textContent = ''; }, 2000);
    }
  } catch(e) {
    if (msg) { msg.textContent = '保存失败: ' + e.message; msg.style.color = 'var(--red)'; }
  }
}

async function runTqsdkScreen() {
  const endEl = document.getElementById('tqsdk-end');
  const startEl = document.getElementById('tqsdk-start');
  const formulaEl = document.getElementById('tqsdk-formula');
  if (!endEl || !endEl.value) { alert('请选择结束日期'); return; }

  toggleTqsdkButtons(true);
  document.getElementById('tqsdk-results-body').innerHTML =
    '<tr><td colspan="4" style="color:var(--accent);text-align:center;">正在选股中...</td></tr>';

  try {
    const resp = await fetch('/api/tqsdk/screen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_date: (startEl && startEl.value) ? startEl.value.replace(/-/g, '') : '',
        end_date: endEl.value.replace(/-/g, ''),
        formula_name: (formulaEl && formulaEl.value) ? formulaEl.value.trim() : '',
      }),
    });
    const data = await resp.json();
    if (data.status !== 'started') {
      addLog('error', '选股启动失败: ' + (data.message || ''));
      toggleTqsdkButtons(false);
    }
  } catch (e) {
    addLog('error', '请求失败: ' + e.message);
    toggleTqsdkButtons(false);
  }
}
```

- [ ] **Step 3: 在 switchTab 加载公式**

找到 `if (name === 'sim-trader')` 类似的位置，加：

```javascript
} else if (name === 'tqsdk') {
  loadTqsdkFormula();
  // 现有初始化...
}
```

- [ ] **Step 4: 提交**

```bash
git add static/index.html static/js/main.js
git commit -m "feat(ui): add formula_name input and save in TDX tab"
```

---

### Task 5: settings 端点 (GET/PUT formula_name)

**Files:**
- New: `app/api/settings_extra.py` (小文件，避免侵入现有 main.py)

- [ ] **Step 1: 新建 settings_extra.py**

```python
"""额外设置端点（小众配置）"""
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/settings/tqsdk-formula")
async def get_tqsdk_formula():
    from core.settings import settings
    formula_name = settings.get("tqsdk", "formula_name", default="QUANTQQ") or "QUANTQQ"
    return {"status": "ok", "formula_name": formula_name}


@router.put("/api/settings/tqsdk-formula")
async def set_tqsdk_formula(body: dict):
    """更新 TDX 公式名。注：热生效需重启 bridge 进程。"""
    formula_name = (body.get("formula_name") or "").strip()
    if not formula_name:
        return {"status": "error", "message": "公式名不能为空"}
    from core.settings import settings
    if "tqsdk" not in settings._data:
        settings._data["tqsdk"] = {}
    settings._data["tqsdk"]["formula_name"] = formula_name
    settings.save()
    return {"status": "ok", "formula_name": formula_name}
```

- [ ] **Step 2: 注册 router**

找到 `main.py` 或注册路由的地方，加：

```python
from app.api.settings_extra import router as settings_extra_router
app.include_router(settings_extra_router)
```

- [ ] **Step 3: 提交**

```bash
git add app/api/settings_extra.py main.py
git commit -m "feat(api): add settings endpoints for tqsdk formula_name"
```

---

### Task 6: 自检 + 集成验证

- [ ] **Step 1: 跑现有 pytest**

```bash
pytest tests/ -v
```

Expected: 21 个测试全部 PASS（不应受影响）

- [ ] **Step 2: 手动验证流程**

1. **前端保存公式**: 打开 TDX tab → 填 `gs_1_GUPIAO_011` → 点保存 → 提示"已保存"
2. **查看 settings**: 检查 `config/app_setting.json` → `tqsdk.formula_name == "gs_1_GUPIAO_011"`
3. **点开始选股**: body 应包含 formula_name
4. **API 接收**: print/log `formula_name=gs_1_GUPIAO_011` 传给 bridge
5. **history 记录**: tqsdk_screen_history 表中 formula_name 应是 `gs_1_GUPIAO_011`，不是 `QUANTQQ`
6. **CLI 跑新公式**:
   ```bash
   python scripts/populate_sim_trader.py --strategy gs_1_GUPIAO_011
   ```
   输出路径应为 `output/sim_trader/gs_1_GUPIAO_011_state.json`

- [ ] **Step 3: 提交最终确认**

```bash
git status
git push origin master
```

---

## 改动清单总结

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `scripts/populate_sim_quantqq.py` | 删除 | 重命名为通用脚本 |
| `scripts/populate_sim_trader.py` | **新建**（原 populate_sim_quantqq.py 内容） | CLI 参数化（`--strategy` `--period` `--start-date` `--end-date` `--output`）+ 详细日志 |
| `app/tqsdk/bridge.py` | 修改 | 三个 execute 方法加 `formula_name` 参数（向后兼容） |
| `app/api/tqsdk.py` | 修改 | `start_screen` / `run_tqsdk_bt` 接收 formula_name + 写库用真实名 |
| `app/api/settings_extra.py` | **新建** | `GET/PUT /api/settings/tqsdk-formula` |
| `main.py` 或入口文件 | 修改 | 注册新 router |
| `static/index.html` | 修改 | TDX tab 加公式名输入框 |
| `static/js/main.js` | 修改 | `loadTqsdkFormula` / `saveTqsdkFormula` + 选股 body 传 formula_name |

**不改动的文件**：
- `app/backtest/tdx_runner.py` — `params["strategy_name"]` 链路已正确
- `app/sim_trader/store.py`
- `app/api/sim_trader.py`
- 所有交易控制 TAB 相关代码

---

## 测试验证

| 步骤 | 预期 |
|------|------|
| `pytest tests/` | 21/21 PASS |
| `python scripts/populate_sim_trader.py --help` | 显示 argparse 帮助 |
| 前端填 `gs_1_GUPIAO_011` 保存 | 写入 settings.json + 提示已保存 |
| 跑选股 | history.formula_name == "gs_1_GUPIAO_011" |
| CLI 跑 `--strategy gs_1_GUPIAO_011` | 输出 `output/sim_trader/gs_1_GUPIAO_011_state.json` |
| 终端详细日志 | 信号转化率、退出原因分布、月度盈亏、Top3 单笔、最大/最小净值日等 |

---

## 风险与边界

| 风险 | 缓解 |
|------|------|
| settings 写入失败影响其他模块 | 用 `settings._data.setdefault('tqsdk', {})` 安全写入 |
| bridge 子进程已缓存旧公式名 | 提示用户重启 TDX bridge（文档说明） |
| formula_name 含特殊字符 | `_get_formula_name` 用 `.strip()` + 空字符串 fallback |
| 前端刷新后公式名丢失 | 在 `switchTab('tqsdk')` 时重新拉一次 |