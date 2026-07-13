# XCXT xtquant POC 验证记录

> **验证日期**:2026-07-01
> **验证人**:Claude(v5.4 阶段0 POC)
> **验证对象**:新券商迅投系 XCXT,QMT 路径 `D:\Program Files\XCXT\bin.x64`,用户数据 `D:\Program Files\XCXT\userdata_mini`
> **验证脚本**:[scripts/poc_xcxt_connect.py](scripts/poc_xcxt_connect.py)
> **对应开发书**:§17.7 XCXT xtquant 版本验证清单

---

## 1. 环境

| 项 | 值 |
|----|-----|
| Python | 3.13.13(venv313) |
| xtquant 版本 | `250516`(2025-05-16) |
| xtquant 路径 | `venv313/Lib/site-packages/xtquant/` |
| cp313 pyd | `datacenter.cp313-win_amd64.pyd` 存在,**兼容 Python 3.13** ✅ |
| QMT 安装目录 | `D:\Program Files\XCXT\` |
| XtMiniQmt.exe | `D:\Program Files\XCXT\bin.x64\XtMiniQmt.exe` ✅ |
| userdata_mini | `D:\Program Files\XCXT\userdata_mini\`(35 个子文件,**可写** ✅) |
| 资金账号 | 180056133(从环境变量传入,未硬编码) |

---

## 2. 不需连 QMT 的验证(全部通过 ✅)

### 2.1 xtquant 版本与导入路径

| 验证项 | 结果 | 结论 |
|--------|------|------|
| `import xtquant` | OK | 版本 `xtquant_250516` |
| `from xtquant.xttrader import XtQuantTrader` | OK | 主交易接口 |
| `from xtquant.xttype import StockAccount` | OK | **唯一可用路径** |
| `from xtquant.xttrader import XtAccount` | **FAIL** | 此版本无此导出 |
| `getattr(xtquant, 'XtAccount')` | None | 顶层也无 |

**关键结论**:开发书 §5.1/§8 的 4 层 XtAccount 兼容降级,**第 1 层(xttype.StockAccount)即成功**,2/3/4 层用不到,MockXtAccount 兜底用不到。可简化为单一路径 + 异常兜底。

### 2.2 XtQuantTrader 构造与方法签名

| 项 | 签名 | 与开发书/MQ 对比 |
|----|------|------------------|
| `XtQuantTrader.__init__` | `(path, session, callback=None)` | ✅ 一致 |
| `order_stock` | `(account, stock_code, order_type, order_volume, price_type, price, strategy_name='', order_remark='')` | ✅ 与 §8 一致 |
| `order_stock_async` | 同 order_stock | ✅ |
| `cancel_order_stock` | `(account, order_id)` | ✅ |
| `connect` | `()` 返回 int(0=成功) | ✅ |
| `subscribe` | `(account)` 传 StockAccount 对象 | ✅ 不是 int |
| `StockAccount.__init__` | `(account_id, account_type='STOCK')` | ✅ |

### 2.3 查询方法(全部存在)

| 方法 | 用途 | 与 MQ 对比 |
|------|------|-----------|
| `query_stock_orders(account, cancelable_only=False)` | 委托查询 | ✅ MQ 同名 |
| `query_stock_trades(account)` | 成交查询 | ✅ MQ 同名 |
| `query_stock_positions(account)` | 持仓查询 | ✅ MQ 同名 |
| `query_stock_asset(account)` | 资产查询 | ✅ MQ 同名 |
| `query_position_statistics(account)` | 持仓统计(期货) | ✅ 同名 |
| `query_stock_order(account, order_id)` | 单委托查询 | 额外 |

### 2.4 Callback 方法(7+3 个)

`XtQuantTraderCallback` 的回调方法:
- ✅ 开发书 §5.3 的 7 个全在:`on_stock_order` / `on_stock_trade` / `on_order_error` / `on_cancel_error` / `on_disconnected` / `on_account_status` / `on_order_stock_async_response`
- 额外 3 个:`on_connected`(连接成功)、`on_stock_asset`(资产推送)、`on_stock_position`(持仓推送)

### 2.5 常量枚举

**price_type**(C2 固化,§7.3/§8):

| 常量 | 值 | 用途 |
|------|-----|------|
| `FIX_PRICE` | 11 | 限价单 |
| `LATEST_PRICE` | 5 | 最新价 |
| `MARKET_PEER_PRICE_FIRST` | 44 | 对手方最优(止损快成交) |
| `MARKET_SH_CONVERT_5_CANCEL` | 42 | 沪市五档即时成交剩余撤销 |
| `MARKET_SZ_CONVERT_5_CANCEL` | 47 | **深市五档即时成交剩余撤销**(开发书 §8 原只写沪市,需补深市) |
| `MARKET_BEST` | 18 | 最优价 |
| `MARKET_CANCEL_5` | 22 | 五档即时成交剩余撤销(通用) |

**order_type**(C3 方向):

| 常量 | 值 |
|------|-----|
| `STOCK_BUY` | 23 |
| `STOCK_SELL` | 24 |

**状态码**:xtconstant 中无委托状态码常量(48-57+255),印证 MQ 用硬编码是对的。`order_status` 字段是 int 类型(待有委托时最终确认,当前 0 条委托)。

---

## 3. 连真实 QMT 验证(全部通过 ✅)

**前置条件**:XtMiniQmt.exe 已启动并用柳子恒账号登录。

运行 `QMT_ACCOUNT_ID=180056133 python scripts/poc_xcxt_connect.py`:

| # | 验证项 | 结果 |
|---|--------|------|
| 1 | `xtdata.connect()` | ✅ 成功,连接 127.0.0.1:58610,版本 sp3/1.0,数据路径 `D:\Program Files\XCXT\bin.x64/../userdata_mini/datadir` |
| 2 | `XtQuantTrader` 实例化 + `register_callback` + `start` + `connect()` | ✅ `connect()` 返回 0(成功) |
| 3 | `subscribe(StockAccount("180056133","STOCK"))` | ✅ 返回 0 |
| 4 | `query_stock_asset` 资金查询 | ✅ **可用现金 ¥117.01,冻结 ¥0,持仓市值 ¥964,724.7,总资产 ¥964,841.71** |
| 5 | `query_stock_positions` 持仓查询 | ✅ **2 只持仓**:159226.SZ(486700份,均价1.3079)、159290.SZ(229200份,均价1.3671)—— 均为 ETF |
| 6 | `query_stock_orders` 委托查询 | ✅ 返回 0 条(当前无在途委托) |
| 7 | `trader.connected` | ✅ True |
| - | callback 事件(3秒等待) | 0 事件(正常,无下单/成交) |

**链路确认打通**:xtquant → 共享内存 → XtMiniQmt.exe → 券商柜台,真实查到资金和持仓。

---

## 4. 验证清单对照(§17.7)

| 清单项 | 状态 | 备注 |
|--------|------|------|
| pip show xtquant 记录版本号 | ✅ | `250516` |
| 验证导入路径 | ✅ | `xttype.StockAccount` 唯一 |
| 验证 XtQuantTrader 构造签名 | ✅ | `(path, session, callback=None)` |
| 验证 subscribe 哪种生效 | ✅ | `subscribe(StockAccount)` 对象模式 |
| 验证 order_stock 参数顺序 | ✅ | 与开发书 §8 一致 |
| 验证 callback 7 方法名 | ✅ | 全在,额外 3 个 |
| 验证 order_status 是 int 还是 str | ⚠️ 待定 | 当前 0 条委托,xtconstant 无常量,按 int 48-57 设计 |
| 验证 userdata_mini 路径可写 | ✅ | 35 子文件,可写 |
| 验证 XtMiniQmt.exe 共享内存可连 | ✅ | xtdata.connect + trader.connect 双成功 |
| 把验证结果写入文档 | ✅ | 本文件 |

---

## 5. 发现的差异与待确认问题

### 5.1 与开发书的差异(需修正)

1. **§5.1/§8 4 层 XtAccount 兼容**:实际只需第 1 层(xttype.StockAccount),可简化。MockXtAccount 兜底保留但不会触发。
2. **§8 price_type 枚举**:补深市五档 `MARKET_SZ_CONVERT_5_CANCEL=47`(原只写沪市 42);补具体数值(11/44/42/47)。
3. **§1.3 ETF 范围**:当前账户持有 2 只 ETF(159226/159290),开发书 §1.3 未明确 ETF 是否在实盘范围。exit_rules 的 T+1/涨跌停规则对 ETF 不完全适用(ETF 是 T+0,涨跌幅 10%)。**待用户确认**。

### 5.2 待用户确认(重要)

1. **账户已有持仓**:总资产 96.4 万,已有 2 只 ETF 持仓(159226/159290)。实盘启动时:
   - `LIVE_CAPITAL` 设多少?(10 万 / 50 万 / 96.4 万)
   - 已有持仓如何接管?(纳入 live_positions / 忽略 / 手动平仓后清零起步)
2. **order_status 类型**:需在有委托时最终确认 int/str(当前按 int 48-57 设计,与 MQ 一致)。

### 5.3 POC 未覆盖(阶段1 补)

- 真实下单(限价单买 100 股低价股)→ 验证 callback 全链路
- 撤单 → 验证 ack 轮询
- 跌停/涨停判断 → 需行情数据
- 这些留到阶段1 dry-run(用 dry-run mode 字段,不下真单但走完整 callback 路径)

---

## 6. 结论

**XCXT xtquant 兼容性验证通过**,可进入阶段1 dry-run。

核心确认:
- xtquant 250516 兼容 Python 3.13 ✅
- 导入路径、方法签名、callback、常量与开发书设计一致 ✅
- 真实 QMT 连接、订阅、资金/持仓/委托查询全部打通 ✅
- 账号 180056133 真实有效,总资产 96.4 万 ✅

**下一步**:解决 §5.2 两个待确认问题(账户已有持仓处理 + LIVE_CAPITAL),然后进入阶段1 dry-run 开发。

---

**⏱ 时间戳**
- 📅 当前时间:2026-07-01 19:30:00 (周三)
- 🕐 本次 POC 验证经过: 约 30m
