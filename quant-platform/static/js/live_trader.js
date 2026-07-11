// ─── Live Trader 实盘交易 ─────────────────────────────────
// live_trader 在 Windows 端 8001,前端浏览器直连(同机)
const LIVE_API = 'http://' + (window.location.hostname || '127.0.0.1') + ':8001';

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
  const setBadge = (id, text, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = '<span class="lt-badge__dot"></span> ' + text;
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
  // v2审计H3: 已由 loadLiveRiskParams 替代(展示 risk 段实际参数,非写死闸门文案)。保留空壳防旧 onclick 报错。
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
        return '<div style="color:' + lc + ';">  ' + x.code + ' local=' + x.local_volume + ' qmt=' + x.qmt_volume + ' diff=' + x.diff_volume + ' ' + x.level + tag + '</div>';
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

// ─── v2: 净值曲线/成交/开关/模式/参数 ──────────────────
let _liveEquityChart = null;
async function loadLiveEquity() {
  const el = document.getElementById('live-equity-chart');
  if (!el) return;
  try {
    const d = await _liveFetch('/live/equity?days=1');
    const pts = d.points || [];
    if (pts.length === 0) { el.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:60px;">暂无净值数据(盘中每 5min 采样)</div>'; return; }
    const xs = pts.map(p => (p.date || '') + ' ' + (p.time || ''));
    const totals = pts.map(p => p.total);
    if (!_liveEquityChart) _liveEquityChart = echarts.init(el);
    _liveEquityChart.setOption({
      tooltip: { trigger: 'axis' },
      grid: { left: 55, right: 20, top: 20, bottom: 35 },
      xAxis: { type: 'category', data: xs, axisLabel: { fontSize: 10 } },
      yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
      series: [{ name: '总资产', type: 'line', data: totals, smooth: true,
                lineStyle: { width: 2 }, areaStyle: { opacity: 0.1 } }],
    }, true);
  } catch (e) { el.innerHTML = '<div style="color:red;padding:20px;">净值加载失败: ' + e.message + '</div>'; }
}

async function loadLiveDeals() {
  const tbody = document.getElementById('live-deals-tbody');
  if (!tbody) return;
  try {
    const data = await _liveFetch('/live/deals?limit=50');
    if (!data || data.length === 0) { tbody.innerHTML = '<tr><td colspan=6 style="text-align:center;color:var(--text-muted);">无成交</td></tr>'; return; }
    tbody.innerHTML = data.map(d => {
      const dirColor = d.direction === 'buy' ? 'red' : 'green';
      const ts = d.traded_at ? String(d.traded_at).slice(11, 19) : '';
      return '<tr><td>' + ts + '</td><td>' + d.code + '</td>' +
        '<td style="color:' + dirColor + ';">' + (d.direction === 'buy' ? '买入' : '卖出') + '</td>' +
        '<td>' + (d.filled_price || 0).toFixed(2) + '</td><td>' + (d.filled_volume || 0) + '</td>' +
        '<td>' + (d.mode || '') + '</td></tr>';
    }).join('');
  } catch (e) { tbody.innerHTML = '<tr><td colspan=6 style="color:red;">加载失败</td></tr>'; }
}

async function loadLiveSwitches() {
  try {
    const d = await _liveFetch('/live/config/switches');
    const setEl = (id, v) => { const el = document.getElementById(id); if (el) { el.textContent = v ? '开' : '关'; el.style.color = v ? 'green' : 'var(--text-muted)'; } };
    setEl('live-buy-switch', d.buy_enabled);
    setEl('live-sell-switch', d.sell_enabled);
  } catch (e) { /* 静默 */ }
}

let _liveSwitching = false;
async function toggleLiveSwitch(which) {
  if (_liveSwitching) return;  // v2审计中-6: 防抖
  const msgEl = document.getElementById('live-switch-msg');
  try {
    const cur = await _liveFetch('/live/config/switches');
    const key = which === 'buy' ? 'buy_enabled' : 'sell_enabled';
    const newVal = !cur[key];
    if (!confirm('确认 ' + (which === 'buy' ? '买入' : '卖出') + ' 开关 -> ' + (newVal ? '开' : '关') + '?')) return;
    _liveSwitching = true;
    await _liveFetch('/live/config/switches', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [key]: newVal }),
    });
    await loadLiveSwitches();
    if (msgEl) { msgEl.textContent = '✓ ' + (which === 'buy' ? '买入' : '卖出') + '已' + (newVal ? '开启' : '关闭'); msgEl.style.color = 'green'; }
  } catch (e) {
    if (msgEl) { msgEl.textContent = '切换失败: ' + e.message; msgEl.style.color = 'red'; }
  } finally {
    _liveSwitching = false;
  }
}

async function loadLiveModeDisplay() {
  try {
    const d = await _liveFetch('/live/config/mode');
    const el = document.getElementById('live-mode-display');
    if (el) { el.textContent = d.mode; el.style.color = d.mode === 'live' ? 'red' : 'orange'; }
  } catch (e) { /* 静默 */ }
}

async function switchLiveMode() {
  if (_liveSwitching) return;  // v2审计高-2: 防抖(重复点击反向误切)
  const msgEl = document.getElementById('live-switch-msg');
  try {
    const cur = await _liveFetch('/live/config/mode');
    const target = cur.mode === 'live' ? 'dry-run' : 'live';
    const warn = target === 'live' ? '⚠ 将开始真钱交易!' : '将撤所有在途单后切模拟(等终态,超时阻断)';
    if (!confirm('确认切换模式 ' + cur.mode + ' -> ' + target + '?\n' + warn)) return;
    _liveSwitching = true;
    if (msgEl) { msgEl.textContent = '切换中(最长30s,撤单等终态)...'; msgEl.style.color = 'orange'; }
    const d = await _liveFetch('/live/config/mode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: target }),
    });
    if (msgEl) { msgEl.textContent = '✓ 已切换 ' + d.old + ' -> ' + d.new; msgEl.style.color = 'green'; }
    loadLiveModeDisplay(); loadLiveStatus();
  } catch (e) {
    if (msgEl) { msgEl.textContent = '切换失败: ' + e.message; msgEl.style.color = 'red'; }
  } finally {
    _liveSwitching = false;
  }
}

async function loadLiveRiskParams() {
  const el = document.getElementById('live-gates');
  if (!el) return;
  try {
    const d = await _liveFetch('/live/config/risk-params');
    const p = d.params || {};
    const tiers = (p.take_profit_tiers || []).map((t, i) =>
      'TP' + (i + 1) + ' +' + ((t.profit_pct || 0) * 100).toFixed(1) + '% 卖' + ((t.sell_ratio || 0) * 100).toFixed(0) + '%').join(' | ');
    // v2审计中-8: 空值显示"未配置",不误导为 0%
    const pct = (v) => (v === null || v === undefined ? '未配置' : (Number(v)) + '%');
    const fdProfit = pct(p.first_day_exit_min_profit);
    el.innerHTML =
      '<div style="color:var(--text2);font-size:11px;margin-bottom:4px;">参数来自 risk 段,实盘与模拟盘共用(改此参数影响实盘真钱)</div>' +
      '<div>HS 硬止损 ' + pct(p.hard_stop_loss_pct) + '</div>' +
      '<div>' + (tiers || 'TP 未配置') + '</div>' +
      '<div>TR 移动止盈 激活 ' + pct(p.trailing_stop_activate_pct) + ' 回撤 ' + pct(p.trailing_stop_drawdown_pct) + '</div>' +
      '<div>TC 时间退出 ' + (p.time_exit_days ?? '—') + '天 盈利>' + pct(p.time_exit_min_profit_pct) + '</div>' +
      '<div>TF 强制退出 ' + (p.time_exit_force_days ?? '—') + '天</div>' +
      '<div>FD 首日离场 盈利>' + fdProfit + ' ' + (p.first_day_exit_days ?? '—') + '天</div>' +
      '<div style="margin-top:6px;color:var(--text-muted);font-size:11px;">闸门1~9 在下单时触发检查(详见 risk_gate.py)</div>';
  } catch (e) { el.innerHTML = '<div style="color:red;">参数加载失败</div>'; }
}

// v2: 实盘 tab 激活时加载全部(含新增展示)
async function loadLiveAll() {
  loadLiveStatus(); loadLiveAsset(); loadLivePositions(); loadLiveOrders();
  loadLiveEquity(); loadLiveDeals(); loadLiveSwitches(); loadLiveModeDisplay();
  loadLiveRiskParams(); loadScanInterval();
}
