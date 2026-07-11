# 实盘交易界面布局优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `#tab-live-trader` 实盘交易界面从「11 模块平铺 + 内联 style + emoji」重构成「顶栏徽章 + KPI 四连 + 净值主角 + 持仓 + 委托成交 + 手风琴折叠区」的互联网化清爽布局，保留终端黑金视觉个性。

**Architecture:** 纯前端重构。沿用现有 main.css 设计 token 与工具类，新增少量 `.lt-*` 类（徽章/手风琴/KPI）。HTML 整段重写 `#tab-live-trader`（保留所有元素 `id` 以兼容 `live_trader.js`），JS 函数微调渲染逻辑（徽章 class 切换、KPI 填充、汇总浮盈、净值 days 切换、手风琴交互）。后端零改动（今日盈亏第一阶段降级为总浮盈）。

**Tech Stack:** 原生 HTML/CSS/JS（无框架）、ECharts（净值图）、现有 SVG sprite 图标、main.css 设计系统（CSS 变量 + 工具类）。

**Spec:** [docs/superpowers/specs/2026-07-11-live-trader-layout-redesign-design.md](docs/superpowers/specs/2026-07-11-live-trader-layout-redesign-design.md)

---

## File Structure

| 文件 | 责任 | 改动类型 |
|---|---|---|
| `static/css/main.css` | 新增 `.lt-*` 类（徽章/顶栏/KPI/手风琴/KS按钮/响应式） | 追加（末尾） |
| `static/index.html` | 重写 `#tab-live-trader`（1476-1579 行）+ 升 CSS/JS 版本号 | 替换整段 |
| `static/js/live_trader.js` | 5 个函数适配 + 1 个新手风琴函数 | 修改函数体 |

**复用优先**（不重复造轮子）：
- KPI 卡 → 复用思路参考 `.stat-card`，但用 `.lt-kpi`（需要 4 连 grid + 副指标）
- 时间范围按钮 → 复用 `.chip` + `.chip.active`
- KS 按钮 → 复用 `.btn .btn-danger`
- 工具类 → `.flex-between` / `.flex-gap` / `.muted` / `.tc-accent` / `.tc-red` / `.tc-green` / `.fs-xs` / `.mb-2` / `.ta-r`
- 图标 → 复用 SVG sprite（`#i-candle` / `#i-trend` / `#i-settings` / `#i-radio` / `#i-file`）

---

## Task 1: main.css 新增 `.lt-*` 设计系统类

**Files:**
- Modify: `static/css/main.css`（末尾追加，约 466 行后）
- Modify: `static/index.html:34`（`main.css?v=7` → `?v=8`）

- [ ] **Step 1: 在 main.css 末尾追加 .lt-* 类**

打开 `static/css/main.css`，在文件末尾（第 466 行 `.nav-divider` 之后）追加：

```css

/* ═══ 实盘交易界面 (.lt-*) ═══ */
/* 状态徽章 pill */
.lt-badge { display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px; border-radius: 99px; font-size: 11px; font-weight: 500; background: var(--bg3); color: var(--text2); border: 1px solid var(--border); }
.lt-badge--ok { color: var(--accent); border-color: var(--accent); }
.lt-badge--warn { color: var(--orange); }
.lt-badge--danger { color: var(--red); border-color: var(--red); }
.lt-badge--success { color: var(--green); }
.lt-badge-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex-shrink: 0; }

/* 顶栏 */
.lt-topbar { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.lt-topbar-badges { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.lt-topbar-meta { color: var(--text2); font-size: 11px; padding-left: 6px; }
.lt-topbar-meta b { color: var(--text); font-weight: 600; }

/* KPI 四连 */
.lt-kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 12px; }
.lt-kpi { background: var(--surface-1); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 14px; }
.lt-kpi__label { color: var(--text2); font-size: 11px; margin-bottom: 4px; }
.lt-kpi__value { font-size: 22px; font-weight: 700; font-family: var(--font-num); letter-spacing: -0.5px; }
.lt-kpi__value--accent { color: var(--accent); }
.lt-kpi__sub { font-size: 10px; margin-top: 2px; }

/* 净值工具栏（复用 .chip 做范围按钮，容器只做排版） */
.lt-chart-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.lt-chart-title { display: flex; align-items: center; gap: 8px; }
.lt-chart-range { display: flex; gap: 2px; background: var(--bg); border: 1px solid var(--line); border-radius: var(--radius-sm); padding: 2px; }
.lt-chart-range .chip { padding: 3px 10px; min-width: auto; font-size: 11px; border: none; box-shadow: none; }
.lt-chart-range .chip:hover { transform: none; box-shadow: none; }

/* 手风琴 */
.lt-accordion { background: var(--surface-1); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; margin-bottom: var(--gap); }
.lt-accordion__title { background: var(--bg); padding: 10px 14px; border-bottom: 1px solid var(--line); color: var(--text2); font-size: 11px; }
.lt-accordion__item { border-bottom: 1px solid var(--border); }
.lt-accordion__item:last-child { border-bottom: none; }
.lt-accordion__header { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px; cursor: pointer; font-size: 13px; color: var(--text); transition: background .15s; }
.lt-accordion__header:hover { background: var(--surface-hi); }
.lt-accordion__head-left { display: flex; align-items: center; gap: 6px; }
.lt-accordion__arrow { color: var(--text2); transition: transform .2s; display: inline-block; }
.lt-accordion__item.open .lt-accordion__arrow { transform: rotate(90deg); }
.lt-accordion__body { display: none; padding: 12px 14px; }
.lt-accordion__item.open .lt-accordion__body { display: block; }
.lt-accordion__summary { font-size: 11px; color: var(--text2); }

/* 响应式 */
@media (max-width: 1200px) {
  .lt-kpi-grid { grid-template-columns: repeat(2, 1fr); }
}
```

- [ ] **Step 2: 升 main.css 版本号**

`static/index.html:34`：

```
旧: <link rel="stylesheet" href="/static/css/main.css?v=7">
新: <link rel="stylesheet" href="/static/css/main.css?v=8">
```

- [ ] **Step 3: 验证 CSS 语法**

Run: 浏览器打开实盘 tab，F12 控制台无 CSS 404/语法错误
Expected: 控制台干净，`.lt-*` 类在 Elements 面板可见

- [ ] **Step 4: Commit**

```bash
git add static/css/main.css static/index.html
git commit -m "refactor(live_trader): 新增 .lt-* 设计系统类 (徽章/KPI/手风琴)"
```

---

## Task 2: index.html 重写 `#tab-live-trader` 完整 HTML

**Files:**
- Modify: `static/index.html:1475-1579`（整段替换 `<!-- Live Trader Tab -->` 到 `</div>` 闭合）

**关键约束**：保留所有现有 `id`（`live-conn` / `live-mode` / `live-positions-tbody` / `live-equity-chart` 等），确保 `live_trader.js` 的 `getElementById` 不失效。新增 `id` 见下方注释。

- [ ] **Step 1: 用 Edit 替换 `#tab-live-trader` 整段**

把 `static/index.html` 第 1475-1579 行（从 `      <!-- Live Trader Tab: 实盘交易 -->` 到对应的 `      </div>` 闭合，即 `<!-- TQSDK Tab -->` 之前）整段替换为：

```html
      <!-- Live Trader Tab: 实盘交易 -->
      <div id="tab-live-trader">

        <!-- ① 顶栏：状态徽章 + 账号本金 + 一键KS -->
        <div class="lt-topbar">
          <div class="lt-topbar-badges">
            <span class="lt-badge lt-badge--ok" id="live-conn"><span class="lt-badge-dot"></span> QMT —</span>
            <span class="lt-badge lt-badge--warn" id="live-mode">—</span>
            <span class="lt-badge lt-badge--success" id="live-ks"><span class="lt-badge-dot"></span> KS —</span>
            <span class="lt-topbar-meta">账号 <b id="live-account">—</b> · 本金 <b id="live-capital">—</b></span>
          </div>
          <button class="btn btn-danger btn-sm" onclick="activateKillSwitch()">⏻ 一键 Kill Switch</button>
        </div>

        <!-- ② KPI 四连 -->
        <div class="lt-kpi-grid">
          <div class="lt-kpi">
            <div class="lt-kpi__label">总资产</div>
            <div class="lt-kpi__value lt-kpi__value--accent" id="lt-kpi-total">—</div>
            <div class="lt-kpi__sub" id="lt-kpi-total-sub">—</div>
          </div>
          <div class="lt-kpi">
            <div class="lt-kpi__label">持仓市值</div>
            <div class="lt-kpi__value" id="lt-kpi-mv">—</div>
            <div class="lt-kpi__sub" id="lt-kpi-mv-sub">—</div>
          </div>
          <div class="lt-kpi">
            <div class="lt-kpi__label">可用现金</div>
            <div class="lt-kpi__value" id="lt-kpi-cash">—</div>
            <div class="lt-kpi__sub" id="lt-kpi-cash-sub">—</div>
          </div>
          <div class="lt-kpi">
            <div class="lt-kpi__label" id="lt-kpi-pnl-label">总浮盈</div>
            <div class="lt-kpi__value" id="lt-kpi-pnl">—</div>
            <div class="lt-kpi__sub" id="lt-kpi-pnl-sub">—</div>
          </div>
        </div>

        <!-- ③ 净值曲线（主角，全宽） -->
        <div class="card">
          <div class="lt-chart-head">
            <div class="lt-chart-title">
              <svg class="nav-ico"><use href="#i-trend"/></svg>
              <span class="fs-13">净值曲线</span>
              <span class="muted fs-xs">当日 5min 快照 · 总资产</span>
            </div>
            <div class="lt-chart-range" id="lt-equity-range">
              <span class="chip active" data-days="1" onclick="loadLiveEquity(1)">1日</span>
              <span class="chip" data-days="5" onclick="loadLiveEquity(5)">5日</span>
              <span class="chip" data-days="30" onclick="loadLiveEquity(30)">30日</span>
              <span class="chip" data-days="365" onclick="loadLiveEquity(365)">全部</span>
            </div>
          </div>
          <div id="live-equity-chart" style="height:170px;"></div>
        </div>

        <!-- ④ 持仓表（全宽 + 汇总浮盈） -->
        <div class="card">
          <div class="flex-between mb-2">
            <span class="fs-13">持仓 <span class="muted fs-xs">(managed=false 为 ETF 保留，策略不动)</span></span>
            <span class="muted fs-xs">汇总浮盈 <b id="lt-positions-summary" class="tc-red">—</b></span>
          </div>
          <table class="data-table">
            <thead><tr><th>代码</th><th>股数</th><th>可卖</th><th>均价</th><th>现价</th><th>市值</th><th>浮盈</th><th>类型</th></tr></thead>
            <tbody id="live-positions-tbody"></tbody>
          </table>
        </div>

        <!-- ⑤ 委托 + 成交 -->
        <div class="grid-2">
          <div class="card">
            <h4 class="mb-2">委托 <span class="muted fs-xs">(最近 50 笔)</span></h4>
            <table class="data-table">
              <thead><tr><th>时间</th><th>代码</th><th>方向</th><th>价格</th><th>股数</th><th>状态</th><th>模式</th></tr></thead>
              <tbody id="live-orders-tbody"></tbody>
            </table>
          </div>
          <div class="card">
            <h4 class="mb-2">成交 <span class="muted fs-xs">(最近 50 笔)</span></h4>
            <table class="data-table">
              <thead><tr><th>时间</th><th>代码</th><th>方向</th><th>成交价</th><th>股数</th><th>模式</th></tr></thead>
              <tbody id="live-deals-tbody"></tbody>
            </table>
          </div>
        </div>

        <!-- ⑥ 折叠区（手风琴） -->
        <div class="lt-accordion">
          <div class="lt-accordion__title">低频操作 · 点击展开</div>

          <div class="lt-accordion__item">
            <div class="lt-accordion__header" onclick="toggleAccordion(this)">
              <span class="lt-accordion__head-left"><span class="lt-accordion__arrow">▸</span>执行开关 / 模式</span>
              <span class="lt-accordion__summary">买入 <b id="lt-sum-buy">—</b> · 卖出 <b id="lt-sum-sell">—</b> · <b id="lt-sum-mode">—</b></span>
            </div>
            <div class="lt-accordion__body">
              <div class="fs-13" style="line-height:2.2;">
                <div>买入开关: <b id="live-buy-switch">—</b>
                  <button class="btn btn-sm btn-ghost" onclick="toggleLiveSwitch('buy')">切换</button></div>
                <div>卖出开关: <b id="live-sell-switch">—</b>
                  <button class="btn btn-sm btn-ghost" onclick="toggleLiveSwitch('sell')">切换</button></div>
                <div style="margin-top:6px;">当前模式: <b id="live-mode-display">—</b>
                  <button class="btn btn-sm btn-ghost" onclick="switchLiveMode()">切换模式</button></div>
                <div id="live-switch-msg" class="muted fs-xs" style="margin-top:4px;">切 live 开始真钱交易(live→dry-run 会先撤在途单等终态)</div>
              </div>
            </div>
          </div>

          <div class="lt-accordion__item">
            <div class="lt-accordion__header" onclick="toggleAccordion(this)">
              <span class="lt-accordion__head-left"><span class="lt-accordion__arrow">▸</span>风控参数</span>
              <span class="lt-accordion__summary muted">(risk 段,实盘与模拟盘共用)</span>
            </div>
            <div class="lt-accordion__body">
              <div id="live-gates" class="fs-xs" style="line-height:1.8;">点"刷新"加载</div>
            </div>
          </div>

          <div class="lt-accordion__item">
            <div class="lt-accordion__header" onclick="toggleAccordion(this)">
              <span class="lt-accordion__head-left"><span class="lt-accordion__arrow">▸</span>对账记录</span>
              <span class="lt-accordion__summary muted">点"手动对账"触发</span>
            </div>
            <div class="lt-accordion__body">
              <button class="btn btn-sm btn-ghost mb-2" onclick="runReconcile()">手动对账</button>
              <div id="live-reconcile" class="fs-xs" style="line-height:1.6;">点"手动对账"触发</div>
            </div>
          </div>

          <div class="lt-accordion__item">
            <div class="lt-accordion__header" onclick="toggleAccordion(this)">
              <span class="lt-accordion__head-left"><span class="lt-accordion__arrow">▸</span>操作（离场扫描 / 扫描间隔）</span>
              <span class="lt-accordion__summary muted">间隔 <b id="lt-sum-interval">—</b>s</span>
            </div>
            <div class="lt-accordion__body">
              <div class="flex-gap mb-2">
                <button class="btn btn-sm" onclick="runExitScan()">触发离场扫描</button>
                <button class="btn btn-sm btn-danger" onclick="deactivateKillSwitch()">解除 Kill Switch</button>
              </div>
              <div class="flex-gap" style="align-items:center;font-size:13px;">
                <span>离场扫描间隔:</span>
                <input type="number" id="live-scan-interval" min="10" max="300" step="5" style="width:70px;">
                <span>秒</span>
                <button class="btn btn-sm" onclick="saveScanInterval()">保存</button>
                <span id="live-scan-interval-msg" class="muted fs-xs"></span>
              </div>
              <div class="muted fs-xs" style="margin-top:8px;">dry-run 模式下手动下单禁用(防误下单);扫描间隔范围 10~300 秒</div>
            </div>
          </div>

          <div class="lt-accordion__item">
            <div class="lt-accordion__header" onclick="toggleAccordion(this)">
              <span class="lt-accordion__head-left"><span class="lt-accordion__arrow">▸</span>审计回放</span>
              <span class="lt-accordion__summary muted">输入 order_id 回放</span>
            </div>
            <div class="lt-accordion__body">
              <div class="flex-gap mb-2">
                <input id="live-replay-oid" placeholder="输入 order_id" style="width:160px;">
                <button class="btn btn-sm" onclick="replayOrder()">回放</button>
              </div>
              <pre id="live-replay-out" class="fs-xs" style="max-height:200px; overflow:auto; background:var(--bg); padding:8px; border-radius:4px;">回放结果将显示在这里</pre>
            </div>
          </div>
        </div>

      </div>
```

- [ ] **Step 2: 升 live_trader.js 版本号**

`static/index.html` 中 `<script src="/static/js/live_trader.js?v=1">`（约 1681 行）：

```
旧: <script src="/static/js/live_trader.js?v=1"></script>
新: <script src="/static/js/live_trader.js?v=2"></script>
```

- [ ] **Step 3: 验证 HTML 结构 + id 完整**

Run: `node -e "const fs=require('fs');const h=fs.readFileSync('static/index.html','utf8');const ids=['live-conn','live-mode','live-account','live-capital','live-ks','lt-kpi-total','lt-kpi-mv','lt-kpi-cash','lt-kpi-pnl','live-positions-tbody','lt-positions-summary','live-equity-chart','lt-equity-range','live-orders-tbody','live-deals-tbody','live-buy-switch','live-sell-switch','live-mode-display','live-switch-msg','live-gates','live-reconcile','live-scan-interval','live-scan-interval-msg','live-replay-oid','live-replay-out'];ids.forEach(id=>{if(!h.includes('id=\"'+id+'\"'))console.log('MISSING:',id)});console.log('id check done')"`

Expected: 只输出 `id check done`，无 `MISSING:` 行

- [ ] **Step 4: 浏览器验证结构渲染**

Run: 启动主服务，打开实盘 tab
Expected: 看到顶栏徽章 + KPI 四连 + 净值图区 + 持仓表 + 委托成交 + 手风琴折叠区。数据是「—」（JS 还没适配，Task 3+ 才填）。F12 控制台无报错。

- [ ] **Step 5: Commit**

```bash
git add static/index.html
git commit -m "refactor(live_trader): 重写 #tab-live-trader 布局 (顶栏/KPI/净值主角/手风琴)"
```

---

## Task 3: live_trader.js `loadLiveStatus` 适配徽章渲染

**Files:**
- Modify: `static/js/live_trader.js:19-38`（`loadLiveStatus` 函数体）

- [ ] **Step 1: 替换 `loadLiveStatus` 函数**

把 `static/js/live_trader.js` 第 19-38 行的 `loadLiveStatus` 函数整段替换为：

```javascript
async function loadLiveStatus() {
  const setBadge = (id, text, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = '<span class="lt-badge-dot"></span> ' + text;
    el.className = 'lt-badge ' + cls;
  };
  try {
    const d = await _liveFetch('/live/status');
    setBadge('live-conn', d.qmt_connected ? 'QMT 已连接' : 'QMT 未连接',
             d.qmt_connected ? 'lt-badge--ok' : 'lt-badge--danger');
    setBadge('live-mode', d.mode || '—',
             d.mode === 'live' ? 'lt-badge--danger' : 'lt-badge--warn');
    const acc = document.getElementById('live-account');
    if (acc) acc.textContent = d.account_id || '—';
    const cap = document.getElementById('live-capital');
    if (cap) cap.textContent = '¥' + (d.live_capital || 0).toLocaleString();
    const ks = d.kill_switch || {};
    setBadge('live-ks', ks.activated ? 'KS 已激活' : 'KS 未激活',
             ks.activated ? 'lt-badge--danger' : 'lt-badge--success');
  } catch (e) {
    setBadge('live-conn', '服务未启动(8001)', 'lt-badge--danger');
  }
}
```

- [ ] **Step 2: 语法检查**

Run: `node -c static/js/live_trader.js`
Expected: 无输出（语法正确）

- [ ] **Step 3: 浏览器验证**

Run: 刷新实盘 tab（或点顶栏刷新——若状态栏刷新按钮已移除，靠 loadLiveAll 自动加载）
Expected: 顶栏三个徽章显示真实状态（QMT 连接/模式/KS），颜色随状态变化（连接=琥珀边、live=红、KS激活=红）

- [ ] **Step 4: Commit**

```bash
git add static/js/live_trader.js
git commit -m "refactor(live_trader): loadLiveStatus 改用徽章 class 渲染"
```

---

## Task 4: live_trader.js `loadLiveAsset` 适配 KPI 四连

**Files:**
- Modify: `static/js/live_trader.js:40-49`（`loadLiveAsset` 函数体）

**说明**：前 3 个 KPI（总资产/市值/可用）由 `loadLiveAsset` 填充；第 4 个 KPI（总浮盈，第一阶段降级）由 `loadLivePositions` 在 Task 5 填充。

- [ ] **Step 1: 替换 `loadLiveAsset` 函数**

把第 40-49 行的 `loadLiveAsset` 替换为：

```javascript
async function loadLiveAsset() {
  try {
    const d = await _liveFetch('/live/asset');
    const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    const fmt = v => '¥' + (Number(v) || 0).toLocaleString(void 0, { maximumFractionDigits: 2 });
    const total = Number(d.total_asset) || 0;
    const mv = Number(d.market_value) || 0;
    const cash = Number(d.cash) || 0;
    const frozen = Number(d.frozen_cash) || 0;
    setText('lt-kpi-total', fmt(total));
    setText('lt-kpi-mv', fmt(mv));
    setText('lt-kpi-cash', fmt(cash));
    // 副指标
    const cap = (await _liveFetch('/live/status').catch(() => ({}))).live_capital || 0;
    if (cap > 0) {
      const pnlVsCap = total - cap;
      const pctVsCap = (pnlVsCap / cap * 100).toFixed(2);
      const sign = pnlVsCap >= 0 ? '+' : '';
      const sub = document.getElementById('lt-kpi-total-sub');
      if (sub) { sub.textContent = sign + pctVsCap + '% / ' + sign + fmt(pnlVsCap); sub.style.color = pnlVsCap >= 0 ? 'var(--red)' : 'var(--green)'; }
    }
    const mvSub = document.getElementById('lt-kpi-mv-sub');
    if (mvSub) mvSub.textContent = '仓位 ' + (total > 0 ? (mv / total * 100).toFixed(1) : 0) + '%';
    const cashSub = document.getElementById('lt-kpi-cash-sub');
    if (cashSub) cashSub.textContent = '冻结 ' + fmt(frozen);
  } catch (e) { /* 静默 */ }
}
```

- [ ] **Step 2: 语法检查**

Run: `node -c static/js/live_trader.js`
Expected: 无输出

- [ ] **Step 3: 浏览器验证**

Run: 刷新实盘 tab
Expected: KPI 四连前 3 个显示金额（总资产琥珀、市值/可用白色），副指标显示仓位%和冻结。第 4 个「总浮盈」仍为「—」（Task 5 填）。

- [ ] **Step 4: Commit**

```bash
git add static/js/live_trader.js
git commit -m "refactor(live_trader): loadLiveAsset 适配 KPI 四连 + 副指标"
```

---

## Task 5: live_trader.js `loadLivePositions` 加汇总浮盈行

**Files:**
- Modify: `static/js/live_trader.js:51-69`（`loadLivePositions` 函数体）

- [ ] **Step 1: 替换 `loadLivePositions` 函数**

把第 51-69 行替换为：

```javascript
async function loadLivePositions() {
  const tbody = document.getElementById('live-positions-tbody');
  if (!tbody) return;
  try {
    const data = await _liveFetch('/live/positions');
    if (!data || data.length === 0) {
      tbody.innerHTML = '<tr><td colspan=8 style="text-align:center;color:var(--text2);">无持仓</td></tr>';
      const sumEl = document.getElementById('lt-positions-summary');
      if (sumEl) sumEl.textContent = '¥0';
      const pnlEl = document.getElementById('lt-kpi-pnl');
      if (pnlEl) { pnlEl.textContent = '¥0'; pnlEl.style.color = 'var(--text2)'; }
      return;
    }
    let totalFloat = 0;
    tbody.innerHTML = data.map(p => {
      const fp = Number(p.float_profit) || 0;
      totalFloat += fp;
      const tag = p.managed ? '<span class="tc-green">策略</span>' : '<span class="muted">ETF保留</span>';
      const pnlColor = fp >= 0 ? 'var(--red)' : 'var(--green)';
      return '<tr><td>' + p.code + '</td><td>' + p.volume + '</td><td>' + p.can_use_volume + '</td>' +
        '<td>' + (p.avg_cost || 0).toFixed(3) + '</td><td>' + (p.last_price || 0).toFixed(3) + '</td>' +
        '<td>' + (p.market_value || 0).toFixed(0) + '</td>' +
        '<td style="color:' + pnlColor + ';">' + fp.toFixed(0) + '</td>' +
        '<td>' + tag + '</td></tr>';
    }).join('');
    // 汇总浮盈（持仓表头右侧）
    const sumEl = document.getElementById('lt-positions-summary');
    if (sumEl) { sumEl.textContent = (totalFloat >= 0 ? '+' : '') + '¥' + totalFloat.toFixed(0); sumEl.style.color = totalFloat >= 0 ? 'var(--red)' : 'var(--green)'; }
    // KPI 第 4 项（第一阶段降级为总浮盈，标注"总浮盈"）
    const pnlEl = document.getElementById('lt-kpi-pnl');
    if (pnlEl) { pnlEl.textContent = (totalFloat >= 0 ? '+' : '') + '¥' + totalFloat.toFixed(0); pnlEl.style.color = totalFloat >= 0 ? 'var(--red)' : 'var(--green)'; }
    const pnlSub = document.getElementById('lt-kpi-pnl-sub');
    if (pnlSub) pnlSub.textContent = '持仓累计 · 待后端补 last_close 升级为今日盈亏';
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan=8 style="color:var(--red);">加载失败(服务未启动?)</td></tr>';
  }
}
```

- [ ] **Step 2: 语法检查**

Run: `node -c static/js/live_trader.js`
Expected: 无输出

- [ ] **Step 3: 浏览器验证**

Run: 刷新实盘 tab
Expected: 持仓表显示持仓行，表头右侧「汇总浮盈」+ KPI 第 4 项「总浮盈」同步显示同值，红涨绿跌。

- [ ] **Step 4: Commit**

```bash
git add static/js/live_trader.js
git commit -m "refactor(live_trader): loadLivePositions 加汇总浮盈 + 填充 KPI 第4项(降级总浮盈)"
```

---

## Task 6: live_trader.js `loadLiveEquity` 支持时间范围切换

**Files:**
- Modify: `static/js/live_trader.js:190-210`（`loadLiveEquity` 函数 + `_liveEquityChart` 变量）

- [ ] **Step 1: 替换 `loadLiveEquity` 函数**

把第 190-210 行（从 `let _liveEquityChart = null;` 到 `loadLiveEquity` 函数结束）替换为：

```javascript
let _liveEquityChart = null;
let _liveEquityDays = 1;
async function loadLiveEquity(days) {
  const el = document.getElementById('live-equity-chart');
  if (!el) return;
  if (typeof days === 'number') {
    _liveEquityDays = days;
    // 更新切换器 active 状态
    document.querySelectorAll('#lt-equity-range .chip').forEach(c => {
      c.classList.toggle('active', Number(c.dataset.days) === days);
    });
  }
  try {
    const d = await _liveFetch('/live/equity?days=' + _liveEquityDays);
    const pts = d.points || [];
    if (pts.length === 0) { el.innerHTML = '<div style="text-align:center;color:var(--text2);padding:60px;">暂无净值数据(盘中每 5min 采样)</div>'; return; }
    const xs = pts.map(p => (p.date || '') + ' ' + (p.time || ''));
    const totals = pts.map(p => p.total);
    if (!_liveEquityChart) _liveEquityChart = echarts.init(el);
    _liveEquityChart.setOption({
      tooltip: { trigger: 'axis' },
      grid: { left: 55, right: 20, top: 20, bottom: 35 },
      xAxis: { type: 'category', data: xs, axisLabel: { fontSize: 10 } },
      yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
      series: [{ name: '总资产', type: 'line', data: totals, smooth: true,
                lineStyle: { width: 2, color: '#f0b429' },
                itemStyle: { color: '#f0b429' },
                areaStyle: { opacity: 0.12, color: '#f0b429' } }],
    }, true);
  } catch (e) { el.innerHTML = '<div style="color:var(--red);padding:20px;">净值加载失败: ' + e.message + '</div>'; }
}
```

- [ ] **Step 2: 语法检查**

Run: `node -c static/js/live_trader.js`
Expected: 无输出

- [ ] **Step 3: 浏览器验证**

Run: 刷新实盘 tab，点净值区「5日」「30日」按钮
Expected: 点击后按钮高亮切换，净值曲线重新加载对应天数数据。默认「1日」高亮。

- [ ] **Step 4: Commit**

```bash
git add static/js/live_trader.js
git commit -m "feat(live_trader): 净值曲线支持 1/5/30/全部 时间范围切换"
```

---

## Task 7: live_trader.js 手风琴交互 + 摘要填充

**Files:**
- Modify: `static/js/live_trader.js`（新增 `toggleAccordion` 函数 + 修改 `loadLiveSwitches`/`loadLiveModeDisplay`/`loadScanInterval` 填充摘要）

- [ ] **Step 1: 在 `loadLiveAll` 函数前新增 `toggleAccordion`**

找到 `static/js/live_trader.js` 中的 `// v2: 实盘 tab 激活时加载全部` 注释行（约 315 行），在其**上方**插入：

```javascript
// ─── 手风琴折叠交互 ──────────────────────────────
function toggleAccordion(headerEl) {
  const item = headerEl.parentElement;
  if (item && item.classList.contains('lt-accordion__item')) {
    item.classList.toggle('open');
  }
}
```

- [ ] **Step 2: 修改 `loadLiveSwitches` 填充折叠区摘要**

找到 `loadLiveSwitches` 函数（约 229-236 行），在函数末尾的 `catch` 之前，`setEl('live-sell-switch', d.sell_enabled);` 之后，追加摘要填充：

```javascript
async function loadLiveSwitches() {
  try {
    const d = await _liveFetch('/live/config/switches');
    const setEl = (id, v) => { const el = document.getElementById(id); if (el) { el.textContent = v ? '开' : '关'; el.style.color = v ? 'var(--green)' : 'var(--text2)'; } };
    setEl('live-buy-switch', d.buy_enabled);
    setEl('live-sell-switch', d.sell_enabled);
    // 折叠区摘要
    const sb = document.getElementById('lt-sum-buy'); if (sb) sb.textContent = d.buy_enabled ? '开' : '关';
    const ss = document.getElementById('lt-sum-sell'); if (ss) ss.textContent = d.sell_enabled ? '开' : '关';
  } catch (e) { /* 静默 */ }
}
```

- [ ] **Step 3: 修改 `loadLiveModeDisplay` 填充模式摘要**

找到 `loadLiveModeDisplay` 函数（约 261-267 行），替换为：

```javascript
async function loadLiveModeDisplay() {
  try {
    const d = await _liveFetch('/live/config/mode');
    const el = document.getElementById('live-mode-display');
    if (el) { el.textContent = d.mode; el.style.color = d.mode === 'live' ? 'var(--red)' : 'var(--orange)'; }
    const sm = document.getElementById('lt-sum-mode'); if (sm) { sm.textContent = d.mode; sm.style.color = d.mode === 'live' ? 'var(--red)' : 'var(--orange)'; }
  } catch (e) { /* 静默 */ }
}
```

- [ ] **Step 4: 修改 `loadScanInterval` 填充间隔摘要**

找到 `loadScanInterval` 函数（约 160-166 行），替换为：

```javascript
async function loadScanInterval() {
  try {
    const d = await _liveFetch('/live/config/scan-interval');
    const el = document.getElementById('live-scan-interval');
    if (el) el.value = d.interval_sec;
    const sm = document.getElementById('lt-sum-interval'); if (sm) sm.textContent = d.interval_sec;
  } catch (e) { console.warn('加载扫描间隔失败:', e.message); }
}
```

- [ ] **Step 5: 语法检查**

Run: `node -c static/js/live_trader.js`
Expected: 无输出

- [ ] **Step 6: 浏览器验证**

Run: 刷新实盘 tab
Expected: 5 个手风琴项默认收起（只看到 header + 摘要）；点击 header 展开/收起（箭头旋转 90°）；摘要显示「买入 开 · 卖出 开 · dry-run」「间隔 60s」等。

- [ ] **Step 7: Commit**

```bash
git add static/js/live_trader.js
git commit -m "feat(live_trader): 手风琴折叠交互 + 折叠区摘要填充"
```

---

## Task 8: 全局验收 + emoji 清理

**Files:**
- Modify: `static/index.html`（如有残留 emoji 标题则清理——Task 2 的 HTML 已去掉 📊📋📈💹🎛🛡🔍🎬📜，此 task 确认无残留）

- [ ] **Step 1: 确认 #tab-live-trader 内无 emoji 标题**

Run: `node -e "const fs=require('fs');const h=fs.readFileSync('static/index.html','utf8');const m=h.match(/<!-- Live Trader Tab[\s\S]*?<div id=\"tab-tqsdk\">/);const s=m?m[0]:'';const emojis=['📊','📋','📈','💹','🎛','🛡','🔍','🎬','📜'];emojis.forEach(e=>{if(s.includes(e))console.log('EMOJI残留:',e)});console.log('emoji check done')"`
Expected: 只输出 `emoji check done`，无 `EMOJI残留:` 行

- [ ] **Step 2: 确认无内联 style 残留（除动态值必要的）**

Run: 浏览器 F12 → Elements → 在 `#tab-live-trader` 内搜索 `style="`
Expected: 仅剩 `live-equity-chart` 的 `height:170px`、`live-replay-out` 的 `max-height/overflow` 等必要动态样式，无纯布局内联 style

- [ ] **Step 3: 控制台无 JS 错误**

Run: 刷新实盘 tab，F12 Console
Expected: 无红色 error。若有 `Cannot read property of null`，检查对应 id 是否在 Task 2 HTML 中存在。

- [ ] **Step 4: 功能回归验证**

逐项验证（在 dry-run 模式下）：
- 顶栏三个徽章状态正确（QMT/模式/KS）
- KPI 四连数字正确，总资产琥珀、总浮盈红涨绿跌
- 净值曲线渲染，1/5/30/全部 切换正常
- 持仓表 + 汇总浮盈正确
- 委托/成交表正常加载
- 手风琴 5 项展开/收起正常
- 「切换买入开关」弹出确认框，切换后摘要更新
- 「切换模式」弹出确认框
- 「手动对账」可触发
- 「触发离场扫描」弹出确认框
- 扫描间隔可保存
- 审计回放输入 order_id 可回放
- 一键 Kill Switch / 解除 Kill Switch 弹出确认框

- [ ] **Step 5: 其他 tab 不受影响**

Run: 依次切换 选股/自选股/回测/AI回测/数据管理/策略工厂/报告仓库/系统设置/热点板块/交易控制/通达信选股 tab
Expected: 所有 tab 正常显示，无报错

- [ ] **Step 6: Commit 验收记录**

```bash
git add -A
git commit --allow-empty -m "test(live_trader: 布局重构验收通过 (10项功能回归+11 tab无影响)"
```

---

## Self-Review

### Spec 覆盖检查
- §3 信息架构（顶栏/KPI/净值/持仓/委托成交/折叠区）→ Task 2 HTML 全覆盖 ✓
- §4.1 顶栏徽章 + 一键KS → Task 2 HTML + Task 3 JS ✓
- §4.2 KPI 四连 → Task 2 HTML + Task 4 JS（+ Task 5 填第4项）✓
- §4.3 净值切换器 → Task 2 HTML + Task 6 JS ✓
- §4.4 持仓表汇总浮盈 → Task 2 HTML + Task 5 JS ✓
- §4.5 委托+成交 → Task 2 HTML（复用现有 JS）✓
- §4.6 手风琴折叠区 → Task 2 HTML + Task 7 JS ✓
- §5 视觉 token 沿用 → Task 1 CSS 用 var(--*) ✓
- §6 内联 style 清理 → Task 2 HTML 用类 + Task 8 Step 2 验证 ✓
- §7 向后兼容（保留 id）→ Task 2 Step 3 id 检查脚本 ✓
- §9.1 今日盈亏降级 → Task 5 第4项用总浮盈 + 标注待升级 ✓
- §11 验收标准 10 条 → Task 8 全覆盖 ✓

### 占位符扫描
- 无 TBD/TODO ✓
- 每个 code step 有完整代码 ✓
- commit message 具体 ✓

### 类型/命名一致性
- `.lt-accordion__item.open` class（Task 1 CSS）↔ `toggleAccordion` 切换 `.open`（Task 7）✓
- `loadLiveEquity(days)` 参数（Task 6）↔ HTML `onclick="loadLiveEquity(1)"`（Task 2）✓
- `lt-kpi-pnl` id（Task 2 HTML）↔ `getElementById('lt-kpi-pnl')`（Task 5）✓
- `lt-positions-summary` id（Task 2）↔ Task 5 填充 ✓
- `lt-equity-range` id（Task 2）↔ Task 6 querySelectorAll ✓
- `lt-sum-buy/sell/mode/interval` id（Task 2）↔ Task 7 填充 ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-11-live-trader-layout-redesign.md`.
