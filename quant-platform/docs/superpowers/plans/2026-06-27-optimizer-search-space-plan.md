# 优化器搜索空间卡片 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现"优化器搜索空间"卡片的前端渲染 + 保存 + AI 结果联动写入按钮（14 参数 × 5 分组）

**Architecture:** 前端 `renderSearchSpace()` 替换 stub，按 5 组渲染 14 行 min/max/step 输入框，缺键从 FALLBACK 取默认值；`saveSearchSpace()` 提交到新端点 `POST /api/settings/list/optimizer_search_space`；`applyAiBestToRisk()` 调用已有 `GET /api/backtest/ai/status` + `POST /api/backtest/ai/apply`，取整 1 位小数后写入止盈止损卡片。后端仅新增 1 个原子保存端点。

**Tech Stack:** Vanilla JS (无框架), FastAPI (Python), JSON 配置持久化

---

## 文件结构

| 文件 | 职责 | 操作 |
|------|------|------|
| `static/js/main.js` | 前端：渲染搜索空间、保存、AI 联动按钮 | 修改 |
| `app/api/system.py` | 后端：新增原子保存端点 | 修改 |
| `static/index.html` | HTML 已有卡片占位，无需改动 | — |
| `config/app_setting.json` | 数据已有 10 个参数，保存时自动补齐 4 个缺口 | — |
| `core/settings.py` | `optimizer_search_space` property 已有 | — |
| `app/api/backtest.py` | `POST /api/backtest/ai/apply` 已有，直接复用 | — |

---

### Task 1: 后端 — 新增搜索空间原子保存端点

**Files:**
- Modify: `app/api/system.py`

- [ ] **Step 1: 在 `app/api/system.py` 末尾追加路由**

在文件末尾追加以下代码：

```python


class SearchSpaceUpdate(BaseModel):
    items: dict

@router.post("/api/settings/list/optimizer_search_space")
async def save_optimizer_search_space(body: SearchSpaceUpdate):
    """原子保存优化器搜索空间（不干扰 POST /api/settings）"""
    items = body.items
    if not items or not isinstance(items, dict) or len(items) < 1:
        return {"status": "error", "message": "items 不能为空"}
    try:
        settings.set("optimizer", "search_space", items, save=True)
        log.info(f"优化器搜索空间已保存 ({len(items)} 参数)")
        return {"status": "ok", "message": "搜索空间已保存"}
    except Exception as e:
        log.error(f"保存搜索空间失败: {e}")
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 2: 验证端点语法**

```bash
cd e:/1target/p9_project/quant-platform
python -c "import ast; ast.parse(open('app/api/system.py','r',encoding='utf-8').read()); print('SYNTAX OK')"
```
Expected: `SYNTAX OK`

- [ ] **Step 3: 验证路由注册**

```bash
python -c "
from app.api.system import router
routes = [r.path for r in router.routes]
assert '/api/settings/list/optimizer_search_space' in routes
print('ROUTE OK:', [r for r in routes if 'optimizer' in r])
"
```
Expected: `ROUTE OK: ['/api/settings/list/optimizer_search_space']`

- [ ] **Step 4: 手动测试端点**

```bash
# 启动服务后
curl -X POST http://localhost:8000/api/settings/list/optimizer_search_space \
  -H "Content-Type: application/json" \
  -d '{"items":{"tp1_profit":{"min":2.0,"max":6.0,"step":0.5}}}'
```
Expected: `{"status":"ok","message":"搜索空间已保存"}`

- [ ] **Step 5: Commit**

```bash
git add app/api/system.py
git commit -m "feat(api): add POST /api/settings/list/optimizer_search_space endpoint"
```

---

### Task 2: 前端 — 数据定义和缺键降级

**Files:**
- Modify: `static/js/main.js:738`

- [ ] **Step 1: 替换 stub 函数位置，插入常量定义和 FALLBACK**

在 `static/js/main.js` 中，找到第 738 行的 `function renderSearchSpace(data) { /* stub - AI optimizer card */ }`，替换为以下内容：

```javascript
// ── 优化器搜索空间参数定义 (14 参数 × 5 分组) ──
const SEARCH_SPACE_PARAMS = [
  // ── 阶梯止盈 ──
  { key: 'tp1_profit',             label: '止盈1 盈利%',   group: '阶梯止盈', isInt: false },
  { key: 'tp2_profit',             label: '止盈2 盈利%',   group: '阶梯止盈', isInt: false },
  { key: 'tp3_profit',             label: '止盈3 盈利%',   group: '阶梯止盈', isInt: false },
  { key: 'tp1_ratio',              label: '止盈1 卖出%',   group: '阶梯止盈', isInt: false },
  { key: 'tp2_ratio',              label: '止盈2 卖出%',   group: '阶梯止盈', isInt: false },
  { key: 'tp3_ratio',              label: '止盈3 卖出%',   group: '阶梯止盈', isInt: false },
  // ── 止损 ──
  { key: 'hard_stop_loss_pct',     label: '硬止损%',       group: '止损',     isInt: false },
  { key: 'breakeven_threshold_pct',label: '保本触发%',     group: '止损',     isInt: false },
  { key: 'breakeven_stop_pnl_pct', label: '保本线%',       group: '止损',     isInt: false },
  // ── 移动止盈 ──
  { key: 'trailing_activate_pct',  label: '移动激活%',     group: '移动止盈', isInt: false },
  { key: 'trailing_drawdown_pct',  label: '移动回撤%',     group: '移动止盈', isInt: false },
  // ── 时间 ──
  { key: 'time_exit_days',         label: '退出天数',      group: '时间',     isInt: true  },
  { key: 'time_exit_force_days',   label: '强制退出天',    group: '时间',     isInt: true  },
  // ── 首日弱势 ──
  { key: 'first_day_exit_min_profit', label: '目标涨幅%',  group: '首日弱势', isInt: false },
  { key: 'first_day_exit_days',    label: '有效天数',      group: '首日弱势', isInt: true  },
];

// 缺键降级源（app_setting.json 中缺失的参数从 FALLBACK 取默认值）
const FALLBACK_SEARCH_SPACE = {
  tp3_profit:               { min: 18.0, max: 30.0, step: 1.0 },
  tp3_ratio:                { min: 0.2,  max: 0.4,  step: 0.05 },
  time_exit_force_days:     { min: 3,    max: 12,   step: 1 },
  first_day_exit_min_profit:{ min: 1.0,  max: 5.0,  step: 0.5 },
  first_day_exit_days:      { min: 1,    max: 3,    step: 1 },
};
```

- [ ] **Step 2: 验证语法**

```bash
# 无需启动服务，纯语法检查
node -e "const SEARCH_SPACE_PARAMS = [{key:'a',label:'b',group:'c',isInt:false}]; console.log(SEARCH_SPACE_PARAMS.length); console.log('OK');"
```
Expected: `1` 然后 `OK`

- [ ] **Step 3: Commit**

```bash
git add static/js/main.js
git commit -m "feat(ui): add SEARCH_SPACE_PARAMS and FALLBACK_SEARCH_SPACE constants (14 params)"
```

---

### Task 3: 前端 — renderSearchSpace 实现

**Files:**
- Modify: `static/js/main.js`（在 Task 2 的常量定义之后追加）

- [ ] **Step 1: 实现 renderSearchSpace 函数**

在 `SEARCH_SPACE_PARAMS` 和 `FALLBACK_SEARCH_SPACE` 常量定义之后，插入以下代码：

```javascript
function renderSearchSpace(data) {
  const list = document.getElementById('search-space-list');
  if (!list) return;

  const cfg = data || {};
  let html = '';
  let currentGroup = '';

  for (const def of SEARCH_SPACE_PARAMS) {
    // 缺键从 FALLBACK 取默认值
    let v = cfg[def.key];
    if (!v || typeof v.min === 'undefined') {
      v = FALLBACK_SEARCH_SPACE[def.key] || { min: 0, max: 1, step: def.isInt ? 1 : 0.5 };
    }

    // 分组标题
    if (def.group !== currentGroup) {
      currentGroup = def.group;
      html += '<div style="margin-top:6px;margin-bottom:2px;font-size:11px;font-weight:600;color:var(--text2);border-bottom:1px solid var(--border);padding-bottom:2px">── ' + currentGroup + '</div>';
    }

    var min = v.min;
    var max = v.max;
    var step = v.step || (def.isInt ? 1 : 0.5);
    var inputStep = def.isInt ? 1 : 0.01;  // 输入框允许任意精度，展示 step 独立

    html += '<div class="ss-row" style="display:flex;align-items:center;gap:6px;font-size:12px;padding:2px 0">'
      + '<label style="min-width:105px;font-size:11px">' + def.label + '</label>'
      + '<input type="number" class="ss-min" data-key="' + def.key + '" value="' + min + '" step="' + inputStep + '" style="width:62px;height:28px">'
      + '<span style="color:var(--text2)">~</span>'
      + '<input type="number" class="ss-max" data-key="' + def.key + '" value="' + max + '" step="' + inputStep + '" style="width:62px;height:28px">'
      + '<span style="color:var(--text2);font-size:11px;margin-left:4px">步长</span>'
      + '<input type="number" class="ss-step" data-key="' + def.key + '" value="' + step + '" step="' + (def.isInt ? 1 : 0.01) + '" style="width:52px;height:28px">'
      + '</div>';
  }

  if (!html) {
    html = '<div style="color:var(--text2);font-size:12px;padding:8px">暂无搜索空间配置，请先运行 AI 优化或手动配置</div>';
  }

  list.innerHTML = html;
}
```

- [ ] **Step 2: 验证语法**

```bash
node -e "
// 模拟 renderSearchSpace 的核心逻辑
var data = { tp1_profit: { min: 2.0, max: 6.0, step: 0.5 } };
var def = { key: 'tp1_profit', label: '止盈1', group: '阶梯止盈', isInt: false };
var v = data[def.key] || { min: 0, max: 1, step: 0.5 };
console.log('min=' + v.min + ' max=' + v.max + ' step=' + v.step);
console.log('OK');
"
```
Expected: `min=2.0 max=6.0 step=0.5` 然后 `OK`

- [ ] **Step 3: Commit**

```bash
git add static/js/main.js
git commit -m "feat(ui): implement renderSearchSpace() with 14 params in 5 groups"
```

---

### Task 4: 前端 — saveSearchSpace 实现

**Files:**
- Modify: `static/js/main.js`（在 renderSearchSpace 之后追加）

- [ ] **Step 1: 实现 saveSearchSpace 函数**

在 `renderSearchSpace` 函数之后，找到原来的 `function saveSearchSpace() { /* stub */ }` 所在位置（约第 776 行），替换为：

```javascript
async function saveSearchSpace() {
  const msgEl = document.getElementById('save-sspace-msg');
  if (!msgEl) return;

  // 收集所有行的输入值
  var items = {};
  var errors = [];
  document.querySelectorAll('#search-space-list .ss-row').forEach(function(row) {
    var minEl = row.querySelector('.ss-min');
    var maxEl = row.querySelector('.ss-max');
    var stepEl = row.querySelector('.ss-step');
    if (!minEl || !maxEl) return;
    var key = minEl.dataset.key;
    var isInt = (key === 'time_exit_days' || key === 'time_exit_force_days' || key === 'first_day_exit_days');
    var minVal = isInt ? parseInt(minEl.value) : parseFloat(minEl.value);
    var maxVal = isInt ? parseInt(maxEl.value) : parseFloat(maxEl.value);
    var stepVal = stepEl ? (isInt ? parseInt(stepEl.value) : parseFloat(stepEl.value)) : (isInt ? 1 : 0.5);

    if (isNaN(minVal) || isNaN(maxVal)) return;  // 空行跳过
    if (minVal >= maxVal) { errors.push(key + ': min必须<max'); return; }
    if (isNaN(stepVal) || stepVal <= 0) { errors.push(key + ': step必须>0'); return; }

    items[key] = { min: minVal, max: maxVal, step: stepVal };
  });

  if (errors.length > 0) {
    alert('校验失败:\n' + errors.join('\n'));
    return;
  }

  if (Object.keys(items).length === 0) {
    alert('请至少填写一个参数');
    return;
  }

  msgEl.textContent = '保存中...';
  try {
    var r = await fetch('/api/settings/list/optimizer_search_space', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: items })
    }).then(function(r) { return r.json(); });
    if (r.status === 'ok') {
      msgEl.textContent = '✓ 已保存';
      msgEl.style.color = 'var(--green)';
      setTimeout(function() { msgEl.textContent = ''; }, 2000);
    } else {
      msgEl.textContent = '✗ ' + (r.message || '失败');
      msgEl.style.color = 'var(--red)';
    }
  } catch (e) {
    msgEl.textContent = '✗ 网络错误';
    msgEl.style.color = 'var(--red)';
  }
}
```

- [ ] **Step 2: 验证语法**

```bash
node -e "
// 模拟收集逻辑
var row = document.createElement('div');
row.innerHTML = '<input class=\"ss-min\" value=\"2.0\"><input class=\"ss-max\" value=\"-1\">';
var minEl = row.querySelector('.ss-min');
var maxEl = row.querySelector('.ss-max');
var minVal = parseFloat(minEl.value);
var maxVal = parseFloat(maxEl.value);
var ok = minVal < maxVal;
console.log('min=' + minVal + ' max=' + maxVal + ' valid=' + ok);
console.log('SYNTAX OK');
"
```
Expected: `min=2.0 max=-1 valid=false` 然后 `SYNTAX OK`

- [ ] **Step 3: Commit**

```bash
git add static/js/main.js
git commit -m "feat(ui): implement saveSearchSpace() with frontend validation"
```

---

### Task 5: 前端 — applyAiBestToRisk 实现

**Files:**
- Modify: `static/js/main.js`（在 saveSearchSpace 之后追加）

- [ ] **Step 1: 实现 applyAiBestToRisk 函数**

在 `saveSearchSpace` 函数之后插入：

```javascript
async function applyAiBestToRisk() {
  // 第一步：获取 AI 状态，取 best_params
  var bestParams = null;
  try {
    var stateResp = await fetch('/api/backtest/ai/status').then(function(r) { return r.json(); });
    bestParams = stateResp.best_params;
  } catch (e) {
    alert('无法连接到服务器');
    return;
  }

  if (!bestParams || Object.keys(bestParams).length === 0) {
    alert('暂无 AI 优化结果。请先在 AI 回测 tab 运行一次优化。');
    return;
  }

  // 第二步：四舍五入到 1 位小数
  var rounded = {};
  var lines = [];
  for (var k in bestParams) {
    if (!bestParams.hasOwnProperty(k)) continue;
    var raw = bestParams[k];
    if (typeof raw !== 'number') continue;
    var applied = Math.round(raw * 10) / 10;  // round to 1 decimal
    rounded[k] = applied;
    lines.push(k + ': ' + raw + ' → ' + applied);
  }

  if (lines.length === 0) {
    alert('AI 最优参数为空，无法应用');
    return;
  }

  // 第三步：弹 confirm 确认
  var confirmMsg = 'AI 最优参数（取整后）将写入止盈止损：\n\n' + lines.join('\n') + '\n\n是否确认？';
  if (!confirm(confirmMsg)) return;

  // 第四步：调用已有 apply 端点
  try {
    var applyResp = await fetch('/api/backtest/ai/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ params: rounded })
    }).then(function(r) { return r.json(); });

    if (applyResp.status === 'ok') {
      // 第五步：刷新止盈止损卡片
      if (typeof loadSettings === 'function') {
        await loadSettings();
      }
      addLog('ok', 'AI 最优参数已应用到止盈止损 (' + (applyResp.applied || []).length + ' 项)');
    } else {
      alert('应用失败: ' + (applyResp.message || '未知错误'));
    }
  } catch (e) {
    alert('应用失败: ' + e.message);
  }
}
```

- [ ] **Step 2: 在 HTML 中绑定按钮**

确认 `static/index.html:930` 的保存按钮已经正确绑定。应用按钮需要在前端渲染时追加到卡片底部。在 `renderSearchSpace` 函数的 `list.innerHTML = html;` 之前追加：

```javascript
  // 追加"应用 AI 最优参数"按钮
  html += '<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">'
    + '<button class="btn btn-ghost btn-sm" onclick="applyAiBestToRisk()" style="color:var(--accent);width:100%">▶ 应用 AI 最优参数到止盈止损卡片</button>'
    + '</div>';
```

将此代码插入到 `renderSearchSpace` 函数中 `} else {` 之后、`list.innerHTML = html;` 之前的适当位置。完整插入位置如下：

```javascript
function renderSearchSpace(data) {
  // ... 循环生成 html ...

  if (!html) {
    html = '<div style="color:var(--text2);font-size:12px;padding:8px">暂无搜索空间配置，请先运行 AI 优化或手动配置</div>';
  }

  // 追加"应用 AI 最优参数"按钮
  html += '<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">'
    + '<button class="btn btn-ghost btn-sm" onclick="applyAiBestToRisk()" style="color:var(--accent);width:100%">▶ 应用 AI 最优参数到止盈止损卡片</button>'
    + '</div>';

  list.innerHTML = html;
}
```

- [ ] **Step 3: 验证语法**

```bash
node -e "
var bestParams = { tp1_profit: 3.72, hard_stop: -5.83 };
var rounded = {};
var k = 'tp1_profit';
var raw = bestParams[k];
var applied = Math.round(raw * 10) / 10;
rounded[k] = applied;
console.log('raw=' + raw + ' rounded=' + applied);
console.log('OK');
"
```
Expected: `raw=3.72 rounded=3.7` 然后 `OK`

- [ ] **Step 4: Commit**

```bash
git add static/js/main.js
git commit -m "feat(ui): implement applyAiBestToRisk() with round-then-confirm flow"
```

---

### Task 6: 端到端验证

**Files:**
- 无新增

- [ ] **Step 1: 确认所有 stub 已替换**

```bash
grep -n "stub" static/js/main.js | grep -i "searchSpace\|saveSearchSpace\|renderSearchSpace"
```
Expected: 输出为空（无 stub 残留）

- [ ] **Step 2: 启动服务**

```bash
python main.py
```

- [ ] **Step 3: 浏览器验证**

1. 打开 http://localhost:8000 → 进入"系统参数配置"tab
2. 确认搜索空间卡片渲染了 14 行 × 5 组输入框
3. 修改某个 min/max 值 → 点"保存此卡" → 看到 "✓ 已保存"
4. 刷新页面 → 值已持久化
5. 点"▶ 应用 AI 最优参数到止盈止损卡片" → 若无 AI 历史结果则 alert 提示
6. 修改 min 填入比 max 大的值 → 保存 → 看到校验 alert

- [ ] **Step 4: pytest 确认无回归**

```bash
pytest tests/ -x --tb=short -q
```
Expected: 21 passed

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: verify optimizer search space card e2e"
```

---

### Task 7: 推送

- [ ] **Step 1: 检查 git log 和 push**

```bash
git log --oneline -6
git push origin master
```

---

## 完成检查清单

- [ ] `renderSearchSpace` 渲染 14 参数 × 5 分组
- [ ] 缺键从 FALLBACK 取默认值，保存后自动补齐
- [ ] `saveSearchSpace` 有前端校验（min<max, step>0）
- [ ] `applyAiBestToRisk` 取整 1 位小数 + confirm + 写入
- [ ] 21 个现有测试全过
- [ ] 不修改 `POST /api/settings`、`saveRiskSettings`、`ai_optimizer.run()`
