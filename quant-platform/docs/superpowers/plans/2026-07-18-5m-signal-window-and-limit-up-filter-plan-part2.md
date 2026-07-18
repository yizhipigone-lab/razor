# 5m 信号窗口与涨停过滤完整性修复 — 实现计划（Part 2：实盘闸与基线 diff）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成实盘侧涨停拒买闸（RiskGate 5c）与基线 diff 验证。

**Architecture:** 在 `RiskGate` 5b 之后、闸门 7 之前插入 5c 涨停拒买闸；优先复用 `check()` 已传入的 `quote`，缺失时走 `quote_source` 降级；新增 `LiveTraderConfig.limit_up_gate_enabled` 开关；最后跑 QUANTQQ 基线 diff。

**Tech Stack:** Python, pytest, QMT HTTP API

**设计文档：** [docs/superpowers/specs/2026-07-18-5m-signal-window-and-limit-up-filter-design.md](docs/superpowers/specs/2026-07-18-5m-signal-window-and-limit-up-filter-design.md)

**前置计划：** [docs/superpowers/plans/2026-07-18-5m-signal-window-and-limit-up-filter-plan-part1.md](docs/superpowers/plans/2026-07-18-5m-signal-window-and-limit-up-filter-plan-part1.md)

---

## 文件结构（Part 2）

| 文件 | 动作 | 职责 |
|---|---|---|
| `app/live_trader/config.py` | 修改 | 加 `limit_up_gate_enabled` 开关 |
| `app/live_trader/risk_gate.py:125-165` | 修改 | 5b 之后、闸门 7 之前插入 5c 涨停拒买闸 |
| `tests/test_live_trader_smoke.py` | 修改 | 新增 5c 闸通过/关闭/不计入连续拒绝测试 |
| `scripts/run_limit_up_baseline.py` | 新建 | 跑基线并输出 JSON |
| `scripts/diff_limit_up_baseline.py` | 新建 | 对比改前/改后基线并生成 markdown 报告 |
| `docs/reports/2026-07-18-limit-up-baseline-diff.md` | 新建 | 基线 diff 报告 |

---

## Task 6: 新增实盘 RiskGate 涨停拒买闸

**Files:**
- Modify: `app/live_trader/config.py`
- Modify: `app/live_trader/risk_gate.py`
- Test: `tests/test_live_trader_smoke.py`

### Step 1: 修改 `LiveTraderConfig`

在 `app/live_trader/config.py` 的 RiskGate 段增加：

```python
    limit_up_gate_enabled: bool = True       # 闸门5c 涨停封板拒买开关
```

在 `load_config()` 的 `LiveTraderConfig(...)` 构造参数中增加：

```python
        limit_up_gate_enabled=cfg_dict.get("limit_up_gate_enabled", True),
```

### Step 2: 修改 `RiskGate`

在 `app/live_trader/risk_gate.py` 顶部增加 import：

```python
from app.utils.limit_up import _is_valid_price, is_limit_up
```

在 5b 闸门之后、闸门 9 之前插入 5c 闸：

```python
            # 闸门 5c: 涨停封板拒买
            if is_buy and self.config.limit_up_gate_enabled:
                prev_close, price = None, None
                if quote:
                    prev_close = quote.get("lastClose") or quote.get("preClose")
                    price = quote.get("lastPrice")

                if not (_is_valid_price(prev_close) and _is_valid_price(price)):
                    # 优先复用 quote 失败,尝试 quote_source 降级
                    try:
                        from app.data_manager.quote_source import get_realtime_quotes
                        df = get_realtime_quotes([code])
                        if df is not None and not df.empty:
                            row = df.iloc[0]
                            prev_close = row.get("last_close")
                            price = row.get("price")
                    except Exception as e:
                        logger.warning(f"闸门5c quote_source 降级失败: {e}")

                if not (_is_valid_price(prev_close) and _is_valid_price(price)):
                    gates.append(self._gate("5c", "涨停拒买", False, "有效行情", "缺价fail-safe"))
                    return (False, gates, "涨停判断缺行情,fail-safe拒买")

                is_limit, reason = is_limit_up(code, prev_close, price, strict=True)
                if is_limit:
                    gates.append(self._gate("5c", "涨停拒买", False, "未涨停", f"涨停{reason}"))
                    return (False, gates, f"涨停封板拒买: {reason}")
                gates.append(self._gate("5c", "涨停拒买", True, "未涨停", "OK"))
```

### Step 3: 更新 smoke test

在 `tests/test_live_trader_smoke.py` 中 `test_risk_gate_t1_sell_check` 之后增加：

```python
def test_risk_gate_5c_limit_up_blocks(tmp_config, store):
    """闸门5c:涨停股买入被拒,且不计入连续拒绝"""
    from app.live_trader.risk_gate import RiskGate
    from app.live_trader.kill_switch import KillSwitch
    from app.live_trader.schemas import OrderIntent

    ks = KillSwitch(tmp_config, store)
    rg = RiskGate(tmp_config, store, ks, qmt_wrapper=MagicMock(connected=True))

    intent = OrderIntent(code="600000.SH", direction="buy", volume=100, price=11.0)
    passed, gates, reason = rg.check(
        intent, asset={"cash": 500000}, quote={"lastClose": 10.0, "lastPrice": 11.0}
    )
    assert passed is False
    assert "涨停" in reason
    assert not rg._check_consecutive_rejection()


def test_risk_gate_5c_limit_up_disabled(tmp_config, store):
    """闸门5c:开关关闭时放行"""
    from app.live_trader.config import LiveTraderConfig
    from app.live_trader.risk_gate import RiskGate
    from app.live_trader.kill_switch import KillSwitch
    from app.live_trader.schemas import OrderIntent

    cfg = LiveTraderConfig(**{**tmp_config.__dict__, "limit_up_gate_enabled": False})
    ks = KillSwitch(cfg, store)
    rg = RiskGate(cfg, store, ks, qmt_wrapper=MagicMock(connected=True))

    intent = OrderIntent(code="600000.SH", direction="buy", volume=100, price=11.0)
    passed, gates, reason = rg.check(
        intent, asset={"cash": 500000}, quote={"lastClose": 10.0, "lastPrice": 11.0}
    )
    assert passed is True


def test_risk_gate_5c_missing_quote_failsafe(tmp_config, store):
    """闸门5c:quote 缺失价格时 fail-closed 拒买"""
    from app.live_trader.risk_gate import RiskGate
    from app.live_trader.kill_switch import KillSwitch
    from app.live_trader.schemas import OrderIntent

    ks = KillSwitch(tmp_config, store)
    rg = RiskGate(tmp_config, store, ks, qmt_wrapper=MagicMock(connected=True))

    intent = OrderIntent(code="600000.SH", direction="buy", volume=100, price=11.0)
    passed, gates, reason = rg.check(
        intent, asset={"cash": 500000}, quote={"lastClose": None, "lastPrice": None}
    )
    assert passed is False
    assert "fail-safe" in reason or "缺行情" in reason
```

### Step 4: 运行测试

```bash
pytest tests/test_live_trader_smoke.py::test_risk_gate_5c_limit_up_blocks tests/test_live_trader_smoke.py::test_risk_gate_5c_limit_up_disabled tests/test_live_trader_smoke.py::test_risk_gate_5c_missing_quote_failsafe -v
```

Expected: 3 passed。

### Step 5: Commit

```bash
git add app/live_trader/config.py app/live_trader/risk_gate.py tests/test_live_trader_smoke.py
git commit -m "feat(risk_gate): 新增 5c 涨停拒买闸与开关"
```

---

## Task 7: 跑基线 diff 与冒烟测试

**Files:**
- Create: `scripts/run_limit_up_baseline.py`
- Create: `scripts/diff_limit_up_baseline.py`
- Create: `docs/reports/2026-07-18-limit-up-baseline-diff.md`

### Step 1: 创建基线运行脚本

创建 `scripts/run_limit_up_baseline.py`：

```python
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.backtest.simple_runner import run_backtest


def main():
    parser = argparse.ArgumentParser(description="跑 limit-up 修复基线")
    parser.add_argument("--strategy", default="QUANTQQ", help="策略名")
    parser.add_argument("--start", default="20230101", help="起始日期")
    parser.add_argument("--end", default="20240630", help="结束日期")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    args = parser.parse_args()

    params = {
        "strategy_name": args.strategy,
        "start_date": args.start,
        "end_date": args.end,
        "initial_capital": 1_000_000,
        "position_size": 50_000,
        "min_buy_amt": 5_000,
        "hard_stop": 0.05,
        "trail_activate": 0.10,
        "trail_dd": 0.05,
        "time_exit_days": 20,
        "time_exit_profit": 0.03,
        "time_force_days": 5,
        "same_stock_cooldown": 20,
        "loss_streak_halve": 3,
        "loss_streak_pause": 5,
        "use_atr_trail": True,
        "atr_trail_multiplier": 1.0,
        "take_profit_tiers": [],
        "first_day_exit_min_profit": 0.03,
        "first_day_exit_days": 1,
        "signal_params": {},
    }

    result = run_backtest(params)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"基线已保存: {output_path}")


if __name__ == "__main__":
    main()
```

### Step 2: 跑改前基线

```bash
git stash
python scripts/run_limit_up_baseline.py --output output/baseline_before.json
```

Expected: 基线运行完成，`output/baseline_before.json` 生成。

### Step 3: 恢复修改并跑改后基线

```bash
git stash pop
python scripts/run_limit_up_baseline.py --output output/baseline_after.json
```

Expected: 改后基线运行完成，`output/baseline_after.json` 生成。

### Step 4: 创建 diff 脚本

创建 `scripts/diff_limit_up_baseline.py`：

```python
import argparse
import json
from pathlib import Path


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="对比 limit-up 修复前后基线")
    parser.add_argument("before", help="改前基线 JSON")
    parser.add_argument("after", help="改后基线 JSON")
    parser.add_argument("--output", default="docs/reports/2026-07-18-limit-up-baseline-diff.md")
    args = parser.parse_args()

    before = load(args.before)
    after = load(args.after)

    sb = before["summary"]
    sa = after["summary"]

    lines = [
        "# 5m/涨停修复基线 Diff 报告",
        "",
        f"- 策略: {before.get('strategy_name', 'QUANTQQ')}",
        f"- 区间: {before.get('start_date', '?')} ~ {before.get('end_date', '?')}",
        "",
        "| 指标 | 改前 | 改后 | 变化 |",
        "|---|---|---|---|",
        f"| 收益率 | {sb['total_return']:+.2f}% | {sa['total_return']:+.2f}% | {sa['total_return'] - sb['total_return']:+.2f}% |",
        f"| 最大回撤 | {sb['max_drawdown']:.2f}% | {sa['max_drawdown']:.2f}% | {sa['max_drawdown'] - sb['max_drawdown']:+.2f}% |",
        f"| 交易笔数 | {sb['trades']} | {sa['trades']} | {sa['trades'] - sb['trades']:+d} |",
        f"| 买入笔数 | {sb.get('buys', sb['trades'])} | {sa.get('buys', sa['trades'])} | {sa.get('buys', sa['trades']) - sb.get('buys', sb['trades']):+d} |",
        f"| 5m 降级买入笔数 | - | {sa.get('intraday_window_fallback_count', 0)} | - |",
        f"| 胜率 | {sb['win_rate']:.1f}% | {sa['win_rate']:.1f}% | {sa['win_rate'] - sb['win_rate']:+.1f}% |",
        f"| 夏普 | {sb['sharpe']} | {sa['sharpe']} | - |",
        "",
        "## 关键观察",
        "",
        "- 交易笔数变化应主要由两部分组成：5m 降级使部分跳过信号变成买入（增加），涨停 fail-closed 使部分涨停日被拒（减少）。",
        "- 若收益/回撤出现 >5% 的异常跳变，需回查具体交易明细。",
        "",
        "## 退出原因分布",
        "",
        f"- 改前: {sb['exit_reasons']}",
        f"- 改后: {sa['exit_reasons']}",
    ]

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Diff 报告已保存: {output_path}")


if __name__ == "__main__":
    main()
```

### Step 5: 生成 diff 报告

```bash
python scripts/diff_limit_up_baseline.py output/baseline_before.json output/baseline_after.json
```

Expected: `docs/reports/2026-07-18-limit-up-baseline-diff.md` 生成。

### Step 6: 人工确认可解释

阅读报告，确认：
- 买入笔数变化方向符合预期（5m 降级增加 + 涨停 fail-closed 减少）。
- 没有异常大的收益跳变（如总收益率变化 >5%）。

### Step 7: Commit 脚本与报告

```bash
git add scripts/run_limit_up_baseline.py scripts/diff_limit_up_baseline.py docs/reports/2026-07-18-limit-up-baseline-diff.md
git commit -m "docs(report): 5m/涨停修复基线 diff 报告与工具脚本"
```

---

## Self-Review Checklist

- [x] **Spec coverage**: 5m 降级（Part 1 Task 5）、涨停 fail-closed（Part 1 Task 2-4）、实盘闸（Task 6）、基线 diff（Task 7）均覆盖。
- [x] **Placeholder scan**: 无 TBD/TODO；测试代码均给出具体断言。
- [x] **Type consistency**: `can_buy` 签名、`_is_valid_price`、`is_limit_up` 在全部任务中一致。
- [x] **File paths**: 使用项目真实路径。
- [x] **Frequent commits**: 每个 Task 结束都有 commit。

---

## Execution Handoff

**Plan complete and saved to:**
- `docs/superpowers/plans/2026-07-18-5m-signal-window-and-limit-up-filter-plan-part1.md`
- `docs/superpowers/plans/2026-07-18-5m-signal-window-and-limit-up-filter-plan-part2.md`

**Execution options:**

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
