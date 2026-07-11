# 实盘交易界面布局优化设计

> **日期**：2026-07-11
> **范围**：`static/index.html` 的 `#tab-live-trader` 区块 + `static/js/live_trader.js` 渲染逻辑 + `static/css/main.css` 新增设计系统类
> **目标**：互联网化、干净、专业、清爽
> **分支**：fix/sim-trader-data-pollution-20260701

## 1. 背景与现状

实盘交易界面（`#tab-live-trader`，[index.html:1476-1579](static/index.html#L1476-L1579)）当前问题：

- **平铺无层次**：11 个模块纵向堆叠 + 几个 2 列 grid，无信息分级
- **KPI 缺失**：总资产 / 市值 / 可用 / 盈亏用 13px 小字混在状态栏，无大数字突出
- **状态扁平**：连接 / 模式 / KS 用「标签: 值」span 横排，无视觉重点
- **emoji 标题**：📊📋📈💹🎛🛡🔍🎬📜 不够专业
- **内联 style 泛滥**：几乎每个元素都堆 `style="..."`，未用 main.css 设计系统类
- **危险操作不突出**：Kill Switch 按钮和普通操作混在一行
- **净值曲线孤立**：300px 独占一行，上下节奏断裂

## 2. 设计决策摘要

| 维度 | 决策 | 依据 |
|---|---|---|
| 视觉方向 | **A · 终端黑金清爽化**（保留琥珀+纯黑个性，轻改） | 用户选 A；保留品牌识别度，改动半径小 |
| 核心场景 | **盯盘为主** | 用户最常看账户总额 / 持仓盈亏 / 净值走势 |
| 信息密度 | **折中**（核心常驻首屏，低频折叠） | 互联网化分级，首屏清爽 |
| 布局方案 | **② 净值为主角** | 净值曲线全宽放大，持仓表降其下 |

## 3. 信息架构

### 首屏常驻区（盯盘核心）
1. 顶栏：状态徽章 + 账号本金 + 一键 Kill Switch
2. KPI 四连：总资产 / 持仓市值 / 可用现金 / 今日盈亏
3. 净值曲线（全宽，主角）
4. 持仓表（全宽，含汇总浮盈行）
5. 委托 + 成交（2 列）

### 折叠区（低频，手风琴）
6. 执行开关 / 模式
7. 风控参数
8. 对账记录
9. 操作（离场扫描 / 扫描间隔）
10. 审计回放

## 4. 各区块详细设计

### 4.1 顶栏
- 左侧：状态徽章组（胶囊 pill 样式）
  - `● QMT 已连接`（琥珀边框，连接时）/ `未连接`（红）
  - `dry-run 模式`（橙）/ `live 模式`（红，加粗警示）
  - `Kill Switch 未激活`（绿）/ `已激活`（红，闪烁）
- 徽章后：`账号 {account_id} · 本金 ¥{live_capital}`（次要文字 text2）
- 右侧：`一键 Kill Switch` 按钮（红底白字，固定常驻可见）
- 数据源：`/live/status`（qmt_connected, mode, account_id, live_capital, kill_switch.activated）

### 4.2 KPI 四连
四等分 grid，每卡：标签（11px 灰）+ 大数字（22px 等宽）+ 副指标（10px）

| KPI | 数值颜色 | 副指标 | 数据源 |
|---|---|---|---|
| 总资产 | 琥珀 | ▲ +x% / +¥x（vs 本金） | `/live/asset.total_asset` − `/live/status.live_capital` |
| 持仓市值 | 白 | N 只 · 仓位 x% | `/live/asset.market_value` |
| 可用现金 | 白 | 冻结 ¥{frozen_cash} | `/live/asset.cash`, `frozen_cash` |
| 今日盈亏 | 红涨绿跌 | ▲ +x% 今日 | 见 §9.1 依赖 |

### 4.3 净值曲线（主角）
- 全宽，高度 150–180px（比现状 300px 略减，为首屏留位给 KPI + 持仓）
- 右上角：时间范围切换器 `1日 / 5日 / 30日 / 全部`（分段按钮，当前高亮琥珀）
- 默认 `1日`（当日 5min 快照）
- 曲线：琥珀线 + 渐变面积填充 + 末端圆点
- 网格：横向虚线 3 条
- X 轴：时间刻度（09:35 / 10:30 / 11:30 / 13:30 / 14:30 / 15:00）
- 数据源：`/live/equity?days={1|5|30|365}`（后端已支持 days 参数，零改动）

### 4.4 持仓表（全宽）
- 表头：代码 / 股数 / 可卖 / 均价 / 现价 / 市值 / 浮盈 / 类型
- 数字列右对齐，等宽字体（`--font-num`）
- 浮盈：红涨绿跌
- 类型：「策略」（绿）/「ETF保留」（灰）
- 表头右侧：`汇总浮盈 +¥x`（所有持仓 `float_profit` 求和）
- 数据源：`/live/positions`

### 4.5 委托 + 成交（2 列）
- 左：委托（时间 / 代码 / 方向 / 价·量 / 状态 / 模式）
- 右：成交（时间 / 代码 / 方向 / 成交价·量 / 模式）
- 紧凑表格，11px
- 数据源：`/live/orders?limit=50`、`/live/deals?limit=50`

### 4.6 折叠区（手风琴）
- 容器：单个 card，内部分 5 个折叠项
- 每项：header（▸ 标题 + 右侧状态摘要）+ 可展开 body
- 默认全收起
- 展开后内容沿用现有逻辑（开关切换 / 风控参数展示 / 对账 / 操作 / 回放）
- 各项数据源与现状一致（switches / mode / risk-params / reconcile / exit-scan / scan-interval / audit-replay）

## 5. 视觉与设计系统

**沿用现有 main.css token，不新建设计系统**：
- 底色：`--bg #0a0e14` / `--bg2 #11161f` / `--bg3 #1a212e`
- 边框：`--border #1f2733` / `--border-hi #2a3441`
- 文字：`--text #e6edf3` / `--text2 #7d8590`
- 强调：`--accent #f0b429`（琥珀，灵魂色）
- 涨跌：`--up/--red #f85149` / `--down/--green #3fb950`（红涨绿跌）
- 圆角：`--radius 8px` / `--radius-sm 6px`
- 字体：`--font-ui`（Inter）/ `--font-num`（JetBrains Mono）

**新增 CSS 类**（main.css，前缀 `.lt-` 避免冲突）：
- `.lt-topbar` 顶栏 flex 容器
- `.lt-badge` 状态徽章 pill + 变体 `.lt-badge--ok/--warn/--danger`
- `.lt-kpi-grid` KPI 四连 grid
- `.lt-kpi` 单个 KPI 卡 + `.lt-kpi__label/__value/__sub`
- `.lt-chart-toolbar` 净值切换器容器
- `.lt-range-btn` / `.lt-range-btn--active` 时间范围按钮
- `.lt-accordion` 折叠区容器 + `.lt-accordion__item/__header/__body`
- `.lt-btn-kill` 一键 KS 按钮

## 6. 内联 style 清理策略

将 `#tab-live-trader` 区块内所有 `style="..."` 收进上述 CSS 类：

- `display:flex;gap:24px` → `.lt-topbar`
- `font-size:13px;padding:8px 0;border-bottom:1px solid var(--border)` → 类
- 表格内联 `font-size:12px;width:100%` → `.lt-table`（或复用 `.data-table`）
- 按钮 `padding:4px 12px` → `.btn-sm`（已有）

**原则**：复用已有类优先（`.card` / `.data-table` / `.btn` / `.stat-card`），不够再新增。

## 7. 向后兼容（不破坏现有功能）

- **HTML 结构重写 `#tab-live-trader`**（1476-1579 行），**保留所有元素 `id`**（`live-conn` / `live-mode` / `live-positions-tbody` 等），确保 `live_trader.js` 的 `getElementById` 不失效
- **JS 渲染逻辑微调**（不重写）：
  - `loadLiveStatus`：状态徽章渲染（class 切换替代内联 color）
  - `loadLiveAsset`：KPI 卡填充
  - `loadLivePositions`：汇总浮盈行
  - `loadLiveEquity`：时间范围切换器（新增 days 切换逻辑）
  - 手风琴展开 / 收起交互（新增）
- **不动其他 tab**：改动仅限 `#tab-live-trader` + `live_trader.js` + `main.css` 新增类
- **不动后端**：除 §9.1 依赖项（positions 补 last_close，纯新增字段）
- **保留所有功能**：开关 / 风控 / 对账 / 操作 / 回放全部保留，仅收纳进折叠区

## 8. 改动文件清单

| 文件 | 改动 |
|---|---|
| `static/index.html` | 重写 `#tab-live-trader`（1476-1579），去 emoji，去内联 style，用 `.lt-*` 类 |
| `static/js/live_trader.js` | 渲染逻辑适配新结构 + 净值切换器 + 手风琴交互 + KPI / 汇总浮盈 |
| `static/css/main.css` | 新增 `.lt-*` 设计系统类 |

## 9. 实现注意点与依赖

### 9.1 今日盈亏（KPI 第 4 项）✅ 已升级（2026-07-11）
- 口径（[CLAUDE.md](CLAUDE.md)）：当日买入 = (现价−买入价)×股数；过夜 = (现价−昨收)×股数
- **已实现**：
  - 后端 `/live/positions` 补 `last_close`（refresh_quotes 从 QMT lastClose 存）+ `today_buy_volume`（从 live_deals 算今日买入量）
  - 前端 `loadLivePositions` 按 `today_buy_volume` 拆分：今日买入部分按 avg_cost、过夜部分按 last_close
  - 混合持仓（昨日买+今日加仓）正确拆分（审计 C1 修复）
  - 缺昨收的过夜部分不计入 + title 悬停提示哪只
- ~~降级方案（总浮盈）已废弃~~，现为完整今日盈亏

### 9.2 净值多日切换
- 前端切换器调 `/live/equity?days={1|5|30|365}`
- 多日数据点密集时，X 轴刻度自适应（按日聚合或抽样）

### 9.3 手风琴交互
- 纯 JS/CSS 实现，点击 header 切换 body 展开 / 收起
- 展开 / 收起用 CSS `max-height` 过渡，避免布局抖动

### 9.4 响应式
- 首屏常驻区在 < 1200px 时：KPI 四连 → 两行两列；委托 + 成交 → 单列堆叠
- 折叠区天然适配窄屏

## 10. 默认假设与可调点

| 假设 | 可调 |
|---|---|
| KPI 第 4 = 今日盈亏 | 可换总浮盈 / 今日 + 总浮盈都放 |
| 净值默认 1 日 | 可改默认 5 日 |
| 折叠区默认全收 | 可默认展开某项（如执行开关） |
| 净值高度 150–180px | 可调 |
| 去 emoji 换 SVG 图标 | 可改为保留 emoji（需同步调整 §11 验收标准） |
| 手风琴折叠 | 可换子 tab |

## 11. 验收标准

- [ ] 首屏从上到下：顶栏 → KPI 四连 → 净值曲线 → 持仓表 → 委托+成交，无横向滚动
- [ ] 5 项低频模块收纳进手风琴折叠区，默认收起，点击展开
- [ ] 净值曲线支持 1日/5日/30日/全部 切换，切换后数据正确
- [x] KPI 四连大数字清晰，今日盈亏按口径正确（含混合持仓 today_buy_volume 拆分）
- [ ] 一键 Kill Switch 按钮固定顶栏右侧，红色醒目
- [ ] `#tab-live-trader` 内无 `style="..."` 内联样式（除动态值外）
- [ ] 标题无 emoji（改 SVG 图标或纯文字）
- [ ] 所有元素 `id` 保留，`live_trader.js` 现有函数无报错
- [ ] 其他 tab（回测/选股/工厂等）不受影响
- [ ] 浏览器控制台无 JS 错误
