# 审计报告：lifecycle.py 拆分计划书 — 文档质量

> 日期：2026-07-19
> 审计对象：docs/PLAN-live_trader_lifecycle拆分_2026-07-19.md
> 审计维度：**计划书作为施工蓝图的质量**（不审代码本身）
> 审计方法：Read/Grep 实际代码验证每条结论，引用具体 file:line
> 审计员：code-reviewer agent（未派子 agent）
> 总体结论：**WARNING** — 可作施工蓝图，但 1 个 HIGH 必须修，3 个 MEDIUM 建议修，4 个 LOW 可选

---

## 一、Finding 清单（按严重级）

### CRITICAL：无

---

### HIGH

#### H1 [完整性 + 一致性] _cleanup_dryrun_residue 行号错 36 行

**严重级**：HIGH（不影响实盘资金，但让施工者对函数规模、R5 风险判断产生偏差）

**证据**：
- app/live_trader/main.py:351-459 实际范围 —— L351 def 开始，L459 logger.error 是函数体最后一行（except 块收尾）
- L460 起是 # ===== FastAPI app ===== 和 app = FastAPI(...)，**不属于该函数**
- 计划书第 1 节表（行 26）：L351-495（145 行）—— **行号尾错 36 行**
- 计划书第 8 节（行 129）：_cleanup_dryrun(145) —— **行数算错**
- 计划书 R5（行 110）：大函数（145 行）—— **同源错误**

**真实算术**（重新核算）：

| 函数 | 计划书行数 | 实际行数 | 差 |
|---|---|---|---|
| lifespan | 208 (L65-278) | 214 (L65-278) | -6 |
| _check_port + _acquire_lock | 30 | 24 (L36-39 + L42-61) | -6 |
| _takeover | 68 | 68 (L281-348) | 0 |
| _cleanup_dryrun | **145** | **109** (L351-459) | **+36** |
| 进程管理 | 28 | 28 (L704-731) | 0 |
| 合计 | ~480 | ~443 | +37 |

main.py 741 实际 ~298 行（计划书估 ~260，偏乐观 38 行）。**结论方向不变**（远低于 EVAL 原估 ~500），但底层数字错。

**建议**：
1. 第 1 节表行 26：L351-495 改 L351-459，备注列 145 行 改 109 行
2. 第 8 节算式：_cleanup_dryrun(145) 改 (109)，合计 ~480 改 ~443
3. R5 描述：145 行 改 109 行
4. main.py 终态预估：~260 行 改 ~298 行（仍达标，但更诚实）

---

### MEDIUM

#### M1 [一致性] 组件 import 数三处打架，且都与实际不符

**严重级**：MEDIUM

**证据**：
- 计划书 TL;DR（行 12）：装配 13 组件
- 计划书第 1 节（行 24）：装配 13 组件 + 连 QMT + 接管 + 调度器
- 计划书第 2 节（行 35）：lifespan 内的 12 个组件 import（L67-81）
- 实际 main.py:67-81 数 import 语句：**15 个**
  - L67 load_config / L68 LiveTraderStore / L69 Notifier / L70 NotificationStore
  - L71 KillSwitch / L72 ClearanceLock / L73 QmtWrapper / L74 CallbackHandler
  - L75 ConnectionManager / L76 RiskGate / L77 PnlEngine / L78 Reconciler
  - L79 ExitMonitor / L80 AuditLogger / L81 LiveScheduler

**问题**：3 个数字（13/12/15）互相打架，且都与实际（15）不符。施工者照 12 个清点会漏核 3 个 import 是否成功搬运。

**建议**：统一改为 **15**。第 1 节 装配 13 组件 的口径需说明 —— 是把 logger/_state 这种不算 业务组件，还是 13 本身就错？建议口径：装配 15 个业务组件（L67-81 全部 import）。

---

#### M2 [可行性] 第 3 节顶部 import 设计遗漏 lifespan 内的 3 个函数内 import

**严重级**：MEDIUM（不会导致 bug，但实施者读计划书会困惑 这些要不要搬到顶部）

**证据**：lifespan 函数体内还有 3 个**函数内局部 import**：
- main.py:92 from .runtime_state import load_runtime_state（lifespan 启动段）
- main.py:132 from datetime import datetime as _dt（kill switch 残留解析）
- main.py:237 from .order_executor import OrderExecutor（executor 创建段）

计划书第 2 节只提了 12 个组件 import (L67-81)，第 3 节顶部 import 设计也没列这 3 个。

**评估**：这 3 个都是**函数内局部 import**，搬整段 lifespan 后跟着进入 lifecycle.py，相对路径 from .xxx 在同包（app/live_trader/）下**不变**。L132 from datetime import datetime as _dt 是 std 局部重命名，搬后 OK。所以**不会出 bug**。

**但计划书没说清 局部 import 跟着搬**，实施者可能误以为要把它们提到 lifecycle.py 顶部（会改变作用域语义，虽然此处无害）。

**建议**：第 2 节或第 3 节加一句：lifespan 函数内还有 3 个局部 import（L92 runtime_state / L132 datetime as _dt / L237 order_executor），整段搬后保留在 lifespan 函数体内不动，不提到顶部。相对路径 from .xxx 同包不变。

---

#### M3 [风险识别] 日志源标识变化未识别（R1-R5 全没提）

**严重级**：MEDIUM（影响运维检索/告警，不影响实盘资金）

**证据**：
- 计划书第 3 节：logger = get_logger("live_trader.lifecycle") —— logger 名 **变了**
- 但搬走的 lifespan（22 处 logger 调用，L84-278）+ _takeover_positions（2 处，L346-347）+ _cleanup_dryrun_residue（8 处，L406-457）共 **32 处 logger 调用**会改用 live_trader.lifecycle logger
- 现网日志/告警若按 module=live_trader.main 检索启动日志、持仓接管日志、dry-run 残留日志，**搬迁后全部 丢**（实际是改名了）
- 计划书 R1-R5 5 个风险点**全没提**日志源变化

**建议**：补一个风险点 R6：

    MEDIUM R6 日志源标识变化
    - lifespan + 生命周期函数共 32 处 logger 调用搬走后，日志 module 名
      从 live_trader.main 变成 live_trader.lifecycle。
    - 影响：运维按 main 检索启动/接管/残留日志会 丢（实际改名）。
    - 缓解：通告运维/告警规则改 grep live_trader.(main|lifecycle)；
      或保持 logger 名不变（lifecycle.py 也用 get_logger("live_trader.main")）。
    - 推荐：用新名 live_trader.lifecycle（语义清晰），同步更新告警规则。

---

### LOW

#### L1 [完整性] 第 5 节死 import 清理漏列 3 个

**严重级**：LOW

**证据**（grep 验证）：
- main.py:9 import time —— grep time. 在 main.py **无任何使用**（L383 是函数内 import time as _time 跟着 _cleanup_dryrun 搬走）。**L9 现在就是死 import**，计划书第 5 节没列。
- main.py:706 import subprocess —— grep 显示 main.py 除 L706 import 本身**无 subprocess. 使用**（Popen 创建在 routers/system.py）。随进程管理搬走应删，计划书第 5 节只提了 L708 threading，**漏了 L706/L707**。
- main.py:707 import sys —— 同上，main.py 除 L707 import 本身**无 sys. 使用**。同漏。

**建议**：第 5 节表补 3 行：

| import | 状态 |
|---|---|
| import time（L9）| 现在就已死（grep 无 time. 使用）删 |
| import subprocess（L706）| 随进程管理搬走删（grep 无 subprocess. 使用）|
| import sys（L707）| 随进程管理搬走删（grep 无 sys. 使用）|

---

#### L2 [完整性] os 待 grep 确认 审计给出明确结论

**严重级**：LOW

**证据**：grep os. 在 main.py 全部命中在搬迁范围（L46/53/55/57 _acquire_lock；L114/274/275 lifespan）。**main.py 剩余代码（L460 之后，含 place_order_service / process_buy_signals / _process_one_signal）零 os. 使用**。

**结论**：import os（L7）搬走后**可删**。计划书第 5 节留的 待 grep 确认 可以确认：删。

**建议**：第 5 节 import os 行备注改为：可删（grep 确认 main 剩余代码零 os. 使用）。

---

#### L3 [可测试性] 验证清单项 6 grep 模式漏掉多符号 import 的部分符号

**严重级**：LOW

**证据**：routers/system.py:188/217 的 import 是**多符号一行**：

    from ..main import _cleanup_zombies, _spawned_lock, _spawned_processes

若实施时只改了 _cleanup_zombies 没改 _spawned_lock/_spawned_processes（或反之），验证清单项 6 grep 模式 from ..main import _takeover|from ..main import _cleanup_zombies **不会捕获**这种部分漏改。

**建议**：验证清单项 6 grep 模式扩展为：

    grep -E "from \.\.main import .*(_takeover|_cleanup_zombies|_spawned_lock|_spawned_processes|_kill_all_subprocesses)" routers/system.py

预期 0 命中。

---

#### L4 [优先级] 第 1 节 6 组函数编号暗示分步，但实际应整组一次搬

**严重级**：LOW

**证据**：第 1 节用 #1-#6 编号（行 21-27），给人的感觉是 按顺序分步搬。但：
- lifespan（#3）依赖 #1/2（启动辅助）+ #4/5（接管/清理）+ #6（关闭段调 _kill_all_subprocesses）
- 分步搬会出现 lifespan 已搬但 _takeover 还在 main 的中间态 → import 循环或断链
- 第 9 节回滚预案 lifecycle.py 删除即可 暗示一次性，第 7 节验证清单也是整体验证

**评估**：实际方案是**整组一次性搬**（合理），但计划书没明说，第 1 节的编号有歧义。

**建议**：第 1 节表前加一句：6 组函数整组一次性搬到 lifecycle.py，编号仅作清单索引，不分步（lifespan 跨组依赖，分步会产生中间断链）。

---

## 二、6 维度逐项结论

### 1. 完整性：有缺
- 6 组函数覆盖（lifespan 装配段 L110-157 组件初始化 / L159-226 连 QMT / L228 _state.update / L236 executor / L248 scheduler / L259-278 关闭段，**全部在 lifespan 函数体内，整段搬零遗漏**）
- _cleanup_dryrun_residue 的 live/dry-run 分支逻辑（计划书 R5 提到，整段搬保留分支）
- **缺**：lifespan 内 3 个局部 import（M2）、日志源变化（M3）、3 个死 import 清理（L1）

### 2. 一致性：有冲突
- 行数预估 741 -> ~260 与搬走量 ~480 **算术自洽**，但底层 _cleanup_dryrun 多算 36 行（H1）
- **冲突**：组件 import 数 13/12/15 三处打架（M1）

### 3. 可行性：可落地
- lifespan 整段搬 + 相对路径不变方案**可行**（from .xxx 在 lifecycle.py 同包 app/live_trader/ 下不变）
- 顶部 import 设计**基本完整**（asynccontextmanager/os/threading/date/datetime/FastAPI/get_logger/_state 都对）
- 不需要 subprocess/sys 的判断**正确**（Popen 实例方法，对象在 system router 创建）
- **小缺**：没说清局部 import 跟着搬（M2）

### 4. 风险识别：部分准
- R1（lifespan 启动核心）准
- R2（_state 跨模块读写一致）准 —— dict 单例引用透明，_state["lock_fd"] 跨函数但同 lifecycle.py 模块 OK
- R3（import 改源漏改）准
- R4（死 import 残留）部分（漏列 time/subprocess/sys，见 L1）
- R5（_cleanup_dryrun 大函数机械搬）描述行数错（H1）
- **漏**：R6 日志源标识变化（M3）

### 5. 优先级：合理
- 6 组函数整组搬，编号仅索引（L4 建议补说明）
- YAGNI 边界清晰（第 10 节明确不搬 services / 不改外部脚本 / 不重组装配）

### 6. 可测试性：基本够
- 9 项验证清单覆盖 compile/import/identity/routes/grep/pytest/冒烟
- 人工启动冒烟（项 9）**必须保留** —— lifespan 是服务入口，单测 mock 了 QMT/xtquant，真实启动才验证装配链
- **小缺**：项 6 grep 模式漏多符号 import 部分漏改场景（L3）

---

## 三、Review Summary

| Severity | Count | Status |
|----------|-------|--------|
| CRITICAL | 0 | pass |
| HIGH     | 1 (H1) | warn |
| MEDIUM   | 3 (M1/M2/M3) | warn |
| LOW      | 4 (L1/L2/L3/L4) | note |

**Verdict**：**WARNING** — 这份计划书**能作为施工蓝图**（核心方案：整组搬 + 不重组装配顺序 + 相对路径不变 + 多源 import 改源 + main re-export 兜底，经代码验证全部可行；资金安全无忧，因不动交易逻辑）。但 **H1 必须修**（行号错 36 行会让施工者对函数规模和风险产生偏差），M1/M2/M3 强烈建议修（一致性/可行性/风险识别各有盲点），L1-L4 可选但修了更稳。

**迭代建议优先级**：
1. 修 H1（3 处行号 + 算术）
2. 修 M1（组件数统一 15）
3. 修 M3（补 R6 日志源风险）
4. 修 M2 + L4（各加一句话澄清）
5. 修 L1 + L2（死 import 表补全）
6. 修 L3（grep 模式扩展）

修完 1-3 即可批准实施；4-6 可在实施 PR 中顺手清。

---

## 四、审计过程留痕（验证点）

每条结论的代码验证记录：

- main.py 实读全文（741 行），核对 _check_port L36-39 / _acquire_lock L42-61 / lifespan L64-278（含装饰器）/ _takeover L281-348 / _cleanup_dryrun L351-459 / 进程管理 L704-731 的真实范围
- routers/system.py 实读全文，核对 L69/91/188/217 四处函数内 import 属实，多符号 import 行（L188/217）确认为 _cleanup_zombies+_spawned_lock+_spawned_processes 三符号
- scripts/poc_dryrun_e2e.py:94 / tests/test_live_trader_smoke.py:614 两处外部 import 验证属实
- grep os. / time. / subprocess. / sys. / logger. 在 main.py 各命中位置已逐个核对，确认搬迁范围与残留状态
- 未派子 agent（遵守硬约束）
