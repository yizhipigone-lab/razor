# 实盘手工交易下单功能 — 项目实施计划书

> 版本: v1.1(经 code-reviewer 审计迭代) | 日期: 2026-07-18 | 状态: 已审计,可执行
> 作者: Claude | 参考: MQ 项目手工下单功能调研(2026-07-18)

---

## 1. 背景与目标

### 1.1 背景
- 用户需要在前端手工下单(非策略自动单),补足"应急人工干预"能力。
- 调研 MQ 项目(`E:\1target\MQ\MQ`)的手工下单:Vue3+Element Plus 前端 + Node.js 中间层 + Python FastAPI QMT 代理。
- **结论:不移植 MQ,自建。** 我们的后端能力已全面覆盖(且风控更严),唯一缺口是前端表单 + 少量端点。

### 1.2 现状盘点(已核实,文件:行号)

| 能力 | 现状 | 位置 |
|---|---|---|
| 下单主干 | ✅ 已有 `/live/order` → OrderExecutor → 10 道风控闸门 | app/live_trader/main.py:663 |
| 买量规则(科创板200股等) | ✅ 已有 | app/live_trader/buy_volume.py |
| 撤单能力 | ✅ 已有 `cancel_order()` / `query_orders(cancelable_only)` | app/live_trader/qmt_wrapper.py:222,301 |
| 市价类型常量 | ✅ xtquant 齐全(44/45/42/43/47/5/11) | xtquant.xtconstant |
| 委托/成交流水 | ✅ live_deals + live_audit + WebSocket 推送 | app/live_trader/store.py |
| 持仓同步(接管) | ✅ `_takeover_positions` 可复用(保留 peak_price/sell_count/entry_date) | app/live_trader/main.py:274 |
| **前端下单表单** | ❌ 缺失(唯一大缺口) | static/index.html |
| **撤单 HTTP 端点** | ❌ 缺失(wrapper 有,端点没暴露) | — |
| **市价类型市场感知映射** | ❌ 缺失(现 price_type 直接透传 int) | — |

### 1.3 目标(用户已确认范围)
1. **下单表单(核心)**: 代码/方向/价格类型/价格/数量 + 二次确认,POST /live/order
2. **限价 + 常用市价类型**: 限价/最新价/对手最优/本方最优/沪五档撤销/沪五档转限价/深五档撤销,市场感知映射
3. **表单内嵌资金显示**: 实时现金/可用资金,买入金额实时估算对比
4. **下单后自动持仓同步**: 成交后本地持仓立即与 QMT 对齐,不等每日4次对账
5. **撤单按钮**: 委托列表对可撤单加撤单按钮
6. **dry-run 禁用**: 模拟模式下手工下单硬禁用(防误下真单)

### 1.4 非目标(本期不做)
- 条件单/止损单/定时单等高级单类型
- 批量下单/篮子单
- 移动端(x-uni 类)
- 改持仓架构为 MQ 式"QMT 直读"(已论证:我们的有状态止盈需要本地台账)

---

## 2. 技术设计

### 2.1 总体架构

```
[前端 native JS] 手工下单表单(实盘tab新折叠区)
   │ POST /live/order (新增 price_type_key 市价键)
   ▼
[后端 FastAPI] /live/order
   ├─ dry-run → 硬拒 403(行为变更,见 §4 风险R2)
   ├─ kill_switch 检查(已有)
   ├─ price_type_key → price_type.py 市场感知映射 → xtconstant int
   └─ OrderExecutor.execute() → 10 闸门 → QMT
   │
   ├─ 成交回调(已有 callback_handler) → 更新本地持仓
   └─ 前端下单成功 5s 后 → POST /live/positions/sync(新端点) → 本地持仓对齐 QMT

[撤单] 委托列表"撤单"按钮 → POST /live/order/cancel(新端点) → qmt.cancel_order()
```

### 2.2 模块设计

#### M1: 市价类型映射模块(新建 `app/live_trader/price_type.py`)

纯函数模块,无依赖,好测试。职责:市价键 + 股票代码 → xtconstant int。

```python
# 市价键 → (说明, 是否需要价格输入)
PRICE_TYPE_KEYS = {
    "limit":      ("限价", True),          # FIX_PRICE=11
    "latest":     ("最新价", False),        # LATEST_PRICE=5
    "peer_best":  ("对手最优", False),      # MARKET_PEER_PRICE_FIRST=44
    "mine_best":  ("本方最优", False),      # MARKET_MINE_PRICE_FIRST=45
    "sh5_cancel": ("沪五档撤销", False),    # MARKET_SH_CONVERT_5_CANCEL=42
    "sh5_limit":  ("沪五档转限价", False),  # MARKET_SH_CONVERT_5_LIMIT=43
    "sz5_cancel": ("深五档撤销", False),    # MARKET_SZ_CONVERT_5_CANCEL=47
}

def map_price_type(key: str, code: str) -> tuple[int, str | None]:
    """返回 (xtconstant_int, warning)。
    市场感知规则(参考 MQ trade-service.ts:251-287):
    - 北交所(8/4/920 开头)五档类 → 降级对手最优(44) + warning
    - 沪五档类用于非沪市 → 降级对手最优 + warning
    - 深五档撤销用于非深市 → 降级对手最优 + warning
    - 深五档转限价(43 是沪市专属;深交所不支持五档转限价) → 仅沪市可选
    """
```

市场判断(复用现有口径): 60/68 开头=SH;00/30 开头=SZ;8/4/920 开头=BJ。

#### M2: `/live/order` 端点增强(改 main.py)

- 请求体新增可选字段 `price_type_key: str`;有 key 时经 M1 映射(市场感知),无 key 时按现有 `price_type` int 透传(**向后兼容**)。
- 市价映射产生 warning 时,写入 audit + 返回给前端展示。
- **市价单金额估算(HIGH-1 修复)**: 市价类 price_type(price=0)时,risk_gate 闸门1/2/3/4 的金额估算现按 `volume×100` 兜底(risk_gate.py:73),买高价股会被低估 10 倍以上,风控形同虚设。**必须**在 M2 端点层(进 OrderExecutor 之前)先取 QMT 实时价(`qmt.get_realtime_quotes([code])` 的 lastPrice)回填 intent.price 作为估算基准;**取不到行情时市价单 fail-closed 拒绝**(返回明确原因,不放行)。限价单不受影响(price 用户必填)。
- **dry-run 行为变更**: 从"mock 回报"改为"硬拒 403"(用户确认;原 hint 文案本来就是"dry-run 禁用")。buy-signal 链路不受影响(独立端点;mock 逻辑在 executor 层,不动)。

#### M3: 撤单端点(新建 `POST /live/order/cancel`)

```python
# 请求: {"order_id": 12345}
# 逻辑:
# 1. dry-run → 403(与手工单一致)
# 2. kill_switch 激活 → 放行。理由:撤单是减风险操作,kill_switch 语义是
#    "停止开新仓/新单",撤单不在其列(注意:execute() 对买卖是全拒的,
#    不能类比普通卖出路径)
# 3. qmt.cancel_order(order_id) → 0 成功 / -1 断开 / -3 未找到
# 4. 写 audit(action="order_cancel") + 飞书通知(可选,复用 notifier)
# 返回: {"ok": bool, "order_id": int, "reason": str}
```

#### M4: 持仓同步端点(新建 `POST /live/positions/sync`)

```python
# 请求: {"code": "600000.SH"} (可选;不传=全量)
# 实现方式(已拍板,解决审计 HIGH-2):
#   给 _takeover_positions(store, qmt, config, audit) 加可选参数 code: str|None=None
#   —— 加默认参数不改现有调用点(main.py:158 / :1027),向后兼容成立
#   - upsert 保留 peak_price/sell_count/entry_date/managed(现有接管逻辑已保证,:316-318)
# 返回: {"synced": N, "codes": [...]}
# 前端: 下单成功 5s 后自动调用(带 code),结果静默刷新持仓表
```

**卖出全平语义(显式声明)**: takeover 只 upsert 不删除。手工卖光一只票后,正常路径靠 callback_handler 成交回调把本地 volume 置零;sync 只刷新数量、**不删本地行**(删除残留是 _cleanup_dryrun_residue 的职责,live 模式明确"绝不自动删除")。因此"下单后自动对齐"= 数量/价格立即刷新,已清仓行显示 volume=0,不做物理删除。

#### M5: 前端下单表单(改 static/index.html + static/js/live_trader.js)

新增折叠区"手工下单"(位于"执行开关/模式"之后):

```
[买入] [卖出]  (Tab 切换)
代码: [______] (6位数字校验)
价格类型: [限价 ▼]  (下拉,7 种,限定价才显示价格输入)
价格: [______] (limit 必填,两位小数)
数量: [______] (整数;买入按 buy_volume 规则校验)
─────────────
可用资金: ¥xxx,xxx | 预估金额: ¥xx,xxx (实时计算,超额红色提示)
[确认下单] (二次确认弹窗:完整订单信息 + 市价单风险提示)
```

- dry-run 模式:表单整体禁用 + 灰色提示"dry-run 模式手工下单禁用"
- 提交 → POST /live/order → 结果显示(成功绿/失败红) → 刷新委托列表 → 5s 后自动调 /live/positions/sync
- 委托列表:可撤单(状态为已报/部成)行加"撤单"按钮 → confirm → POST /live/order/cancel → 刷新

### 2.3 复用 MQ 的"思路"(不搬代码)

| 借鉴点 | MQ 出处 | 我们的实现 |
|---|---|---|
| 市场感知市价映射(北交所降级等坑) | trade-service.ts:251-287 | M1 price_type.py(纯 Python 重写) |
| 买入/卖出 Tab + 价格类型联动 | trade-card.vue | M5 表单(原生 JS) |
| 资产卡上下文 | trade-view.vue 组合 | M5 内嵌资金显示(复用 /live/asset) |

---

## 3. 任务分解

### Phase 1: 后端(预计 2-3h)

| # | 任务 | 产出 | 验收标准 |
|---|---|---|---|
| T1 | 新建 price_type.py 市价映射 | app/live_trader/price_type.py | 7 种键全映射;北交所五档降级对手最优+warning;单元测试覆盖 |
| T2 | /live/order 支持 price_type_key + dry-run 硬拒 + 市价单金额估算 | main.py | 无 key 时旧行为不变;dry-run 返 403;warning 透传;**市价单用 QMT 实时价估算金额过闸门,取不到行情 fail-closed 拒绝** |
| T3 | 新建 POST /live/order/cancel | main.py | 调 qmt.cancel_order;kill_switch 放行;audit 留痕;单元测试 |
| T4 | 新建 POST /live/positions/sync | main.py | 复用 takeover 逻辑;保留本地扩展字段;单元测试 |

### Phase 2: 前端(预计 3-4h)

| # | 任务 | 产出 | 验收标准 |
|---|---|---|---|
| T5 | 下单表单 UI(accordion item) | index.html | 买入/卖出 Tab;价格类型联动;禁用态(dry-run) |
| T6 | 表单逻辑(校验/确认/提交/资金) | live_trader.js | 6位代码校验;buy_volume 规则;二次确认含市价风险提示;金额实时估算;**市价下拉按代码前缀动态过滤(沪/深/北交所,R4 落地)** |
| T7 | 撤单按钮 | live_trader.js | 可撤单行显示按钮(本地 status=已报/部成判断,可选增强:渲染时用 `query_orders(cancelable_only=True)` 校准可撤集合);confirm;成功后刷新 |
| T8 | 下单后自动持仓同步 | live_trader.js | 成功 5s 后调 /live/positions/sync;持仓表刷新 |
| T9 | 缓存版本 bump(v19→v20) | index.html | — |

### Phase 3: 测试与交付(预计 1-2h)

| # | 任务 | 验收标准 |
|---|---|---|
| T10 | pytest 全套(smoke + 新增) | 全绿;新增 price_type/cancel/sync 测试 |
| T11 | node -c + py_compile | 全过 |
| T12 | 自审计(4维度) + 本计划书审计迭代 | 报告落 docs/审计报告/ |

**总计估时: 约 7-10 小时(含 HIGH-1/HIGH-2 修订后工作量,约 1~1.5 个工作日)**

---

## 4. 风险与边界

| # | 风险 | 等级 | 应对 |
|---|---|---|---|
| R1 | 市价单(对手最优/五档)可能成交在不利价格 | 中 | 二次确认弹窗对市价类单单独红字提示"市价单不保证成交价" |
| R2 | dry-run 从 mock 改硬拒是行为变更 | 低 | 已核实 tests/ 无任何测试直接打 /live/order;buy-signal 链路不受影响(mock 在 executor 层不动);计划书显式声明 |
| R3 | 撤单竞态(点撤单时已成交) | 低 | QMT 返回错误如实展示;撤单后刷新委托+持仓 |
| R4 | 北交所/跨市场市价类型误选 | 中 | 双保险:后端 M1 映射层统一降级+warning;前端 T6 下拉按代码前缀动态过滤可选项 |
| R5 | 持仓同步覆盖本地扩展字段 | 低 | 复用 takeover upsert(已验证保留 peak_price/sell_count/entry_date/pending_buy_volume/strategy_name) |
| R6 | kill_switch 激活时撤单被误拦 | 低 | M3 设计明确:撤单不走 kill_switch 拦截(减风险操作,不在"停新单"语义内) |
| R7 | 市价单 price=0 时闸门1/2/3/4 金额估算失真(volume×100 兜底,风控形同虚设) | **高** | **M2 已设计修复:市价单先用 QMT 实时价回填估算基准;取不到行情 fail-closed 拒绝。T2 验收强制覆盖** |
| R8 | 手工卖光后 sync 不删本地行,残留行显示 volume=0 | 低 | M4 已显式声明语义(只刷量不删行;回调置零为主路径;物理删除归 _cleanup_dryrun_residue) |

## 5. 兼容性声明(规则3:不破坏现有功能)

- `/live/order` 无 `price_type_key` 时行为完全不变(向后兼容)
- `_takeover_positions` **加可选参数 `code: str|None=None`**,现有两个调用点(main.py:158 / :1027)不传参、行为不变
- 现有 10 闸门、kill switch、auto_buy、buy-signal 链路零改动
- dry-run 行为变更仅限 `/live/order` 一个端点,计划书显式声明并经用户确认;mock 逻辑留在 executor 层不动,buy-signal 仍可用

## 6. 测试计划

| 类型 | 内容 |
|---|---|
| 单元 | price_type 7键映射 + 北交所降级 + 跨市场降级;cancel 端点(0/-1/-3 三分支);sync 端点字段保留 |
| 回归 | 现有 test_live_trader_smoke(27)、test_buy_signal_bridge、test_order_executor 全绿 |
| 冒烟 | dry-run 表单禁用可见;live 模式(用户择机)手工下一笔小额限价单验证全链路 |
| 前端 | node -c 语法;浏览器控制台无报错;缓存版本 bump 生效 |

## 7. 交付物

1. 代码:price_type.py + main.py 3 端点 + index.html/live_trader.js 表单与撤单
2. 测试:新增 pytest(预计 +6~8 个) + 全量回归
3. 文档:本计划书 + 审计报告(docs/审计报告/)
