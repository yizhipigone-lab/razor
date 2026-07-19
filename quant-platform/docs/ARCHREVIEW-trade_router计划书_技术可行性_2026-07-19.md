# 审计报告：trade router 计划书 — 技术可行性

> 审计对象：`docs/PLAN-live_trader_trade_router拆分_2026-07-19.md`
> 审计员：architect agent（Read/Grep 实际验证，禁止派子 agent）
> 日期：2026-07-19
> 范围：仅技术可行性（不审文档措辞）
> 结论：**PASS（带 WARNING）** — 整套拆法能落地，但 R5 风险点漏掉 2 处相对 import 调整，trade.py 顶部 import 漏列 `date`/`time`，落地前必须补强

---

## 一、总体结论

| 维度 | 评级 | 说明 |
|---|---|---|
| 循环 import 规避策略 | ✅ PASS | 函数内 import 模式已在 system/market/config_api 三轮验证可行 |
| 3 个核心服务留 main | ✅ PASS | 决策正确，避免连带改 scheduler 引发更大爆炸半径 |
| scheduler.py:436 不动 | ✅ PASS | process_buy_signals 留 main，调用链不变 |
| asyncio 归属判断 | ⚠️ WARNING | R4 判断正确，但计划书仍写"import asyncio  # 备用"是自相矛盾的死代码 |
| 路由级相对 import 调整 | ❌ 计划书漏掉 2 处 | R5 只覆盖 cancel-by-source 的绝对路径，未提 `.schemas`/`.price_type` 必须改 `..` |
| trade.py 顶部 import 完整性 | ❌ 计划书漏列 | place_order 路由用 `date.today()`/`time.time()`，但顶部 import 设计没列 `date`/`time` |
| 整体能否落地 | ✅ 能 | 补齐上述 3 处后，落地后不会埋雷 |

**一句话**：技术判断 95% 站得住，但有 3 个**机械性遗漏**（不是设计错误），落地时必须补全，否则 `/live/order` 上线即 NameError。

---

## 二、7 个核心技术问题逐个回答

### Q1. 函数内 import 能否不循环？main 顶部 import trade_router 会不会冲突？
**✅ 认同计划书，不循环、不冲突**
- main.py 顶部 `from .routers.trade import router as trade_router` 触发 trade.py 加载
- trade.py 顶部只 import `_state` + `auth` + stdlib/fastapi/logger，**没有任何一条会回头 import main**
- 路由函数内 `from ..main import place_order_service` 是运行时调用，此时 main.py 已完成模块级执行（place_order_service 在 L654 已定义），从 sys.modules 拿到完整模块
- 同模式已在 system.py / market.py / config_api.py 三处落地验证
- **关键约束**：trade.py 顶部**绝对不能**直接 `from ..main import place_order_service`，否则 main.py 部分加载状态下 place_order_service 还未定义，会 ImportError

### Q2. process_buy_signals 留 main、scheduler.py:436 不动的决策对不对？
**✅ 认同计划书，决策正确**
- `scheduler.py:436` `from .main import process_buy_signals`（函数内 import）
- 阶段 1.4 后 process_buy_functions 仍定义在 main.py:713，scheduler import 路径不变 → **零改动**
- 这是"小步重构"正确做法：阶段 1.4 只搬路由壳，阶段 3 才搬服务（连带改 scheduler），爆炸半径最小
- 反例：若阶段 1.4 同时搬 process_buy_signals，scheduler.py:436 要同步改，两文件联动，违反"独立小步"

### Q3. process_buy_signals / _process_one_signal 的闭包/模块级依赖能否全解析？
**✅ 认同，留 main 时所有依赖正常解析**
grep 确认这两个函数用到的模块级名字（全在 main.py 顶部或同模块）：
- `asyncio`(L6) → L776/779/874
- `logger`(L17) → L758/773/808
- `_state`(L22) → L742/743/746
- `datetime`/`date`(L11) → L754/830
- `place_order_service`(L654 同模块) → L875
- 函数内 import（schemas/buy_volume/xtquant_compat/hashlib/settings）相对 main.py 单点路径**保持不变**
- **未发现隐藏模块级变量依赖**

### Q4. asyncio 归属：buy_signal 路由本体是否真的不直接用 asyncio？
**✅ 认同判断，但反对计划书第 46 行的 `import asyncio`**
- main.py:881-920 buy_signal 路由本体：grep `asyncio\.` 在该范围 **0 命中**
- 路由内只 `await process_buy_signals(...)`，并发逻辑在 service 内
- 全文 `asyncio\.` 只在 L776/779/874（留 main 的函数内）
- **矛盾点**：计划书第 46 行 `import asyncio  # 备用`，但 R4 自己又说"trade router 不需要 asyncio" → **死代码，违反 YAGNI**
- 对比 config_api.py:21 的 `import asyncio` 是真用了（L232 asyncio.sleep），trade.py 真没用
- **建议**：删除 trade.py 顶部 `import asyncio`

### Q5. `/live/audit/replay/{order_id}` 路径参数路由搬 APIRouter 后匹配受影响吗？
**✅ 不受影响，认同 R3**
- FastAPI 路径参数匹配由 starlette 统一处理，与 @app 还是 @router 无关
- 搬到 @router.get + include_router 后匹配规则完全一致
- market.py 已搬多个复杂路径，均正常

### Q6. place_order_service 被 WEB 和 TDX 共用，搬路由后共用关系是否破坏？
**✅ 不破坏，认同计划书**
- place_order_service 定义 main.py:654，**留 main**
- caller 1：/live/order（搬 trade.py）→ 函数内 import → source="WEB", lock_wait=30s
- caller 2：_process_one_signal（留 main L814）→ 同模块直接调 → source="TDX", lock_wait=5s
- 阶段 1.4 内：caller 2 同模块调用不受影响；caller 1 改函数内 import，运行时拿到同一函数对象 → **source 参数语义、价格策略、terminal 标记全不变**

### Q7. R1-R5 是否完整？有无未覆盖的技术坑？
**⚠️ R5 不完整，漏 2 类相对 import 调整 + 1 类顶部 import 漏列**

计划书 R5 只讨论 cancel-by-source 的绝对路径（不变），**完全没覆盖**：

**漏点 1**：/live/order 路由内相对 import 搬到 trade.py 后必须单点→双点：
- `main.py:573` `from .schemas import OrderIntent, OrderRequest` → `from ..schemas import`
- `main.py:599` `from .price_type import map_price_type` → `from ..price_type import`

**漏点 2**：/live/buy-signal 路由内：
- `main.py:887` `from .schemas import BuySignalRequest` → `from ..schemas import`

**漏点 3**：trade.py 顶部 import 漏列 `date` 和 `time`：
- `main.py:632` `f"{date.today()}|...|{int(time.time() * 1000)}"` 用了 date 和 time
- 来自 main.py:9 `import time` 和 main.py:11 `from datetime import date, datetime`
- 计划书顶部 import 设计**没列这两个**
- 搬过去后 place_order 路由会 `NameError: name 'date' is not defined`

**漏点 4**（次要）：cancel_by_source L934 `from app.utils.xtquant_compat` 绝对路径不变 — R5 判断正确。

---

## 三、对计划书技术判断的认同/存疑清单

| # | 计划书判断 | 评级 | 备注 |
|---|---|---|---|
| 1 | 3 核心服务留 main，阶段 3 才搬 | ✅ 认同 | 爆炸半径最小 |
| 2 | trade router 函数内 import 调 service | ✅ 认同 | 3 个 router 已验证 |
| 3 | trade.py 顶部 `from .._state import` | ✅ 认同 | 一致 |
| 4 | trade.py 顶部 `from ..auth import _verify_token, _require_admin` | ✅ 认同 | auth.py 独立 |
| 5 | trade.py 顶部 `import asyncio  # 备用` | ❌ 反对 | 死代码，违反 YAGNI |
| 6 | trade.py 顶部 import 清单 | ⚠️ 不完整 | **漏列 `from datetime import date` 和 `import time`** |
| 7 | R1 place_order 链路 CRITICAL | ✅ 认同 | 真实下单必须实测 |
| 8 | R2 buy-signal + scheduler 共用 CRITICAL | ✅ 认同 | 阶段 1.4 不动 scheduler 正确 |
| 9 | R3 路径参数路由需验证 | ✅ 认同 | pytest 已覆盖 |
| 10 | R4 asyncio 留 main 不是死 import | ✅ 认同 | L776/779/874 使用 |
| 11 | R5 cancel-by-source 绝对路径低风险 | ✅ 认同 | 但 **R5 范围不够**，漏 /live/order 和 /live/buy-signal 相对 import |
| 12 | 拆分顺序 batch 1→2→3 | ✅ 认同 | 合理 |
| 13 | 验证清单 7 项 | ⚠️ 不完整 | 缺第 8 项：grep trade.py 不应有 `from .schemas`/`from .price_type` |
| 14 | 行数预估 1007 → ~720 | ✅ 认同 | 合理 |
| 15 | 回滚预案 | ✅ 认同 | 标准 small-step 兜底 |

**合计**：13 认同 / 2 不完整 / 1 反对 / 0 致命错误。

---

## 四、落地前必须补强的 3 个修正

**修正 1（CRITICAL）**：trade.py 顶部 import 补齐 date 和 time，删 asyncio
```python
import time
from datetime import date
from fastapi import APIRouter, HTTPException, Request
from core.logger import get_logger
from .._state import state as _state
from ..auth import _verify_token, _require_admin
logger = get_logger("live_trader.routers.trade")
router = APIRouter()
```

**修正 2（CRITICAL）**：路由内相对 import 单点→双点
- /live/order：`from .schemas` → `from ..schemas`、`from .price_type` → `from ..price_type`
- /live/buy-signal：`from .schemas` → `from ..schemas`
- cancel_by_source 的 `from app.utils.xtquant_compat` 保持绝对路径不变

**修正 3（HIGH）**：验证清单加第 8 项
```bash
grep -n "from \.schemas\|from \.price_type\|from \.buy_volume" app/live_trader/routers/trade.py
# 预期: 0 命中(应该是 .. 双点)
```

---

## 五、PASS/FAIL/WARNING 汇总

| 项 | 评级 |
|---|---|
| 循环 import 规避 | PASS |
| 核心服务留 main 决策 | PASS |
| scheduler 不动决策 | PASS |
| place_order_service 共用关系保持 | PASS |
| asyncio 归属判断 | PASS（但计划书写法需修正） |
| 路径参数路由匹配 | PASS |
| **trade.py 顶部 import 完整性** | **FAIL（漏列 date/time）** |
| **R5 相对 import 覆盖范围** | **FAIL（漏 2 处单点→双点）** |
| 验证清单完整性 | WARNING（建议加第 8 项） |

**总体：PASS（带 WARNING）** — 设计思路全部正确，但 2 处机械性遗漏必须实施前补进计划书，否则搬完 /live/order 路由会在生产环境 `NameError: name 'date' is not defined`，真实下单端点 500。
