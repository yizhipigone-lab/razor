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

let _liveCapital = 0;
const fmtCapital = v => '¥' + (Number(v) || 0).toLocaleString(void 0, { maximumFractionDigits: 0 });

async function loadLiveStatus() {
  const setBadge = (id, text, cls) => { const el = document.getElementById(id); if (!el) return; el.innerHTML = '<span class="lt-badge__dot"></span> ' + text; el.className = 'lt-badge ' + cls; };
  const resetBadge = (id, cls) => setBadge(id, '—', cls);
  try {
    const d = await _liveFetch('/live/status');
    _liveCapital = Number(d.live_capital) || 0;
    setBadge('live-conn', d.qmt_connected ? 'QMT 已连接' : 'QMT 未连接', d.qmt_connected ? 'lt-badge--ok' : 'lt-badge--danger');
    const _modeText = d.mode === 'live' ? '实盘·真钱' : (d.mode === 'dry-run' ? '模拟·不下单' : (d.mode || '—'));
    setBadge('live-mode', _modeText, d.mode === 'live' ? 'lt-badge--danger' : 'lt-badge--warn');
    const acc = document.getElementById('live-account'); if (acc) acc.textContent = d.account_id || '—';
    const cap = document.getElementById('live-capital'); if (cap) cap.textContent = fmtCapital(_liveCapital);
    const ks = d.kill_switch || {};
    setBadge('live-ks', ks.activated ? 'KS 已激活' : 'KS 未激活', ks.activated ? 'lt-badge--danger' : 'lt-badge--success');
  } catch (e) {
    setBadge('live-conn', '服务未启动(8001)', 'lt-badge--danger');
    resetBadge('live-mode', 'lt-badge--warn');
    resetBadge('live-ks', 'lt-badge--success');
    const acc = document.getElementById('live-account'); if (acc) acc.textContent = '—';
    const cap = document.getElementById('live-capital'); if (cap) cap.textContent = '—';
    _liveCapital = 0;
  }
}

async function loadLiveAsset() {
  const setErr = (id) => { const el = document.getElementById(id); if (el) { el.textContent = '—'; el.style.color = 'var(--text2)'; } };
  try {
    const d = await _liveFetch('/live/asset');
    const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    const fmt = v => { const n = Number(v) || 0; const sign = n > 0 ? '+' : (n < 0 ? '-' : ''); return sign + '¥' + Math.abs(n).toLocaleString(void 0, { maximumFractionDigits: 0 }); };
    const total = Number(d.total_asset) || 0, mv = Number(d.market_value) || 0, cash = Number(d.cash) || 0, frozen = Number(d.frozen_cash) || 0;
    setText('lt-kpi-total', fmt(total));
    setText('lt-kpi-mv', fmt(mv));
    setText('lt-kpi-cash', fmt(cash));
    if (_liveCapital > 0) {
      const pnlVsCap = total - _liveCapital;
      const pctVsCap = _liveCapital > 0 ? (pnlVsCap / _liveCapital * 100).toFixed(2) : '0';
      const sub = document.getElementById('lt-kpi-total-sub');
      if (sub) { sub.textContent = fmt(pnlVsCap) + ' / ' + pctVsCap + '%'; sub.style.color = pnlVsCap > 0 ? 'var(--red)' : (pnlVsCap < 0 ? 'var(--green)' : 'var(--text2)'); }
    }
    const mvSub = document.getElementById('lt-kpi-mv-sub');
    if (mvSub) mvSub.textContent = '仓位 ' + (total > 0 ? (mv / total * 100).toFixed(1) : 0) + '%';
    const cashSub = document.getElementById('lt-kpi-cash-sub');
    if (cashSub) cashSub.textContent = '冻结 ' + fmt(frozen);
  } catch (e) {
    ['lt-kpi-total','lt-kpi-mv','lt-kpi-cash'].forEach(setErr);
    console.error('live asset 加载失败', e);
  }
}

async function loadLivePositions() {
  const tbody = document.getElementById('live-positions-tbody');
  if (!tbody) return;
  try {
    const data = await _liveFetch('/live/positions');
    if (!data || data.length === 0) {
      tbody.innerHTML = '<tr><td colspan=9 class="ta-c muted">无持仓</td></tr>';
      const sumEl = document.getElementById('lt-positions-summary');
      if (sumEl) { sumEl.textContent = '¥0'; sumEl.style.color = 'var(--text2)'; }
      const pnlEl = document.getElementById('lt-kpi-pnl');
      if (pnlEl) { pnlEl.textContent = '¥0'; pnlEl.style.color = 'var(--text2)'; }
      const pnlLabel = document.getElementById('lt-kpi-pnl-label');
      if (pnlLabel) pnlLabel.textContent = '今日盈亏';
      const pnlSub = document.getElementById('lt-kpi-pnl-sub');
      if (pnlSub) pnlSub.textContent = '过夜按昨收·当日买入按买入价';
      return;
    }
    const _now = new Date();
    const today = _now.getFullYear() + '-' + String(_now.getMonth()+1).padStart(2,'0') + '-' + String(_now.getDate()).padStart(2,'0');
    let totalFloat = 0, totalTodayPnl = 0, hasMissingClose = false;
    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    tbody.innerHTML = data.map(p => {
      const fp = Number(p.float_profit) || 0;
      totalFloat += fp;
      const last = Number(p.last_price) || 0;
      const vol = Number(p.volume) || 0;
      const avgCost = Number(p.avg_cost) || 0;
      const lastClose = Number(p.last_close) || 0;
      const todayBuyVol = Math.min(Number(p.today_buy_volume) || 0, vol);
      const overnightVol = vol - todayBuyVol;
      let todayPnl = 0;
      if (vol > 0 && last > 0) {
        if (todayBuyVol > 0) todayPnl += (last - avgCost) * todayBuyVol;
        if (overnightVol > 0) {
          if (lastClose > 0) todayPnl += (last - lastClose) * overnightVol;
          else hasMissingClose = true;
        }
      }
      totalTodayPnl += todayPnl;
      const tag = p.managed ? '<span class="tc-green">策略</span>' : '<span class="muted">ETF保留</span>';
      const pnlCls = fp > 0 ? 'up' : (fp < 0 ? 'down' : 'muted');
      const missingTitle = (overnightVol > 0 && lastClose <= 0) ? ' title="缺昨收,未计入今日盈亏"' : '';
      return '<tr'+missingTitle+'><td>' + esc(p.code) + '</td><td class="muted">' + esc(p.name || '') + '</td><td>' + vol + '</td><td>' + (Number(p.can_use_volume) || 0) + '</td>' +
        '<td>' + avgCost.toFixed(3) + '</td><td>' + last.toFixed(3) + '</td>' +
        '<td>' + (Number(p.market_value) || 0).toFixed(0) + '</td>' +
        '<td class="' + pnlCls + '">' + fp.toFixed(0) + '</td>' +
        '<td>' + tag + '</td></tr>';
    }).join('');
    const fmtSign = v => v > 0 ? '+' : (v < 0 ? '-' : '');
    const floatText = fmtSign(totalFloat) + '¥' + Math.abs(totalFloat).toLocaleString(void 0, { maximumFractionDigits: 0 });
    const floatColor = totalFloat > 0 ? 'var(--red)' : (totalFloat < 0 ? 'var(--green)' : 'var(--text2)');
    const sumEl = document.getElementById('lt-positions-summary');
    if (sumEl) { sumEl.textContent = floatText; sumEl.style.color = floatColor; }
    const pnlText = fmtSign(totalTodayPnl) + '¥' + Math.abs(totalTodayPnl).toLocaleString(void 0, { maximumFractionDigits: 0 });
    const pnlColor = totalTodayPnl > 0 ? 'var(--red)' : (totalTodayPnl < 0 ? 'var(--green)' : 'var(--text2)');
    const pnlEl = document.getElementById('lt-kpi-pnl');
    if (pnlEl) { pnlEl.textContent = pnlText; pnlEl.style.color = pnlColor; }
    const pnlLabel = document.getElementById('lt-kpi-pnl-label');
    if (pnlLabel) pnlLabel.textContent = '今日盈亏';
    const pnlSub = document.getElementById('lt-kpi-pnl-sub');
    if (pnlSub) pnlSub.textContent = hasMissingClose ? '部分持仓缺昨收,未计入(鼠标悬停看哪只)' : '过夜按昨收·当日买入按买入价';
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan=9 class="tc-red">加载失败(服务未启动?)</td></tr>';
  }
}

async function loadLiveOrders() {
  const tbody = document.getElementById('live-orders-tbody');
  if (!tbody) return;
  try {
    const data = await _liveFetch('/live/orders?limit=50');
    if (!data || data.length === 0) { tbody.innerHTML = '<tr><td colspan=7 style="text-align:center;color:var(--text2);">无委托</td></tr>'; return; }
    const statusMap = { 48: '未报', 49: '待报', 50: '已报', 51: '待撤', 52: '部成待撤', 53: '部撤', 54: '已撤', 55: '部成', 56: '已成', 57: '废单', 255: '未知' };
    tbody.innerHTML = data.map(o => {
      const dirColor = o.direction === 'buy' ? 'var(--red)' : 'var(--green)';
      const stColor = o.status === 56 ? 'var(--green)' : (o.status === 57 ? 'var(--red)' : 'var(--orange)');
      const ts = o.created_at ? o.created_at.slice(11, 19) : '';
      return '<tr><td>' + ts + '</td><td>' + o.code + '</td>' +
        '<td style="color:' + dirColor + ';">' + (o.direction === 'buy' ? '买入' : '卖出') + '</td>' +
        '<td>' + (o.price || 0).toFixed(2) + '</td><td>' + o.volume + '</td>' +
        '<td style="color:' + stColor + ';">' + (statusMap[o.status] || o.status) + '</td>' +
        '<td>' + o.mode + '</td></tr>';
    }).join('');
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan=7 style="color:var(--red);">加载失败</td></tr>';
  }
}

async function runReconcile() {
  const el = document.getElementById('live-reconcile');
  if (!el) return;
  try {
    el.textContent = '对账中...';
    const d = await _liveFetch('/live/reconcile', { method: 'POST' });
    const color = d.critical > 0 ? 'var(--red)' : (d.warnings > 0 ? 'var(--orange)' : 'var(--green)');
    let html = '<div style="color:' + color + ';">总计 ' + d.total + '只 | CRITICAL ' + d.critical + ' | WARN ' + d.warnings + ' | INFO ' + d.infos + '</div>';
    if (d.details) {
      html += d.details.map(x => {
        const lc = x.level === 'CRITICAL' ? 'var(--red)' : (x.level === 'WARN' ? 'var(--orange)' : 'var(--text2)');
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
    const sm = document.getElementById('lt-sum-interval'); if (sm) sm.textContent = d.interval_sec;
  } catch (e) { console.warn('加载扫描间隔失败:', e.message); }
}

async function saveScanInterval() {
  const el = document.getElementById('live-scan-interval');
  const msgEl = document.getElementById('live-scan-interval-msg');
  if (!el) return;
  const val = parseFloat(el.value);
  if (isNaN(val) || val < 10 || val > 300) {
    if (msgEl) { msgEl.textContent = '范围 10~300 秒'; msgEl.style.color = 'var(--red)'; }
    return;
  }
  try {
    const d = await _liveFetch('/live/config/scan-interval', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ interval_sec: val }),
    });
    if (msgEl) { msgEl.textContent = '✓ 已保存 (' + d.interval_sec + 's)'; msgEl.style.color = 'var(--green)'; }
  } catch (e) {
    if (msgEl) { msgEl.textContent = '保存失败: ' + e.message; msgEl.style.color = 'var(--red)'; }
  }
}

// ─── v2: 净值曲线/成交/开关/模式/参数 ──────────────────
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
    if (pts.length === 0) {
      if (_liveEquityChart) { _liveEquityChart.dispose(); _liveEquityChart = null; }
      el.innerHTML = '<div style="text-align:center;color:var(--text2);padding:60px;">暂无净值数据(盘中每 5min 采样)</div>';
      return;
    }
    const _accentColor = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#f0b429';
    const xs = pts.map(p => (p.date || '') + ' ' + (p.time || ''));
    const totals = pts.map(p => p.total);
    if (!_liveEquityChart) _liveEquityChart = echarts.init(el);
    _liveEquityChart.setOption({
      tooltip: { trigger: 'axis' },
      grid: { left: 55, right: 20, top: 20, bottom: 35 },
      xAxis: { type: 'category', data: xs, axisLabel: { fontSize: 10 } },
      yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
      series: [{ name: '总资产', type: 'line', data: totals, smooth: true,
                lineStyle: { width: 2, color: _accentColor },
                itemStyle: { color: _accentColor },
                areaStyle: { opacity: 0.12, color: _accentColor } }],
    }, true);
  } catch (e) {
    if (_liveEquityChart) { _liveEquityChart.dispose(); _liveEquityChart = null; }
    el.innerHTML = '<div style="color:var(--red);padding:20px;">净值加载失败: ' + e.message + '</div>';
  }
}

async function loadLiveDeals() {
  const tbody = document.getElementById('live-deals-tbody');
  if (!tbody) return;
  try {
    const data = await _liveFetch('/live/deals?limit=50');
    if (!data || data.length === 0) { tbody.innerHTML = '<tr><td colspan=6 style="text-align:center;color:var(--text2);">无成交</td></tr>'; return; }
    tbody.innerHTML = data.map(d => {
      const dirColor = d.direction === 'buy' ? 'var(--red)' : 'var(--green)';
      const ts = d.traded_at ? String(d.traded_at).slice(11, 19) : '';
      return '<tr><td>' + ts + '</td><td>' + d.code + '</td>' +
        '<td style="color:' + dirColor + ';">' + (d.direction === 'buy' ? '买入' : '卖出') + '</td>' +
        '<td>' + (d.filled_price || 0).toFixed(2) + '</td><td>' + (d.filled_volume || 0) + '</td>' +
        '<td>' + (d.mode || '') + '</td></tr>';
    }).join('');
  } catch (e) { tbody.innerHTML = '<tr><td colspan=6 style="color:var(--red);">加载失败</td></tr>'; }
}

async function loadLiveSwitches() {
  try {
    const d = await _liveFetch('/live/config/switches');
    const setEl = (id, v) => { const el = document.getElementById(id); if (el) { el.textContent = v ? '开' : '关'; el.style.color = v ? 'var(--green)' : 'var(--text2)'; } };
    setEl('live-buy-switch', d.buy_enabled);
    setEl('live-sell-switch', d.sell_enabled);
    // 折叠区摘要
    const sb = document.getElementById('lt-sum-buy'); if (sb) sb.textContent = d.buy_enabled ? '开' : '关';
    const ss = document.getElementById('lt-sum-sell'); if (ss) ss.textContent = d.sell_enabled ? '开' : '关';
  } catch (e) {
    const setErr = (id) => { const el = document.getElementById(id); if (el) { el.textContent = '—'; el.style.color = 'var(--text2)'; } };
    setErr('live-buy-switch'); setErr('live-sell-switch');
    const sb = document.getElementById('lt-sum-buy'); if (sb) sb.textContent = '—';
    const ss = document.getElementById('lt-sum-sell'); if (ss) ss.textContent = '—';
  }
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
    if (msgEl) { msgEl.textContent = '✓ ' + (which === 'buy' ? '买入' : '卖出') + '已' + (newVal ? '开启' : '关闭'); msgEl.style.color = 'var(--green)'; }
  } catch (e) {
    if (msgEl) { msgEl.textContent = '切换失败: ' + e.message; msgEl.style.color = 'var(--red)'; }
  } finally {
    _liveSwitching = false;
  }
}

async function loadLiveModeDisplay() {
  try {
    const d = await _liveFetch('/live/config/mode');
    const el = document.getElementById('live-mode-display');
    if (el) { el.textContent = d.mode; el.style.color = d.mode === 'live' ? 'var(--red)' : 'var(--orange)'; }
    const sm = document.getElementById('lt-sum-mode'); if (sm) { sm.textContent = d.mode; sm.style.color = d.mode === 'live' ? 'var(--red)' : 'var(--orange)'; }
  } catch (e) {
    const el = document.getElementById('live-mode-display');
    if (el) { el.textContent = '—'; el.style.color = 'var(--text2)'; }
    const sm = document.getElementById('lt-sum-mode'); if (sm) { sm.textContent = '—'; sm.style.color = 'var(--text2)'; }
  }
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
    if (msgEl) { msgEl.textContent = '切换中(最长30s,撤单等终态)...'; msgEl.style.color = 'var(--orange)'; }
    const d = await _liveFetch('/live/config/mode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: target }),
    });
    if (msgEl) { msgEl.textContent = '✓ 已切换 ' + d.old + ' -> ' + d.new; msgEl.style.color = 'var(--green)'; }
    loadLiveModeDisplay(); loadLiveStatus();
  } catch (e) {
    if (msgEl) { msgEl.textContent = '切换失败: ' + e.message; msgEl.style.color = 'var(--red)'; }
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
      '<div style="margin-top:6px;color:var(--text2);font-size:11px;">闸门1~9 在下单时触发检查(详见 risk_gate.py)</div>';
  } catch (e) { el.innerHTML = '<div style="color:var(--red);">参数加载失败</div>'; }
}

// ─── 手风琴折叠交互 ──────────────────────────────
function toggleAccordion(headerEl) {
  const item = headerEl.parentElement;
  if (item && item.classList.contains('lt-accordion__item')) {
    item.classList.toggle('open');
  }
}

// v2: 实盘 tab 激活时加载全部(含新增展示)
async function loadLiveAll() {
  loadLiveStatus(); loadLiveAsset(); loadLivePositions(); loadLiveOrders();
  loadLiveEquity(); loadLiveDeals(); loadLiveSwitches(); loadLiveModeDisplay();
  loadLiveRiskParams(); loadScanInterval();
}
