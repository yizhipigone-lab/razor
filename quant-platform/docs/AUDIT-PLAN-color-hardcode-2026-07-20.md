# 审计报告：前端颜色硬编码统一 + 交易记录量列改造计划书

> 审计对象: docs/PLAN-color-hardcode-unify-and-trade-shares-2026-07-20.md
> 审计日期: 2026-07-20
> 审计类型: 计划书质量审计（非代码审计）
> 审计方法: Read/Grep 实测代码逐项核对计划书关键断言，禁止推断
> 审计员: code-reviewer agent

---

## 0. 总体结论

**WARNING — 可进入实施，但有 2 个 HIGH 问题必须先解决**

计划书的事实基础扎实（120/6/127 处硬编码、token 齐全、holding dict 缺 remaining、COLOR getter 不全 等关键断言全部实测确认）。但有两个 HIGH 级设计盲区会让 P3/P4 阶段直接撞墙：① C 类图表配色实际有 5/7/8 色数组，现有 token 不够覆盖；② D 类映射会把"硬止损"从黄变红、"强制清仓"从紫变灰，是较大语义视觉变化，风险章节没提。

建议：补齐 C 类色板方案 + D 类视觉变化告知用户确认后，再开 P0。

---

## 1. 关键断言核对（PASS 清单）

| # | 计划书断言 | 实测结果 | 判定 |
|---|---|---|---|
| 1 | main.js 硬编码颜色 120 处 | Grep 得 120 处 | PASS |
| 2 | utils.js 6 处 | utils.js:81 color #fff + :83 rgba(0,0,0,0.15) + :91-94 bgColors 4 个 = 6 处 | PASS |
| 3 | live_trader.js 1 处且是好实践 | live_trader.js:349 getComputedStyle 读 --accent 带 #f0b429 fallback | PASS |
| 4 | main.css token 齐全含 --cat-* | main.css:3-51 覆盖 bg/bg2/bg3/border/text(1/2/3)/accent/up/down/red/green/yellow/purple/orange/cat-hs..tf/shadow-*/accent-soft 全在 | PASS |
| 5 | main.js:1-14 COLOR getter 不全 | main.js:6-14 实测仅 7 个 getter：up/down/accent/text/text2/text3/bg3（缺 yellow/orange/purple/red/green/cat-*）| PASS |
| 6 | utils.js 是 ES module | utils.js:11/21/32/41/68 全是 export function | PASS |
| 7 | main.js:2960 用 #ef232a/#14b143 | main.js:2960 const tpColor = todayPnl >= 0 ? '#ef232a' : '#14b143' + :2968 dcColor 同款 | PASS |
| 8 | main.js:2012 是 el.style.color = #xxx | main.js:2012 pEl.style.color = q.change_pct >= 0 ? '#ef232a' : '#14b143' + :2016 同款 | PASS |
| 9 | main.js:269-273 回测分类色硬编码 | main.js:269-273 HS/TP1/TR/TC/TF 5 个 hex（#f85149/#d29922/#58a6ff/#a371f7/#8b949e）| PASS |
| 10 | main.js:633 是 echarts option color | main.js:633 itemStyle color s.profit>=0 ? #ef4444 : #22c55e | PASS |
| 11 | trades 端点 holding dict 无 remaining | sim_trader.py:442 只有 shares: p.shares，dict 至 :452 结束，无 remaining | PASS |
| 12 | status 端点 positions dict 有 remaining | sim_trader.py:251 remaining: p.remaining_shares | PASS |
| 13 | main.js:312 trades data-shares 用总量 | main.js:312 data-shares = shares（其中 shares = t.shares 来自 :306）| PASS |
| 14 | main.js:348 量列显示总量 | main.js:348 td 内容是 shares 变量 | PASS |
| 15 | main.js:2534/2547 innerHTML 字符串 style | main.js:2534 模板字符串含 style background rgba(255,255,255,0.08) color #eee + :2547 button style | PASS |
| 16 | 持仓表 data-shares 已用 remaining 兜底 | main.js:207 data-shares 用 (p.remaining || p.shares || 0) —— 持仓表路径已经是正确范式，计划书 2.2 的前端改造其实是把这个范式移植到交易记录表 | PASS（附注：计划书未指出该范式已存在，可借鉴）|

**事实层 16/16 全部 PASS** —— 计划书的现状盘点可信度高，可放心基于其推进。

---

## 2. FAIL 项（CRITICAL / HIGH）

### [HIGH-1] C 类图表配色色板覆盖不全，现有 token 不够用

**File**: static/js/main.js:391, 813, 827, 4654, 4778, 4964, 5115, 5306

**Issue**: 计划书 1.2 节 C 类只列了 5 种 hex 简单映射到 --yellow/--cat-tr/--cat-tc/--accent/--orange，但实测图表配色数组有多种规模，色板远超现有 token：

- main.js:391  idxColors = 3 色 (#ef4444/#f97316/#22c55e)
- main.js:813/827/4654/4778  colors = 5 色数组完全相同重复 4 次 (#d29922/#3fb950/#58a6ff/#a371f7/#f59e0b)
- main.js:4964  colors = 5 色 (与上面不同: #f59e0b/#ef4444/#8b5cf6/#22c55e/#3b82f6)
- main.js:5115  idxColors = 8 色 (#ef4444/#f97316/#eab308/#22c55e/#3b82f6/#8b5cf6/#ec4899/#06b6d4)
- main.js:5306  colors = 7 类对象 (阶梯止盈 #ef4444 / 阶梯止盈2档 #dc2626 / 移动止盈 #f97316 / 时间止盈 #22c55e / 硬止损 #eab308 / 强制清仓 #8b5cf6 / 期末清仓 #3b82f6)

涉及到的独特色共 14 种：#ef4444 #f97316 #22c55e #d29922 #3fb950 #58a6ff #a371f7 #f59e0b #8b5cf6 #3b82f6 #eab308 #ec4899 #06b6d4 #dc2626。

现有 token（--up/--down/--red/--green/--yellow/--orange/--purple/--accent 8 个语义色 + 6 个 --cat-*）**无法 1:1 覆盖**。计划书 C 类"约 30 处映射到 cat-*/accent"是低估。

**Fix**: 计划书 1.2 节 C 类必须补一个明确的色板方案，三选一：
- (a) 新增 --series-1 到 --series-8 共 8 个"图表系列色" token，统一作为多系列区分色板；
- (b) 明确允许 echarts 多系列区分色保持硬编码（豁免清单），只统一单语义色（涨跌/分类）；
- (c) 用现有 8 个语义色按梯度循环复用，明确给出"图表系列 i -> token[i % 8]"映射。

不补这个方案，P3 阶段会直接卡住。

---

### [HIGH-2] D 类映射会引入较大语义视觉变化，风险章节未提示

**File**: static/js/main.js:269-273, 5306  对照  static/css/main.css:25-30

**Issue**: 计划书 1.2 D 类说"回测分类色（硬止损/止盈/移动止盈等）-> --cat-hs/--cat-tp1/..."。但实际硬编码与 token **语义颜色不一致**：

| 类别 | 现硬编码 | 目标 token | token 值 | 视觉变化 |
|---|---|---|---|---|
| 硬止损 HS | #f85149 红（:269）| --cat-hs | #f85149 红 | 无 |
| 硬止损 HS（饼图 :5306）| #eab308 黄 | --cat-hs | #f85149 红 | **黄->红** |
| 止盈 TP1 | #d29922 黄（:270）| --cat-tp1 | #d29922 黄 | 无 |
| 移动止盈 TR | #58a6ff 蓝（:271）| --cat-tr | #58a6ff 蓝 | 无 |
| 时间退出 TC | #a371f7 紫（:272）| --cat-tc | #a371f7 紫 | 无 |
| 强制退出 TF | #8b949e 灰（:273）| --cat-tf | #8b949e 灰 | 无 |
| 强制清仓（饼图 :5306）| #8b5cf6 紫 | --cat-tf | #8b949e 灰 | **紫->灰** |
| 阶梯止盈（饼图）| #ef4444 红 | （无对应 token）| - | 待定 |
| 期末清仓（饼图）| #3b82f6 蓝 | （无对应 token）| - | 待定 |

**main.js:5306 饼图有 7 类退出策略，但 CSS 只有 6 个 --cat-* token**，且语义对不齐（饼图有"阶梯止盈 2 档/期末清仓"等 token 没定义的类别）。

风险章节（1.6）只提了"#ef232a -> #f85149 红色变暗"一种视觉变化，**完全没提分类色语义变化**。计划书第 5 节"待确认项"第 1 条把它推回给审计——审计结论是：**必须先与用户确认分类色语义映射，否则 P4 完成后用户会惊讶"硬止损怎么变红了"**。

**Fix**:
1. 计划书 1.6 风险表新增一行："D 类分类色：main.js:5306 饼图与 main.css:25-30 token 语义不一致，硬止损黄->红、强制清仓紫->灰，需用户确认预期视觉"；
2. 1.2 D 类补充：是否需要扩展 --cat-* 到 8 类（新增"阶梯止盈/期末清仓"）。

---

## 3. WARNING 项（MEDIUM / LOW）

### [MEDIUM-1] DRY 违反：5 色数组重复 4 次 + 涨跌色三元重复 8 次，计划书未提抽常量

**File**: main.js:813, 827, 4654, 4778（5 色数组完全相同）；main.js:2012, 2016, 2818, 2864, 2927, 2960, 2968, 3968（"大于等于 0 ? #ef232a : #14b143" 重复 8 次）

**Issue**: 计划书目标是"全量统一"，但没指出这是 DRY 违反的优化机会。若不抽常量，P1-P4 阶段会重复写 COLOR.up/COLOR.down 8 次、5 色数组 4 次。

**Fix**: P0 阶段除了扩 COLOR getter，再加 2 个常量：
- const PALETTE_5 = [COLOR.yellow, COLOR.down, COLOR.catTr, COLOR.purple, COLOR.orange];
- const trendColor = (v) => v >= 0 ? COLOR.up : COLOR.down;

120 处中至少 12 处可压缩到 2 行，整体工作量降一档。

---

### [MEDIUM-2] COLOR getter 性能风险被低估，应 P0 就加缓存

**File**: 计划书 1.6 性能行 + main.js:6-14

**Issue**: 计划书说"高频 echarts 场景若卡顿再加缓存（本次先不做）"。但 getComputedStyle(document.documentElement) 是**同步、可能触发 reflow** 的调用。echarts option 构建时密集访问（如 main.js:5115 8 色数组会调 8 次 + tooltip/legend 各色再调）一次图表渲染可能 20+ 次 getComputedStyle。多个图表同屏（dashboard 页）会叠加。

**Fix**: P0 就加模块级缓存：cssVar 函数内维护 _cache 字典，首次读取后缓存；主题切换在本项目罕见（无暗/亮切换），缓存基本永不失效，零风险。可选：监听 window theme-change 事件清缓存。

---

### [MEDIUM-3] CSS 文件也有零星硬编码，与"CSS 已全走 token 不用动"说法不符

**File**: static/css/main.css:103, 109, 111, 113

**Issue**: 计划书 1.3 节断言"本项目 CSS 已全走 token，不用动"。实测：
- :103 input:disabled ... border-color: rgba(255,255,255,0.1)
- :109 .btn-primary { background: var(--accent); color: #000; }
- :111 .btn-danger { background: var(--red); color: #fff; }
- :113 .btn-success { background: var(--green); color: #000; }

这些是按钮文字对比色（黄底黑字、红底白字、绿底黑字），**保留硬编码是合理的**（对比色不需要随 token 变），但计划书的"已全走 token"是过度断言。

**Fix**: 1.3 节改为"CSS 主体已走 token，少量按钮文字对比色（main.css:109/111/113 的 color #000/#fff）保留硬编码，合理无需动"。范围声明更准确，避免实施时误以为"只要看到 hex 就是 bug"。

---

### [MEDIUM-4] 后端 remaining 表达式有 0 值回退隐患

**File**: 计划书 2.2 节代码块（复制自 sim_trader.py:427）

**Issue**: 计划书给的代码 "remaining: p.remaining_shares if p.remaining_shares else p.shares" 是 falsy 判断，当 remaining_shares == 0（理论上持仓应已转移，但防御性考虑）会回退到 p.shares，前端会显示 "5000/5000" 而非 "0/5000"，掩盖全平仓的异常状态。

**Fix**: 改为显式 None 判断：
- remaining: p.remaining_shares if p.remaining_shares is not None else p.shares

或更严格：remaining == 0 时不该出现在 holding dict（应在已平仓），加一行 assert/log。

---

### [MEDIUM-5] 边缘场景覆盖不完整：空数据态 / remaining==0 / 图表无数据态

**File**: 计划书 2.2 节

**Issue**: 完整性检查发现以下场景计划书没明确：
1. **空数据态**：trades 表无记录时 main.js:359 显示"暂无记录" colspan=13（已验证），新加 remaining 字段后是否影响 colspan？——不影响（remaining 是数据字段，非列）。
2. **remaining==0**：见 [MEDIUM-4]。
3. **图表无数据态**：main.js:3782 等图表在 sortedMonths=[] 时颜色数组仍硬编码，P3 改造时需确认空态不崩。
4. **当日买入 + 当日部分卖出**：bought_today=True + remaining < shares，data-shares 应用 remaining（逻辑正确，但计划书没举例）。

**Fix**: 2.2 节加一个"边界场景对照表"，列出 5 种场景（全仓/部分卖出/已平仓/当日买入部分卖出/空数据）的预期显示。

---

### [LOW-1] "126 处" vs "127 合计" 前后不一致

**File**: 计划书第 0 节 vs 1.1 节表格合计

**Issue**: 第 0 节"把前端 126 处硬编码颜色全部收敛"，1.1 节表格合计 127（含 live_trader 1 处），又说"live_trader 那 1 处不纳入本次"。数学上是 126，但口径不统一会让实施时困惑。

**Fix**: 全文统一用 126，1.1 表格"合计 127"改为"合计 127（本次实施 126，live_trader 1 处已是好实践保留）"。

---

### [LOW-2] 灰度归并对照表是 P2 硬依赖，但承诺"实施前出"且第 5 节又推回审计

**File**: 计划书 1.2 末段 + 第 5 节待确认项第 1 条

**Issue**: 1.2 说"归并方案在实施前出一版对照表，逐处确认"；第 5 节又把"灰度归并方案是否合理"列为待审计确认项。审计无法替你出对照表（要逐处看视觉）。

**Fix**: 计划书就给出初稿，例如：
- #fff/#eee/#ddd -> --text（浅，主文字）
- #ccc/#aaa/#999/#888/#777 -> --text2（中，次要文字）
- #666/#555/#444/#333 -> --text3（深，占位/禁用）

审计可以判断这个归并是否合理，但出题要计划书自己出。

---

### [LOW-3] 缓存 bump 策略未明确，5 个阶段 5 次 bump 版本号规则缺失

**File**: 计划书 1.5 + 第 3 节；static/index.html:1874（当前 main.js?v=47）

**Issue**: 当前版本号 47，5 个阶段各 bump 一次 -> 48/49/50/51/52。但：
- 没说每次 bump 是改 index.html 哪些标签（只 main.js 还是 utils.js 也要 bump？）
- utils.js 在 index.html 是否独立 script 标签？实测 grep "utils.js?v=" 在 index.html **未匹配到**——utils.js 可能通过 import 引用，不需独立 bump，但需确认
- 没说版本号规则（递增数字 vs 日期）

**Fix**: 1.5 节加一句"每次阶段末把 index.html:1874 的 main.js?v=N 递增为 N+1；utils.js 通过 ES module import 加载，浏览器自动跟随 main.js 缓存失效"。

---

### [LOW-4] 工作量估计缺失，126 处逐个 Edit 无时间预算

**File**: 计划书第 5 节待确认项第 6 条

**Issue**: 126 处逐个 Edit + node -c，按每处 30-60 秒估，纯机械工时 1-2 小时，加 echarts 配色判断（C/D 类需逐处思考）实际可能 3-4 小时。计划书没估，无法跟用户预期对齐。

**Fix**: 各阶段加估时列：P0 30min / P1 1h / P2 1h / P3 1.5h / P4 30min + 问题二 30min 约 5h。

---

## 4. 各维度评估

### 4.1 完整性 — PASS（带 1 个 MEDIUM）
- 两个问题方案全覆盖
- 问题二边界场景基本覆盖（已平仓/部分卖出/当日买入），缺 remaining==0 + 空数据态显式说明（[MEDIUM-5]）
- 问题一 5 类映射框架合理，但 C/D 两类有覆盖盲区（[HIGH-1]/[HIGH-2]）

### 4.2 一致性 — PASS
- 与 CLAUDE.md 一致：禁止硬编码、禁止脚本批量改前端（1.5 节明令"每处逐个 Edit"）、改 CSS 要 bump 缓存
- 内部前后基本自洽，仅 [LOW-1] 126/127 数字口径不统一
- 与既有范式一致：main.js:207 持仓表已用 (p.remaining || p.shares)，问题二改造与之对齐

### 4.3 可行性 — PASS（核心判断准确）
- var(--token) 只在 CSS/HTML style 生效、JS/echarts 必须用 cssVar/COLOR —— 判断准确（实测 main.js:2012/2960 验证）
- utils.js ES module 边界问题真实存在
- 方案 a（utils 内联 3 行 cssVar）零依赖、可行
- 方案 b（main.js 改 type="module"）确实是连锁大改
- **建议直接定方案 a**（第 5 节待确认项第 3 条可关闭）

### 4.4 风险识别 — FAIL（漏 2 个 HIGH）
- 已识别：视觉变化 / echarts 主观性 / 126 处易遗漏 / 性能 / 回滚 —— 5 项
- **漏识别**：C 类色板不足（[HIGH-1]）、D 类语义色变化（[HIGH-2]）、CSS 残留硬编码误判（[MEDIUM-3]）、DRY 优化机会（[MEDIUM-1]）
- 风险清单深度不够，是本计划书最大短板

### 4.5 优先级 — PASS（带 1 个 LOW）
- "先问题二再问题一 P0-P4"顺序合理（问题二小且独立，立竿见影）
- P1 涨跌色 30 处 1 commit 略大，可再拆 P1a（实时刷新路径 8 处）/P1b（静态渲染 22 处）—— 这是第 5 节待确认项第 4 条，**建议拆**
- P3 图表配色 + P4 分类色边界清晰

### 4.6 可测试性 — PASS（带 1 个 MEDIUM）
- node -c / py_compile / pytest 三件套
- pytest 回归用 tests/test_sim_trader_store.py + tests/test_models.py（两文件均存在）
- **缺**：无前端单测/视觉回归。颜色改动靠浏览器肉眼，**P1 完成出对比截图**（计划书已提）是必要缓解
- 可加：用项目已有的 playwright-cli 做 dashboard 截图对比（自动化视觉回归）

---

## 5. 改进建议（按优先级）

### 必做（进实施前）
1. **[HIGH-1]** 补 C 类色板方案（推荐新增 --series-1..8 token）
2. **[HIGH-2]** 风险表加 D 类视觉变化告知，与用户确认硬止损黄->红、强制清仓紫->灰是否符合预期
3. **[MEDIUM-2]** P0 加 COLOR getter 模块级缓存

### 建议做（提升质量）
4. **[MEDIUM-1]** P0 抽 PALETTE_5 / trendColor(v) 常量，降工作量
5. **[MEDIUM-3]** 1.3 节范围声明改准（CSS 主体走 token，按钮对比色保留硬编码合理）
6. **[MEDIUM-4]** 后端 remaining 用 "is not None" 而非 truthy
7. **[LOW-2]** 1.2 节给出灰度归并初稿对照表

### 可选（打磨）
8. **[LOW-1]** 全文统一 126 口径
9. **[LOW-3]** 缓存 bump 规则写明（index.html:1874 main.js?v=N -> N+1）
10. **[LOW-4]** 各阶段加估时
11. P1 拆为 P1a/P1b
12. 关闭第 5 节待确认项第 3 条（直接定方案 a）

---

## 6. 摘要

| 严重级 | 数量 | 主要项 |
|---|---|---|
| CRITICAL | 0 | - |
| HIGH | 2 | C 类色板覆盖不足（[HIGH-1]）、D 类语义色变化未提示（[HIGH-2]）|
| MEDIUM | 5 | DRY 抽常量、getter 缓存、CSS 范围声明、remaining 0 值、边缘场景 |
| LOW | 4 | 数字口径、归并对照表、bump 规则、估时 |

**事实基础**：16/16 关键断言实测 PASS，计划书现状盘点可信。

**结论**：**WARNING** —— 事实扎实、可行性判断准确、优先级合理，但风险识别漏了 2 个 HIGH（C/D 类色板/语义问题），不补就进 P3/P4 会撞墙。补齐 [HIGH-1]+[HIGH-2]+[MEDIUM-2] 三项后，可进入实施。建议先把改进建议"必做"3 项迭代进计划书，再开 P0。
