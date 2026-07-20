# 计划书 v2：前端颜色硬编码全量统一 + 交易记录量列「剩余/总量」

> 日期：2026-07-20（v2 经 code-reviewer 审计迭代，审计报告见 `docs/AUDIT-PLAN-color-hardcode-2026-07-20.md`）
> 来源：今日盈亏基准价 bug 修复后的自检遗留项
> 用户决策：① 颜色**全量统一**走 token；② 交易记录持仓行量列显示**「剩余/总量」**
> v2 变更：吸收审计 11 项发现（2 HIGH / 5 MEDIUM / 4 LOW），补 C 类色板方案、D 类语义变化告知、P0 缓存与抽常量、remaining 边界、灰度归并初稿、估时等

---

## 0. 一句话目标

把前端 **126 处**硬编码颜色全部收敛到 CSS token，顺手修掉交易记录表「量」列用全部股数冒充剩余股数、导致部分卖出后今日盈亏偏高的 bug。

> 口径说明：Grep 实测 3 个文件共 127 处，其中 `live_trader.js` 那 1 处已是好实践（`getComputedStyle` 读 `--accent` 带 fallback），**本次实施 126 处**。

---

## 1. 问题一：颜色硬编码全量统一

### 1.1 现状盘点（审计 16/16 实测 PASS）

| 文件 | 硬编码处数 | 说明 |
|---|---|---|
| `static/js/main.js` | 120 | 重灾区，含 echarts 图表配色 |
| `static/js/utils.js` | 6 | 全在 `createNotification`（`#fff` + `rgba(0,0,0,0.15)` + success/info/warning/error 4 背景色）|
| `static/js/live_trader.js` | 1 | 已是好实践，**保留不动** |
| **本次实施合计** | **126** | |

**三套不一致的红绿色值**（同语义三个写法，混乱根源）：

| 语义 | 出现的硬编码 | 等于哪个 token |
|---|---|---|
| 红涨 | `#ef232a` / `#ef4444` / `#f85149` | `--up` / `--red`（= `#f85149`）|
| 绿跌 | `#14b143` / `#22c55e` / `#3fb950` | `--down` / `--green`（= `#3fb950`）|

**项目 token 齐全**（`main.css:1-55`）：表面 `--bg/-2/-3`、文字 `--text/-2/-3`、强调 `--accent/-orange/-yellow/-purple`、涨跌 `--up/-down`、回测分类 `--cat-hs/-tp1/-tp2/-tr/-tc/-tf`、派生 `--accent-soft/-line/-shadow-*`。

**已有但未被使用的工具**（`main.js:1-14`）：`cssVar(name, fallback)` 函数 + `COLOR` 对象（仅 7 个 getter：up/down/accent/text/text2/text3/bg3，**缺 yellow/orange/purple/red/green/cat-***）。

### 1.2 分类与映射方案（v2 补全 C/D）

| 类别 | 典型硬编码 | 映射目标 | 约几处 |
|---|---|---|---|
| **A 涨跌色** | `#ef232a`/`#14b143`/`#ef4444`/`#22c55e`/`#f85149`/`#3fb950` | `--up` / `--down` | ~30 |
| **B 文字灰度** | 见下方归并初稿表 | `--text`/`--text2`/`--text3` | ~45 |
| **C 图表多系列配色** | 5/7/8 色数组（见 1.2.C） | **新增 `--series-1..8`** | ~30 |
| **D 回测分类色** | `main.js:269-273` legend + `:5306` 饼图 7 类 | **扩 `--cat-*` 覆盖全部退出策略** | ~6 |
| **E 背景/边框/阴影** | `#111`/`#1a1a2e`/`#333`/`rgba(0,0,0,…)` | `--bg2/-bg3/-border/-shadow-*` | ~15 |

#### 1.2.B 灰度归并初稿（LOW-2 补）

灰度多档不能 1:1 映射三档 text token，按视觉就近归并：

| 硬编码 | 归并到 | 用途 |
|---|---|---|
| `#fff` / `#eee` / `#ddd` | `--text` | 主文字（浅）|
| `#ccc` / `#aaa` / `#999` / `#888` / `#777` | `--text2` | 次要文字（中）|
| `#666` / `#555` / `#444` / `#333` | `--text3` | 占位/禁用（深）|

> 实施时逐处确认归并是否合适，遇歧义保留原灰阶走向最近的档。

#### 1.2.C 图表多系列配色方案（HIGH-1，**已确认：方案 a 新增 --series-1..8**）

实测图表配色数组涉及 **14 种独特色**，远超现有 8 个语义 token，无法 1:1 映射。三选一：

| 方案 | 做法 | 优 | 劣 |
|---|---|---|---|
| **a（推荐）** | 新增 `--series-1..8` 共 8 个「图表系列色」token，多系列按 `i % 8` 循环取 | 完全走 token、可主题化 | 需在 main.css 加 8 个 token |
| b | echarts 多系列区分色保持硬编码（豁免清单），只统一单语义色（涨跌/分类）| 改动最小、视觉零变化 | 没彻底统一，留豁免口子 |
| c | 用现有 8 个语义色按梯度循环复用 | 不加 token | 语义色和系列色混用，歧义 |

**推荐 a**。`--series-1..8` 初稿取自现有最高频色：
`--series-1:#d29922`(黄) / `-2:#3fb950`(绿) / `-3:#58a6ff`(蓝) / `-4:#a371f7`(紫) / `-5:#f59e0b`(橙) / `-6:#ef4444`(红) / `-7:#8b5cf6`(紫2) / `-8:#3b82f6`(蓝2)。

图表实例分布：`main.js:391`(3色) / `:813/:827/:4654/:4778`(同 5 色重复 4 次) / `:4964`(5 色) / `:5115`(8 色) / `:5306`(7 类对象)。

#### 1.2.D 回测分类色方案（HIGH-2，**已确认：接受变色统一 legend**）

`main.js:269-273` legend 5 类 vs `:5306` 饼图 7 类，**同类别颜色不一致**，且饼图有 token 没定义的类别：

| 类别 | legend(:269-273) | 饼图(:5306) | 现有 token | 统一后 |
|---|---|---|---|---|
| 硬止损 | `#f85149` 红 | `#eab308` 黄 ⚠️ | `--cat-hs` 红 | 红（饼图黄→红，**变化**）|
| 止盈1/阶梯止盈 | `#d29922` 黄 | `#ef4444` 红 ⚠️ | `--cat-tp1` 黄 | 黄（饼图红→黄，**变化**）|
| 阶梯止盈2档 | — | `#dc2626` | 无 | **新增 `--cat-tp2` 已有(#3fb950) 或新值** |
| 移动止盈 | `#58a6ff` 蓝 | `#f97316` 橙 ⚠️ | `--cat-tr` 蓝 | 蓝（饼图橙→蓝，**变化**）|
| 时间止盈 | `#a371f7` 紫 | `#22c55e` 绿 ⚠️ | `--cat-tc` 紫 | 紫（饼图绿→紫，**变化**）|
| 强制退出/清仓 | `#8b949e` 灰 | `#8b5cf6` 紫 ⚠️ | `--cat-tf` 灰 | 灰（饼图紫→灰，**变化**）|
| 期末清仓 | — | `#3b82f6` | 无 | **新增 `--cat-end`** |

**已确认（用户 2026-07-20 拍板：接受变色）**：扩 `--cat-*` 覆盖全部退出策略（新增 `--cat-tp2-ladder`、`--cat-end` 等），legend 与饼图**同类别同色**（消除当前不一致），颜色以 legend 现值为准（已对齐 token）。饼图 5 处变色（上表 ⚠️ 行）用户已接受。

### 1.3 关键技术约束（v2 修正 CSS 范围声明）

CSS `var(--token)` **只在两类位置生效**：
1. CSS 规则里
2. **HTML `style` 属性 / `innerHTML` 字符串里的 style**（如 `main.js:2534/2547`）

下面这类**吃不了 `var()`**，必须用 `COLOR.xxx` / `cssVar()`：
3. **JS 直接赋值** `el.style.color = '#xxx'`（如 `main.js:2012/2960`）
4. **echarts option 的 color 字段**（如 `main.js:633/3939/5251`）

→ 第 3、4 类必须 `COLOR.up` / `cssVar('--up', '#f85149')`，**不能写 `var(--up)`**。这是最大实施坑。

**CSS 范围声明（修正 MEDIUM-3）**：CSS 主体已走 token，**少量按钮文字对比色保留硬编码是合理的**——`main.css:109/.btn-primary color:#000`(黄底黑字)、`:111/.btn-danger color:#fff`(红底白字)、`:113/.btn-success color:#000`(绿底黑字)。对比色不随 token 变，本次不动 CSS。避免「看到 hex 就是 bug」的误判。

### 1.4 utils.js 模块边界（直接定方案 a）

- `utils.js` 是 ES module（`export function`），`main.js` 是传统脚本（全局 `function`），`cssVar` 跨不了模块边界。
- **方案 a（定）**：`utils.js` 内联 3 行本地 `_cssVar`（`getComputedStyle` 读取），自给自足，零依赖、零连锁改动。
- 方案 b（抽公共 `color.js` 两边 import）需 `main.js` 改 `type="module"`，连锁大改，**否决**。

### 1.5 分阶段实施（v2 加缓存 + 抽常量 + P1 拆分 + bump 规则 + 估时）

> 严守 memory 教训：**禁止脚本批量替换前端**，每处逐个 `Edit` + `node -c`，阶段末 `bump` 缓存。
> **bump 规则（LOW-3）**：每阶段末把 `index.html:1874` 的 `main.js?v=N` 递增为 `N+1`（当前 47）。`utils.js` 经 ES module import 加载，随 `main.js` 缓存失效，无需独立 bump。

| 阶段 | 内容 | 估时 | 产出 | 风险 |
|---|---|---|---|---|
| **P0 基础设施** | ① `cssVar` 加**模块级缓存**（MEDIUM-2，首次读后缓存，主题切换罕见零风险）；② 扩 `COLOR` getter（补 yellow/orange/purple/red/green/cat-*/series-*/bg/bg2/border）；③ 抽常量 `PALETTE_5` / `trendColor(v)`（MEDIUM-1，消除 5 色数组重复 4 次 + 涨跌三元重复 8 次）；④ `utils.js` 内联 `_cssVar` | 30min | 后续阶段依赖 | 低 |
| **P1a 涨跌色·实时路径** | 实时刷新 8 处（`main.js:2012/2016/2818/2864/2927/2960/2968/3968`）→ `COLOR.up/down` | 30min | 与本次 bug 同源，最高价值 | 低 |
| **P1b 涨跌色·静态渲染** | 其余 ~22 处涨跌色（含 K 线 `:3939` up/down）| 30min | 收尾涨跌色 | 低 |
| **P2 文字灰度** | ~45 处按 1.2.B 归并表 → `COLOR.text/text2/text3` | 1h | 视觉变化最小 | 中（归并判断）|
| **P3 图表配色** | echarts 多系列 ~30 处 → `--series-1..8`（**依赖 HIGH-1 决策**）| 1.5h | echarts 需 `COLOR.seriesN` 注入 | 中 |
| **P4 分类色+背景** | D 类 6 处（**依赖 HIGH-2 决策**）+ E 类 ~15 处 | 30min | 收尾 | 低 |

每个阶段结束：`node -c main.js` + 浏览器实测一个页面 + `bump` 缓存版本。

### 1.6 风险与缓解（v2 加 D 类语义变化 + C 类色板行）

| 风险 | 缓解 |
|---|---|
| 涨跌色视觉变化（`#ef232a`→`#f85149` 红变暗等）| 用户已同意全量统一；P1a 完先出对比截图确认基调 |
| **D 类饼图 5 处变色（硬止损黄→红等）⚠️ HIGH-2** | **必须用户确认**（见 1.2.D）；不接受则该处走豁免 |
| **C 类色板 token 不够 ⚠️ HIGH-1** | 新增 `--series-1..8`（见 1.2.C），待用户选方案 |
| echarts 配色主观性 | 多系列区分色走 `--series-*`，保持视觉差异 |
| 126 处逐个 Edit 易遗漏 | 每阶段 Grep 复查该类「零残留」；阶段末 `rg '#[0-9a-fA-F]{3,6}'` 看剩多少 |
| `COLOR` getter 每次 `getComputedStyle`（reflux）| **P0 加模块级缓存**（MEDIUM-2）|
| 回滚 | 每阶段独立 commit，`git checkout` 单文件回退 |

---

## 2. 问题二：交易记录量列「剩余/总量」

### 2.1 现状（审计实测 PASS）

- 后端 `sim_trader.py:442` trades 端点 holding dict 只有 `'shares': p.shares`（总量），**无 remaining**。
- 前端 `main.js:312` `data-shares = t.shares`（总量），实时刷新用它算今日盈亏 → **部分卖出后偏高**。
- 对照：status 端点（`sim_trader.py:251`）**已返回** `'remaining'`，持仓表 `main.js:207` 已用 `(p.remaining || p.shares)` 范式——**本次改造是把该范式移植到交易记录表**。

### 2.2 改造方案（v2 修正 remaining 表达式 + 补边缘场景）

**后端**（`sim_trader.py` holding dict）：
```python
'shares': p.shares,
'remaining': p.remaining_shares if p.remaining_shares is not None else p.shares,  # is not None, 非 truthy
```
> 修正 MEDIUM-4：用 `is not None` 而非 truthy，避免 `remaining_shares == 0` 时错误回退到总量、掩盖全平仓异常。

**前端量列**（`main.js:348`）：
- 完全持仓（remaining == shares）：单值 `4100`
- 部分卖出（remaining < shares）：`2500 / 5000`
- 已平仓行：维持原样

**前端 data-shares**（`main.js:312`）：`data-shares = t.remaining || t.shares`（今日盈亏按剩余算）

**边界场景对照表（MEDIUM-5 补）**：

| 场景 | remaining | shares | 量列显示 | data-shares | 今日盈亏基准股数 |
|---|---|---|---|---|---|
| 全仓持仓 | 4100 | 4100 | `4100` | 4100 | 4100 |
| 部分卖出 | 2500 | 5000 | `2500 / 5000` | 2500 | 2500 |
| 当日买入+当日部分卖 | 2500 | 5000 | `2500 / 5000` | 2500 | 2500 |
| 已平仓 | — | 5000 | `5000`（原样）| — | — |
| 空数据（无记录）| — | — | colspan=13「暂无记录」| — | — |
| remaining==0 异常 | 0 | 5000 | `0 / 5000`（暴露异常）| 0 | 0（today_pnl=0，不冒充）|

### 2.3 工作量与风险

后端 1 处、前端 2 处，30min，低风险。向后兼容（新字段，旧前端不读不崩）。

---

## 3. 实施顺序（v2 更新）

1. **问题二**（量列，独立小改，1 commit，30min）
2. **P0 基础设施**（缓存 + 扩 COLOR + 抽常量 + utils 桥接，1 commit）
3. **P1a 涨跌色实时路径**（1 commit，完成后出对比截图给用户定基调）
4. **P1b → P2**（各 1 commit）
5. **P3 图表配色**（待 HIGH-1 决策后，1 commit）
6. **P4 分类色+背景**（待 HIGH-2 决策后，1 commit）

每 commit 前：`node -c` + `py_compile` + `pytest tests/test_sim_trader_store.py tests/test_models.py -q`。前端改动 `bump` 缓存。

---

## 4. 不做（out of scope）

- `live_trader.js`（已是好实践）
- `static/css/*.css`（主体已走 token，按钮对比色保留合理）
- `alerts.js`/`websocket.js`/`market-updater.js`（Grep 未报硬编码则不动，实施前再核）
- 后端 Python 代码（颜色只在 JS）

---

## 5. 决策记录（2026-07-20 用户全部拍板）

> 审计原列 6 项待确认，v2 关闭 4 项（utils 方案 a 已定、灰度归并表已出、P1 已拆 a/b、bump 规则已明），剩 2 个视觉决策用户已确认：

1. **[HIGH-1] C 类图表色板** → **方案 a**：新增 `--series-1..8` 共 8 个图表系列色 token
2. **[HIGH-2] D 类饼图变色** → **接受变色**：扩 `--cat-*` 统一 legend 与饼图，饼图 5 处策略色变化用户接受

**所有决策已定，可开 P0。**
