# 模拟盘 QUANTQQ 数据灌入 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 编写独立脚本 `scripts/populate_sim_quantqq.py`，跑一次将 QUANTQQ + 5m + 2026.1.1~今天 的回测结果写入 `JsonSimStore`，前端"交易控制"TAB 自动展示。

**Architecture:** 独立脚本调用 `run_tdx_backtest()` → 字段名映射转换 → JSON 写入 `output/sim_trader/state.json`。不改引擎、API、前端。

**Tech Stack:** Python 3（调用现有 `tdx_runner` / `JsonSimStore`）

**关键发现：** `app/api/sim_trader.py` 的 `get_engine()` 使用 `JsonSimStore`（JSON 文件 `output/sim_trader/state.json`），不是 DuckDB 的 `SimTraderStore`。脚本必须写 JSON。

---

### Task 1: 格式转换 + JSON 写入函数（核心逻辑）

**Files:**
- Create: `scripts/populate_sim_quantqq.py`

- [ ] **Step 1: 创建脚本骨架**

```python
"""
一次性脚本：灌入 QUANTQQ 5m 回测结果到模拟盘 JSON Store
用法: python scripts/populate_sim_quantqq.py
依赖: 通达信客户端已启动
"""
import sys, os, json
from pathlib import Path
from datetime import date, datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    from app.sim_trader.config import (
        INITIAL_CAPITAL, POSITION_SIZE, MIN_BUY_AMT,
        HARD_STOP, TAKE_PROFIT_TIERS, TRAIL_ACTIVATE, TRAIL_DD,
        TIME_EXIT_DAYS, TIME_EXIT_PROFIT, TIME_FORCE_DAYS,
        LOSS_STREAK_HALVE, LOSS_STREAK_PAUSE, PAUSE_DAYS,
        SAME_STOCK_COOLDOWN, USE_ATR_TRAIL, ATR_TRAIL_MULTIPLIER,
        FIRST_DAY_EXIT_MIN_PROFIT, FIRST_DAY_EXIT_DAYS,
    )

    START_DATE = date(2026, 1, 1)
    END_DATE   = date.today()
    STRATEGY   = "QUANTQQ"
    PERIOD     = "5m"

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
        "take_profit_tiers": [dict(t) for t in TAKE_PROFIT_TIERS],
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
    print(f"  QUANTQQ 模拟盘数据灌入")
    print(f"  区间: {START_DATE} ~ {END_DATE}  |  策略: {STRATEGY} ({PERIOD})")
    print("=" * 64)

    # ── 运行 TDX 回测 ──────────────────────────
    print("\n[1/3] 运行 TDX 回测 ...")
    from app.backtest.tdx_runner import run_tdx_backtest

    def _progress(stage, total, msg):
        print(f"  [{stage}/{total}] {msg}", flush=True)

    import time
    t0 = time.time()
    result = run_tdx_backtest(params, progress_cb=_progress)
    t1 = time.time()
    print(f"\n  TDX 回测耗时: {t1-t0:.1f}秒")

    if result is None or result.get("status") != "ok":
        print(f"\n[ERROR] 回测失败: {result.get('message', result)}")
        sys.exit(1)

    # ── 写入 JSON Store ───────────────────────
    print("\n[2/3] 转换格式并写入 state.json ...")
    write_to_json_store(result, STRATEGY, params['initial_capital'])

    # ── 打印摘要 ───────────────────────────────
    print("\n[3/3] 完成:")
    s = result["summary"]
    print(f"  交易笔数: {len(result['trades'])}/{s.get('signals','?')}")
    print(f"  收益率: {s['total_return']:+.2f}%  胜率: {s['win_rate']:.1f}%")
    print(f"  最大回撤: {s['max_drawdown']:.2f}%  夏普: {s['sharpe']}")
    print(f"  数据源: {s.get('data_source', '?')}")
    json_path = ROOT / "output" / "sim_trader" / "state.json"
    print(f"\n  → 已写入: {json_path}")
    print(f"  → 刷新前端"交易控制"TAB 即可查看")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 添加格式转换和 JSON 写入函数**

在 `main()` 后面追加：

```python
def write_to_json_store(result: dict, strategy_name: str, initial_capital: float):
    """将 run_tdx_backtest 结果写入 JsonSimStore (state.json)"""

    from app.sim_trader.store import JsonSimStore

    store = JsonSimStore()
    # 清空旧数据
    store._data = {}

    trades_src = result["trades"]
    equity_src = result["equity"]
    summary = result["summary"]

    # ── 1) 交易记录 ──────────────────────────
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

    # ── 2) 净值曲线 ──────────────────────────
    # equity[]: {date, equity, norm, cash, pos, dd}
    equity_out = []
    for e in equity_src:
        equity_out.append({
            "date": str(e["date"]),
            "equity": float(e["equity"]),
            "cash": float(e["cash"]),
            "pos": int(e["pos"]),
        })
    store._data["equity_curve"] = equity_out

    # ── 3) 终态 ──────────────────────────────
    # cash: 取最后一条 equity 的 cash
    cash_end = float(equity_src[-1]["cash"]) if equity_src else float(initial_capital)

    store._data["state"] = {
        "cash": cash_end,
        "consecutive_losses": 0,
        "pause_until": None,
        "trade_count": len(trades_out),
    }

    # ── 4) 持仓 ──────────────────────────────
    # 历史回放完成后无活跃持仓，清空
    store._data["positions"] = {}

    # ── 5) prev_day_snap ──────────────────────
    # 回放区间最后一天的快照（让引擎冷启动时正确处理）
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
```

- [ ] **Step 3: 跑一次验证格式**

Run: `python scripts/populate_sim_quantqq.py`
Check: 运行成功，打印校验通过
Check: `output/sim_trader/state.json` 文件存在且格式正确

- [ ] **Step 4: 提交**

```bash
git add scripts/populate_sim_quantqq.py
git commit -m "feat(scripts): add QUANTQQ sim-trader data populate script"
```

---

### Task 2: 自检与迭代

- [ ] **Step 1: 前端验证**

重启服务器 `python run.py`，打开"交易控制"TAB，检查：
1. 净值图显示 2026.1~6 曲线 ✅
2. 交易记录表列出 QUANTQQ 买卖 ✅
3. 刷新页面数据仍在 ✅

- [ ] **Step 2: 与 CLI 回测对比**

```
python scripts/run_quantqq_backtest.py  # CLI 输出
python scripts/populate_sim_quantqq.py  # 对比 summary
```

Check: 收益率、胜率、交易笔数一致

- [ ] **Step 3: 代码质量检查**

```bash
python -m py_compile scripts/populate_sim_quantqq.py
```

Check: 无语法错误
Check: 无 `console.log`（Python 不用检查这个）
Check: 无硬编码密钥
Check: 所有参数从 `config.py` 读

- [ ] **Step 4: 提交最终版本**

```bash
git add scripts/populate_sim_quantqq.py
git commit -m "chore(scripts): finalize populate_sim_quantqq with verification"
```
