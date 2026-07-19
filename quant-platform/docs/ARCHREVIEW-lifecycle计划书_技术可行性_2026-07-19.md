# 审计报告：lifecycle.py 计划书 — 技术可行性

> 审计对象：`docs/PLAN-live_trader_lifecycle拆分_2026-07-19.md`
> 审计员：architect agent（Read/Grep 实证，禁止派子 agent）
> 日期：2026-07-19
> 方法：实读 main.py（全 742 行）/ routers/system.py / _state.py / 外部引用 + 全仓 grep 6 类符号
> 总体评级：**PASS with WARNING**（可落地，无结构性硬伤；3 处补强后实施更稳）

---

## 一、7 个核心技术问题逐项裁决

### Q1. lifespan 整段搬，12 组件 import + 装配顺序能否原样工作？✅ 认同
- 12 组件 import（main.py:67-81）全相对 import，lifecycle.py 与 main.py 同在 app/live_trader/，路径不变
- lifespan 内函数内 import（L92 runtime_state / L132 datetime as _dt / L237 OrderExecutor / L300/309 xtquant_compat）搬迁后全正确
- 装配顺序整段搬零改动，依赖链天然保持

### Q2. _state 跨模块读写一致性 ✅ 认同
- `_state.py:20` 模块级单例 dict，`from ._state import state` 拿同一引用
- 关键子验证（关闭段锁释放）：_acquire_lock 双分支写 lock_fd/lock_file（L49/L58），关闭段双 if 检测（L272-275），整段搬后写入/读取同模块内闭合

### Q3. _check_port_in_use / _acquire_lock 归属 ✅ 认同（全仓 grep）
- _check_port_in_use：仅 main.py:36 定义 + L98 lifespan 调用，无其他引用
- _acquire_lock：仅 main.py:42 定义 + L104 lifespan 调用，无其他引用
- 搬 lifecycle 正确，无耦合

### Q4. main.py 死 import 清单 ⚠️ 大体认同，补 1 项
| import | 搬后状态 |
|---|---|
| `import socket`（L8）| 死 → 删 ✓ |
| `from contextlib import asynccontextmanager`（L10）| 死 → 删 ✓ |
| `import os`（L7）| **死 → 可删**（architect 已替计划书完成"待 grep 确认"：main.py 剩余代码 L462-741 grep `os\.` 零命中）|
| `import threading as _threading`（L708）| 随进程管理搬 ✓ |
| `import subprocess`（**L706**）/ `import sys`（**L707**）| **计划书 §5 漏列**（随进程管理搬走，不致命，但清单不完整）MEDIUM |

额外 NOTE（不在本计划范围）：
- main.py L9 `import time`：搬后剩余代码也不再用于 `time.`（L395 _time 是局部别名）。也死，但阶段 3 清。
- main.py L12 HTTPException/Request：搬后剩余代码也不用。也死，阶段 3 清。

### Q5. lifecycle.py 顶部 import 完整吗 ⚠️ 大体认同，补 2 条更稳
- 计划书 §3 列的（os/threading/asynccontextmanager/date/datetime/FastAPI/get_logger/_state）全必需 ✓
- 遗漏（不影响运行，影响可读性）：subprocess/sys。_kill_all_subprocesses/_cleanup_zombies 只用 Popen 实例方法（p.kill/p.poll/p.wait），严格说不需要顶部 import（Popen 实例从 routers/system.py append 进来）。计划书 §3 注释"不需要"技术上正确。建议补顶部 import 增可读性。LOW

### Q6. lifespan 关闭段顺序整段搬后有无隐患 ✅ 认同
关闭段（L261-275）scheduler.stop→conn.stop→qmt.stop→callback.stop→store.close→notif_store.close→_kill_all_subprocesses→释放锁，全在 lifespan 体内，整段搬零改动。

### Q7. _cleanup_dryrun_residue 搬走后 main.py 内还有调用吗 ✅ 认同
全仓 grep：仅 main.py:351 定义 + L167 lifespan 调用。main.py 内只有 lifespan 一处调，搬后无残留。

---

## 二、额外发现（计划书未提）

### A1. 🔴 [MEDIUM] logger name 变化影响运维监控 —— **最值得修的一项**
计划书 §3 `logger = get_logger("live_trader.lifecycle")`。

原 main.py L17 `get_logger("live_trader.main")`。lifespan 内有大量关键启动日志：
- L85 "实盘交易模块 live_trader 启动"（启动横幅）
- L95 模式/资金/账号
- L256 "live_trader 启动完成"
- L278 "live_trader 已关闭"
- L222 "QMT 连接失败"

搬后这些日志 logger name 从 `live_trader.main` → `live_trader.lifecycle`。**若运维/NSSM/告警按 logger name grep "live_trader.main" 匹配启动事件**（如"启动完成"健康检查），搬后失配，可能误报"服务未起"。

**建议**：lifecycle.py 沿用 `get_logger("live_trader.main")`（Python 允许多模块共用同一 logger name），保持日志源标签不变。或显式文档化此变化告知运维。

### A2. 🟡 [LOW] §5 死 import 清单漏 L706/L707
`import subprocess`/`import sys` 随进程管理搬走，不是"死"但应说明，清单完整性。

### A3-A6. 🟢 [NOTE] 其他验证通过
- _state 分支检测正确性（Q2 覆盖）
- main re-export _takeover_positions 无循环 import（lifecycle 不反向 import main）✓
- routers/system.py `from ..lifecycle import` 路径正确 ✓
- routers 其他（market/config_api/trade）对 main 函数内 import 零命中，计划书 §4.1 清单完整 ✓

---

## 三、认同/存疑清单

| 计划书条目 | 裁决 | 说明 |
|---|---|---|
| §1 lifecycle 6 组内容 | ✅ 认同 | grep 验证全归属正确 |
| §2 lifespan 整段搬不重组 | ✅ 认同 | 装配依赖链零改动是最安全拆法 |
| §3 顶部 import | ⚠️ LOW | 漏 subprocess/sys，建议补 |
| §3 logger name | ⚠️ **MEDIUM** | **name 变化影响运维监控，建议沿用 "live_trader.main"** |
| §4.1 routers 4 处改源 | ✅ 认同 | grep 验证 |
| §4.2 main re-export | ✅ 认同 | 单向 import 无循环 |
| §4.3 外部靠 re-export 兜底 | ✅ 认同 | poc:94/test:614 实测命中 |
| §5 socket/asynccontextmanager 死 import | ✅ 认同 | grep 验证 |
| §5 os 待 grep 确认 | ✅ architect 已替它确认可删 | main 剩余代码零 os 用法 |
| §5 漏 L706 subprocess/L707 sys | ⚠️ LOW | 随进程管理搬，清单不完整 |
| §6 R1-R5 风险 | ✅ 认同 | |
| §7 验证清单 9 项 | ⚠️ LOW | 缺 logger name 回归检查 |
| §8 行数预估 741→~260 | ✅ 认同 | 算术对 |
| §9 回滚 / §10 YAGNI | ✅ 认同 | |

---

## 四、总体结论：PASS with WARNING

**能落地，落地后不会埋结构性雷**。

为什么 PASS：
1. lifespan 整段搬零重组 = 启动逻辑不变（最安全拆法）
2. _state 模块级 dict 引用透明，跨模块读写天然一致
3. _check_port/_acquire_lock/_cleanup_dryrun 均只 lifespan 单点使用（grep 实证）
4. 4 处 routers + 2 处外部 re-export 改源清单完整
5. 无循环 import（lifecycle 不反向 import main）

WARNING 项（建议实施前修订）：
1. 🔴 **logger name**：lifecycle.py 沿用 `get_logger("live_trader.main")`，否则启动/关闭/QMT 失败等关键日志 logger name 全变，运维监控可能失配。**最值得修**。
2. 🟡 §5 死 import 表补 L706 subprocess / L707 sys
3. 🟢 §3 顶部 import 补 subprocess/sys（可读性）
4. 🟢 §7 验证清单加 logger name 回归检查

所有结构性决策均经实读代码 + grep 验证站得住，不构成阻断。
