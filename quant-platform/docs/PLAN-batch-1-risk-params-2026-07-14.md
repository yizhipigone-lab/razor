# Batch 1 PLAN：风控参数集中（候选8）

> 日期：2026-07-14
> 状态：待用户确认后执行
> 分批顺序：Batch 1 / 8

---

## 现状与问题

项目里有三层风控参数读写，分散在 3 种模式里：

| 模式 | 位置 | 数据源 | 用途 |
|------|------|--------|------|
| `_cfg()` 函数 | engine.py、intraday_monitor.py、exit_monitor.py | settings → config.py | 运行时交易 |
| 直接 import config.py 常量 | tdx_runner.py、api/backtest.py:381 | config.py（import 时快照） | 回测默认值 |
| `load_risk_params()` | schema.py | config.py 直接读（不是 settings） | AI 优化器 search_space |

**根本问题：** `load_risk_params()` 读 config.py，`_cfg()` 读 settings → config，**两者数据源顺序不一致**，同一参数在不同地方可能返回不同值。

**`apply_bt_to_system`（api/backtest.py:492）是双向写：同时写 config.py 模块变量 + settings JSON，重启前 settings 是真，重启后 config.py 是真。**

---

## 目标

统一所有风控参数的读取路径：
- **唯一路径：** `settings` 优先读 → `config.py` 兜底
- **返回：** `RiskParams` frozen dataclass
- **向后兼容：** 旧的 `load_risk_params()` 调用方不改一行调用代码

---

## 改动清单（5 个文件）

### 1. 新建 `app/config/risk_params.py`

```python
"""
风控参数集中加载层（2026-07-14）
数据流：settings（app_setting.json） → config.py 兜底
与 engine._cfg() / intraday_monitor._cfg() / exit_monitor._load_risk_params() 行为完全一致
"""
from dataclasses import dataclass
from typing import List
from core.settings import settings as _settings

@dataclass(frozen=True)
class RiskParams:
    """风控参数 — frozen dataclass，tests 可直接 mock"""
    hard_stop: float                   # 负数，如 -0.06
    trail_activate: float              # 正数，如 0.05
    trail_dd: float
    take_profit_tiers: list            # [{"profit_pct": 0.03, "sell_ratio": 0.30}]
    time_exit_days: int
    time_exit_profit: float
    time_force_days: int
    first_day_exit_min_profit: float
    first_day_exit_days: int
    use_atr_trail: bool = False
    atr_trail_multiplier: float = 1.0

def _g(key: str, default):
    """读 settings → config.py 兜底"""
    val = _settings.get("risk", key)
    if val is not None:
        return val
    import app.sim_trader.config as _sc
    return getattr(_sc, key.upper(), default)

def load_risk_params() -> RiskParams:
    return RiskParams(
        hard_stop=_g("hard_stop_loss_pct", -6.0) / 100.0,
        trail_activate=_g("trailing_stop_activate_pct", 5.0) / 100.0,
        trail_dd=_g("trailing_stop_drawdown_pct", 2.0) / 100.0,
        take_profit_tiers=_g("take_profit_tiers", [{"profit_pct": 0.03, "sell_ratio": 0.30}]),
        time_exit_days=_g("time_exit_days", 7),
        time_exit_profit=_g("time_exit_min_profit_pct", 3.0) / 100.0,
        time_force_days=_g("time_exit_force_days", 12),
        first_day_exit_min_profit=_g("first_day_exit_min_profit", 0.0),
        first_day_exit_days=_g("first_day_exit_days", 1),
        use_atr_trail=_g("use_atr_stop", False),
        atr_trail_multiplier=_g("atr_stop_multiplier", 1.0),
    )
```

### 2. 修改 `app/config/schema.py`

- 保留 `RiskSchema` dataclass（其他模块可能引用）
- `load_risk_params()` 改为调用 `app.config.risk_params.load_risk_params()` 并转换为 `RiskSchema`，加 `DeprecationWarning`

```python
def load_risk_params() -> RiskSchema:
    import warnings
    warnings.warn(
        "app.config.schema.load_risk_params is deprecated. "
        "Use app.config.risk_params.load_risk_params instead.",
        DeprecationWarning,
        stacklevel=2
    )
    from app.config.risk_params import load_risk_params as _new
    p = _new()
    return RiskSchema(
        hard_stop=p.hard_stop,
        trail_activate=p.trail_activate,
        trail_dd=p.trail_dd,
        time_exit_days=p.time_exit_days,
        time_exit_profit=p.time_exit_profit,
        time_force_days=p.time_force_days,
        first_day_exit_min_profit=p.first_day_exit_min_profit,
        first_day_exit_days=p.first_day_exit_days,
        take_profit_tiers=p.take_profit_tiers,
        use_atr_trail=p.use_atr_trail,
        atr_trail_multiplier=p.atr_trail_multiplier,
    )
```

### 3. 修改 `app/sim_trader/engine.py:490-512`

删除：
- `_cfg()` 函数定义（约 6 行）
- `sim_params` dict 构建（约 13 行）

改为：
```python
from app.config.risk_params import load_risk_params
sim_params = load_risk_params()
# sim_params 是 dataclass，字段直接用 .hard_stop 等
```

### 4. 修改 `app/sim_trader/intraday_monitor.py:131-165`

删除：
- `_cfg()` 函数定义（约 7 行）
- inline `sim_params` dict（约 13 行）

改为：
```python
from app.config.risk_params import load_risk_params
params = load_risk_params()
# 直接用 params.hard_stop 等，类型正确（已转小数）
```

### 5. 修改 `app/live_trader/exit_monitor.py:216-236`

删除：
- `_load_risk_params()` 方法（约 21 行）

改为：
```python
from app.config.risk_params import load_risk_params
params = load_risk_params()
# 用 params.hard_stop 等
```

---

## 不改的文件（向后兼容）

| 文件 | 不改原因 |
|------|----------|
| `app/api/backtest.py:492` 的 `apply_bt_to_system` | 写 config.py 模块变量是运行时缓存机制，不动；它也写 settings，reload 后新函数能读到 |
| `app/backtest/tdx_runner.py` | 回测用 import 时刻快照值，合理；不受新函数影响 |
| `app/backtest/simulate_one_trade.py` | 走 schema.py 废弃兼容路径，能继续工作 |

---

## 验证方式

| 步骤 | 命令 | 期望 |
|------|------|------|
| 1 | `python -c "from app.config.risk_params import load_risk_params; r=load_risk_params(); print(r.hard_stop)"` | 输出数字（如 -0.06） |
| 2 | `python -c "from app.config.schema import load_risk_params; r=load_risk_params(); print(r.hard_stop)"` | 同样输出 + DeprecationWarning |
| 3 | 启动 sim_trader，验证 check_stops 正常工作 | 日志无报错 |
| 4 | 对比修改前后的 check_stops 返回值 | 一致 |

---

## 改动量

| 文件 | 增/删 |
|------|-------|
| `app/config/risk_params.py` | 新增 ~50 行 |
| `app/config/schema.py` | 修改 ~20 行 |
| `app/sim_trader/engine.py` | 删除 ~23 行，改 ~2 行 |
| `app/sim_trader/intraday_monitor.py` | 删除 ~20 行，改 ~2 行 |
| `app/live_trader/exit_monitor.py` | 删除 ~21 行，改 ~2 行 |
| **合计** | **新增 50 行，删除 64 行，修改 6 行** |

---

## 执行顺序

1. 新建 `app/config/risk_params.py`
2. 修改 `app/config/schema.py`（加 DeprecationWarning 包装）
3. 三个调用方去 `_cfg` + 改 import（顺序无关）
4. 跑验证命令确认无报错
