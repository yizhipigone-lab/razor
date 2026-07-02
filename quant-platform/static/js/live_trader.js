// ─── Live Trader 实盘交易 ─────────────────────────────────
// live_trader 在 Windows 端 8001,前端浏览器直连(同机)
const LIVE_API = 'http://127.0.0.1:8001';

async function _liveFetch(path, opts) {
  try {
    const r = await fetch(LIVE_API + path, opts);
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || r.statusText);
    }
    return await r.json();
  } catch (e) {
    console.error('live API error', path, e);
    throw e;
  }
}

async function loadLiveStatus() {
  try {
    const d = await _liveFetch('/live/status');
    const conn = document.getElementById('live-conn');
    if (conn) conn.textContent = d.qmt_connected ? '🟢 QMT已连接' : '🔴 未连接';
    if (conn) conn.style.color = d.qmt_connected ? 'green' : 'red';
    const mode = document.getElementById('live-mode');
    if (mode) { mode.textContent = d.mode; mode.style.color = d.mode === 'live' ? 'red' : 'orange'; }
    const acc = document.getElementById('live-account');
    if (acc) acc.textContent = d.account_id;
    const cap = document.getElementById('live-capital');
    if (cap) cap.textContent = '¥' + (d.live_capital || 0).toLocaleString();
    const ks = d.kill_switch || {};
    const ksEl = document.getElementById('live-ks');
    if (ksEl) { ksEl.textContent = ks.activated ? '🔴 已激活' : '🟢 未激活'; ksEl.style.color = ks.activated ? 'red' : 'green'; }
  } catch (e) {
    const conn = document.getElementById('live-conn');
    if (conn) { conn.textContent = '⚠ 服务未启动(8001)'; conn.style.color = 'red'; }
  }
}

async function loadLiveAsset() {
  try {
    const d = await _liveFetch('/live/asset');
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set('live-cash', '¥' + (d.cash || 0).toFixed(2));
    set('live-frozen', '¥' + (d.frozen_cash || 0).toFixed(2));
    set('live-mv', '¥' + (d.market_value || 0).toFixed(2));
    set('live-total', '¥' + (d.total_asset || 0).toFixed(2));
  } catch (e) { /* 静默 */ }
}

async function loadLivePositions() {
  const tbody = document.getElementById('live-positions-tbody');
  if (!tbody) return;
  try {
    const data = await _liveFetch('/live/positions');
    if (!data || data.length === 0) { tbody.innerHTML = '<tr><td colspan=8 style="text-align:center;color:var(--text-muted);">无持仓</td></tr>'; return; }
    tbody.innerHTML = data.map(p => {
      const tag = p.managed ? '<span style="color:green;">策略</span>' : '<span style="color:var(--text-muted);">ETF保留</span>';
      const pnlColor = (p.float_profit || 0) >= 0 ? 'red' : 'green';
      return '<tr><td>' + p.code + '</td><td>' + p.volume + '</td><td>' + p.can_use_volume + '</td>' +
        '<td>' + (p.avg_cost || 0).toFixed(3) + '</td><td>' + (p.last_price || 0).toFixed(3) + '</td>' +
        '<td>' + (p.market_value || 0).toFixed(0) + '</td>' +
        '<td style="color:' + pnlColor + ';">' + (p.float_profit || 0).toFixed(0) + '</td>' +
        '<td>' + tag + '</td></tr>';
    }).join('');
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan=8 style="color:red;">加载失败(服务未启动?)</td></tr>';
  }
}

async function loadLiveOrders() {
  const tbody = document.getElementById('live-orders-tbody');
  if (!tbody) return;
  try {
    const data = await _liveFetch('/live/orders?limit=50');
    if (!data || data.length === 0) { tbody.innerHTML = '<tr><td colspan=7 style="text-align:center;color:var(--text-muted);">无委托</td></tr>'; return; }
    const statusMap = { 48: '未报', 49: '待报', 50: '已报', 51: '待撤', 52: '部成待撤', 53: '部撤', 54: '已撤', 55: '部成', 56: '已成', 57: '废单', 255: '未知' };
    tbody.innerHTML = data.map(o => {
      const dirColor = o.direction === 'buy' ? 'red' : 'green';
      const stColor = o.status === 56 ? 'green' : (o.status === 57 ? 'red' : 'orange');
      const ts = o.created_at ? o.created_at.slice(11, 19) : '';
      return '<tr><td>' + ts + '</td><td>' + o.code + '</td>' +
        '<td style="color:' + dirColor + ';">' + (o.direction === 'buy' ? '买入' : '卖出') + '</td>' +
        '<td>' + (o.price || 0).toFixed(2) + '</td><td>' + o.volume + '</td>' +
        '<td style="color:' + stColor + ';">' + (statusMap[o.status] || o.status) + '</td>' +
        '<td>' + o.mode + '</td></tr>';
    }).join('');
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan=7 style="color:red;">加载失败</td></tr>';
  }
}

function loadLiveGates() {
  const el = document.getElementById('live-gates');
  if (!el) return;
  el.innerHTML =
    '<div>闸门1 单笔金额 ≤ 20%×资金</div>' +
    '<div>闸门2 现金保留 ≥ 10% (仅buy)</div>' +
    '<div>闸门3 单只集中度 ≤ 30% (含在途预扣 C1)</div>' +
    '<div>闸门4 总仓位 ≤ 90% (含在途预扣 C1)</div>' +
    '<div>闸门5a 日亏 ≥ 3% 禁buy (QMT缺价fail-safe H1)</div>' +
    '<div>闸门5b 单笔浮亏 ≥ 5% 禁该只</div>' +
    '<div>闸门6 启动自检 (QMT连接+参数+DB)</div>' +
    '<div>闸门7 连续5次risk/broker拒→kill (5分钟窗 H4)</div>' +
    '<div>闸门8 kill switch 激活时全拒</div>' +
    '<div>闸门9 T+1 卖出≤can_use_volume (H6)</div>' +
    '<div style="margin-top:6px;color:var(--text-muted);font-size:11px;">闸门实时状态在下单时触发检查</div>';
}

async function runReconcile() {
  const el = document.getElementById('live-reconcile');
  if (!el) return;
  try {
    el.textContent = '对账中...';
    const d = await _liveFetch('/live/reconcile', { method: 'POST' });
    const color = d.critical > 0 ? 'red' : (d.warnings > 0 ? 'orange' : 'green');
    let html = '<div style="color:' + color + ';">总计 ' + d.total + '只 | CRITICAL ' + d.critical + ' | WARN ' + d.warnings + ' | INFO ' + d.infos + '</div>';
    if (d.details) {
      html += d.details.map(x => {
        const lc = x.level === 'CRITICAL' ? 'red' : (x.level === 'WARN' ? 'orange' : 'var(--text-muted)');
        const tag = x.managed ? '' : ' [ETF豁免]';
        return '<div style="color:' + lc + ';">  ' + x.code + ' local=' + x.local + ' qmt=' + x.qmt + ' diff=' + x.diff + ' ' + x.level + tag + '</div>';
      }).join('');
    }
    el.innerHTML = html;
  } catch (e) {
    el.textContent = '对账失败: ' + e.message;
  }
}

async function runExitScan() {
  if (!confirm('确认触发离场扫描?dry-run 模式会模拟 mock 回报。')) return;
  try {
    const d = await _liveFetch('/live/exit-scan', { method: 'POST' });
    alert('离场扫描完成:执行 ' + d.executed + ' 个卖出动作');
    loadLivePositions();
    loadLiveOrders();
  } catch (e) { alert('离场扫描失败: ' + e.message); }
}

async function activateKillSwitch() {
  const reason = prompt('激活 Kill Switch 原因?', '手动激活');
  if (!reason) return;
  if (!confirm('⚠ 确认激活 Kill Switch?将停止所有新单!')) return;
  try {
    await _liveFetch('/live/kill-switch/activate?reason=' + encodeURIComponent(reason), { method: 'POST' });
    alert('Kill Switch 已激活');
    loadLiveStatus();
  } catch (e) { alert('激活失败: ' + e.message); }
}

async function deactivateKillSwitch() {
  if (!confirm('确认解除 Kill Switch?')) return;
  try {
    await _liveFetch('/live/kill-switch/deactivate', { method: 'POST' });
    alert('Kill Switch 已解除');
    loadLiveStatus();
  } catch (e) { alert('解除失败: ' + e.message); }
}

async function replayOrder() {
  const oid = document.getElementById('live-replay-oid').value.trim();
  if (!oid) { alert('请输入 order_id'); return; }
  try {
    const d = await _liveFetch('/live/audit/replay/' + oid);
    document.getElementById('live-replay-out').textContent = JSON.stringify(d, null, 2);
  } catch (e) {
    document.getElementById('live-replay-out').textContent = '回放失败: ' + e.message;
  }
}

// ─── 离场扫描间隔配置 ──────────────────────────────
async function loadScanInterval() {
  try {
    const d = await _liveFetch('/live/config/scan-interval');
    const el = document.getElementById('live-scan-interval');
    if (el) el.value = d.interval_sec;
  } catch (e) { console.warn('加载扫描间隔失败:', e.message); }
}

async function saveScanInterval() {
  const el = document.getElementById('live-scan-interval');
  const msgEl = document.getElementById('live-scan-interval-msg');
  if (!el) return;
  const val = parseFloat(el.value);
  if (isNaN(val) || val < 10 || val > 300) {
    if (msgEl) { msgEl.textContent = '范围 10~300 秒'; msgEl.style.color = '#e74c3c'; }
    return;
  }
  try {
    const d = await _liveFetch('/live/config/scan-interval', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ interval_sec: val }),
    });
    if (msgEl) { msgEl.textContent = '✓ 已保存 (' + d.interval_sec + 's)'; msgEl.style.color = 'green'; }
  } catch (e) {
    if (msgEl) { msgEl.textContent = '保存失败: ' + e.message; msgEl.style.color = '#e74c3c'; }
  }
}
