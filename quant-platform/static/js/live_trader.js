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
    _renderLiveAsset(d);
  } catch (e) {
    ['lt-kpi-total','lt-kpi-mv','lt-kpi-cash'].forEach(setErr);
    console.error('live asset 加载失败', e);
  }
}

// 纯渲染:供 loadLiveAsset 与 live_trader_snapshot 推送共用
function _renderLiveAsset(d) {
  const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  const fmt = v => { const n = Number(v) || 0; const sign = n > 0 ? '+' : (n < 0 ? '-' : ''); return sign + '¥' + Math.abs(n).toLocaleString(void 0, { maximumFractionDigits: 0 }); };
  const total = Number(d.total_asset) || 0, mv = Number(d.market_value) || 0, cash = Number(d.cash) || 0, frozen = Number(d.frozen_cash) || 0;
  window._liveCash = cash; window._liveFrozen = frozen; window._liveAssetOk = true;  // A5:缓存供 applyLiveQuotes 重算总资产(集成M2:_liveAssetOk 标记 asset 就绪)
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
}

async function loadLivePositions() {
  const tbody = document.getElementById('live-positions-tbody');
  if (!tbody) return;
  try {
    const data = await _liveFetch('/live/positions');
    _renderLivePositions(data);
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan=9 class="tc-red">加载失败(服务未启动?)</td></tr>';
  }
}

// 纯渲染:供 loadLivePositions(fetch 路径) 与 live_trader_snapshot 推送(B3)共用
function _renderLivePositions(data) {
  const tbody = document.getElementById('live-positions-tbody');
  if (!tbody) return;
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
    const mvKpi = document.getElementById('lt-kpi-mv');  // 审计M2:空仓重置市值/总资产,防残留旧值
    if (mvKpi) { mvKpi.textContent = '¥0'; mvKpi.style.color = 'var(--text2)'; }
    const totalKpi = document.getElementById('lt-kpi-total');
    if (totalKpi) { totalKpi.textContent = '¥0'; totalKpi.style.color = 'var(--text2)'; }
    const tMv0 = document.getElementById('lt-total-mv');  // 空仓清总计行
    if (tMv0) tMv0.textContent = '¥0';
    const tFp0 = document.getElementById('lt-total-fp');
    if (tFp0) { tFp0.textContent = '¥0'; tFp0.style.color = 'var(--text2)'; }
    return;
  }
  const _now = new Date();
  const today = _now.getFullYear() + '-' + String(_now.getMonth()+1).padStart(2,'0') + '-' + String(_now.getDate()).padStart(2,'0');
  let totalFloat = 0, totalMv = 0, totalTodayPnl = 0, hasMissingClose = false;
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  // 整数千位分隔符(股数/可卖/市值/浮盈);股价列不加(A股基本不上千)
  const fmtInt = v => (Number(v) || 0).toLocaleString(void 0, { maximumFractionDigits: 0 });
  tbody.innerHTML = data.map(p => {
    const fp = Number(p.float_profit) || 0;
    totalFloat += fp;
    totalMv += Number(p.market_value) || 0;
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
    // A1: tr 加 lt-pos-row + data-* 供 market_quotes 推送重算;现价列带 pos-price 兼容 pollLiveQuotes 兜底
    const bareCode = String(p.code || '').split('.')[0];
    return '<tr'+missingTitle+' class="lt-pos-row" data-code="'+esc(bareCode)+'" data-avg="'+avgCost+'" data-vol="'+vol+'" data-todaybuy="'+todayBuyVol+'" data-lastclose="'+lastClose+'"><td>' + esc(p.code) + '</td><td class="muted">' + esc(p.name || '') + '</td><td>' + fmtInt(vol) + '</td><td>' + fmtInt(p.can_use_volume) + '</td>' +
      '<td>' + avgCost.toFixed(3) + '</td><td class="pos-price lt-cur">' + last.toFixed(3) + '</td>' +
      '<td class="lt-mv">' + fmtInt(p.market_value) + '</td>' +
      '<td class="lt-fp ' + pnlCls + '">' + fmtInt(fp) + '</td>' +
      '<td>' + tag + '</td></tr>';
  }).join('');
  const fmtSign = v => v > 0 ? '+' : (v < 0 ? '-' : '');
  const floatText = fmtSign(totalFloat) + '¥' + Math.abs(totalFloat).toLocaleString(void 0, { maximumFractionDigits: 0 });
  const floatColor = totalFloat > 0 ? 'var(--red)' : (totalFloat < 0 ? 'var(--green)' : 'var(--text2)');
  const sumEl = document.getElementById('lt-positions-summary');
  if (sumEl) { sumEl.textContent = floatText; sumEl.style.color = floatColor; }
  // 底部总计行:总市值 + 总浮盈(实时由 applyLiveQuotes 覆盖)
  const tMvEl = document.getElementById('lt-total-mv');
  if (tMvEl) tMvEl.textContent = fmtInt(totalMv);
  const tFpEl = document.getElementById('lt-total-fp');
  if (tFpEl) { tFpEl.textContent = floatText; tFpEl.style.color = floatColor; }
  const pnlText = fmtSign(totalTodayPnl) + '¥' + Math.abs(totalTodayPnl).toLocaleString(void 0, { maximumFractionDigits: 0 });
  const pnlColor = totalTodayPnl > 0 ? 'var(--red)' : (totalTodayPnl < 0 ? 'var(--green)' : 'var(--text2)');
  const pnlEl = document.getElementById('lt-kpi-pnl');
  if (pnlEl) { pnlEl.textContent = pnlText; pnlEl.style.color = pnlColor; }
  const pnlLabel = document.getElementById('lt-kpi-pnl-label');
  if (pnlLabel) pnlLabel.textContent = '今日盈亏';
  const pnlSub = document.getElementById('lt-kpi-pnl-sub');
  if (pnlSub) pnlSub.textContent = hasMissingClose ? '部分持仓缺昨收,未计入(鼠标悬停看哪只)' : '过夜按昨收·当日买入按买入价';
  // A1: 渲染完重发行情订阅(纳入实盘持仓 codes),再用最新行情刷新现价
  if (typeof _sendQuoteSubscribe === 'function') _sendQuoteSubscribe();
  if (typeof applyLiveQuotes === 'function') applyLiveQuotes();
}

async function loadLiveOrders() {
  const tbody = document.getElementById('live-orders-tbody');
  if (!tbody) return;
  try {
    const data = await _liveFetch('/live/orders?limit=50');
    _renderLiveOrders(data);
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan=7 style="color:var(--red);">加载失败</td></tr>';
  }
}
// 纯渲染:供 loadLiveOrders 与 live_trader_snapshot 推送共用
function _renderLiveOrders(data) {
  const tbody = document.getElementById('live-orders-tbody');
  if (!tbody) return;
  if (!data || data.length === 0) { tbody.innerHTML = '<tr><td colspan=7 style="text-align:center;color:var(--text2);">无委托</td></tr>'; return; }
  const statusMap = { 48: '未报', 49: '待报', 50: '已报', 51: '待撤', 52: '部成待撤', 53: '部撤', 54: '已撤', 55: '部成', 56: '已成', 57: '废单', 255: '未知' };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  tbody.innerHTML = data.map(o => {
    const dirColor = o.direction === 'buy' ? 'var(--red)' : 'var(--green)';
    const stColor = o.status === 56 ? 'var(--green)' : (o.status === 57 ? 'var(--red)' : 'var(--orange)');
    const ts = o.created_at ? o.created_at.slice(11, 19) : '';
    return '<tr><td>' + ts + '</td><td>' + esc(o.code) + '</td>' +
      '<td style="color:' + dirColor + ';">' + (o.direction === 'buy' ? '买入' : '卖出') + '</td>' +
      '<td>' + (o.price || 0).toFixed(2) + '</td><td>' + o.volume + '</td>' +
      '<td style="color:' + stColor + ';">' + (statusMap[o.status] || o.status) + '</td>' +
      '<td>' + esc(o.mode) + '</td></tr>';
  }).join('');
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
    const pts0 = d.points || [];
    // C1: 盘中不显示当日点;仅当今日已有收盘点(time>='15:00',EOD 15:01 写入)才显示今日
    const _enow = new Date();
    const _today = _enow.getFullYear() + '-' + String(_enow.getMonth()+1).padStart(2,'0') + '-' + String(_enow.getDate()).padStart(2,'0');
    const _todayPts = pts0.filter(p => p.date === _today);
    const _closed = _todayPts.length > 0 && (_todayPts[_todayPts.length - 1].time || '') >= '15:00';
    let pts = _closed ? pts0 : pts0.filter(p => p.date !== _today);
    if (pts.length === 0) pts = pts0;  // 边界:周一盘中全过滤→回退,宁显盘中不空白
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
    el.innerHTML = '<div style="color:var(--red);padding:20px;"></div>';
    el.firstChild.textContent = '净值加载失败: ' + (e.message || '');
  }
}

async function loadLiveDeals() {
  const tbody = document.getElementById('live-deals-tbody');
  if (!tbody) return;
  try {
    const data = await _liveFetch('/live/deals?limit=50');
    _renderLiveDeals(data);
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan=6 style="color:var(--red);">加载失败</td></tr>';
  }
}
// 纯渲染:供 loadLiveDeals 与 live_trader_snapshot 推送共用
function _renderLiveDeals(data) {
  const tbody = document.getElementById('live-deals-tbody');
  if (!tbody) return;
  if (!data || data.length === 0) { tbody.innerHTML = '<tr><td colspan=6 style="text-align:center;color:var(--text2);">无成交</td></tr>'; return; }
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  tbody.innerHTML = data.map(d => {
    const dirColor = d.direction === 'buy' ? 'var(--red)' : 'var(--green)';
    const ts = d.traded_at ? String(d.traded_at).slice(11, 19) : '';
    return '<tr><td>' + ts + '</td><td>' + esc(d.code) + '</td>' +
      '<td style="color:' + dirColor + ';">' + (d.direction === 'buy' ? '买入' : '卖出') + '</td>' +
      '<td>' + (d.filled_price || 0).toFixed(2) + '</td><td>' + (d.filled_volume || 0) + '</td>' +
      '<td>' + esc(d.mode || '') + '</td></tr>';
  }).join('');
}

async function loadLiveSwitches() {
  try {
    const d = await _liveFetch('/live/config/switches');
    const setEl = (id, v) => { const el = document.getElementById(id); if (el) { el.textContent = v ? '开' : '关'; el.style.color = v ? 'var(--green)' : 'var(--text2)'; } };
    setEl('live-buy-switch', d.buy_enabled);
    setEl('live-sell-switch', d.sell_enabled);
    setEl('live-auto-buy-switch', d.auto_buy_enabled);
    // 折叠区摘要
    const sb = document.getElementById('lt-sum-buy'); if (sb) sb.textContent = d.buy_enabled ? '开' : '关';
    const ss = document.getElementById('lt-sum-sell'); if (ss) ss.textContent = d.sell_enabled ? '开' : '关';
    const sab = document.getElementById('lt-sum-auto-buy'); if (sab) sab.textContent = d.auto_buy_enabled ? '开' : '关';
  } catch (e) {
    const setErr = (id) => { const el = document.getElementById(id); if (el) { el.textContent = '—'; el.style.color = 'var(--text2)'; } };
    setErr('live-buy-switch'); setErr('live-sell-switch'); setErr('live-auto-buy-switch');
    const sb = document.getElementById('lt-sum-buy'); if (sb) sb.textContent = '—';
    const ss = document.getElementById('lt-sum-sell'); if (ss) ss.textContent = '—';
    const sab = document.getElementById('lt-sum-auto-buy'); if (sab) sab.textContent = '—';
  }
}

let _liveSwitching = false;
async function toggleLiveSwitch(which) {
  if (_liveSwitching) return;
  _liveSwitching = true;
  const msgEl = document.getElementById('live-switch-msg');
  const labels = { buy: '买入', sell: '卖出', auto: '自动选股' };
  const keys = { buy: 'buy_enabled', sell: 'sell_enabled', auto: 'auto_buy_enabled' };
  try {
    const cur = await _liveFetch('/live/config/switches');
    const key = keys[which];
    const newVal = !cur[key];
    if (!confirm('确认 ' + (labels[which] || which) + ' 开关 -> ' + (newVal ? '开' : '关') + '?')) return;
    await _liveFetch('/live/config/switches', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [key]: newVal }),
    });
    await loadLiveSwitches();
    if (msgEl) { msgEl.textContent = '✓ ' + (labels[which] || which) + '已' + (newVal ? '开启' : '关闭'); msgEl.style.color = 'var(--green)'; }
  } catch (e) {
    if (msgEl) { msgEl.textContent = '切换失败: ' + e.message; msgEl.style.color = 'var(--red)'; }
  } finally {
    _liveSwitching = false;
  }
}

// ── 自动选股时间配置 ───────────────────────────────────
async function loadAutoBuyTime() {
  try {
    const d = await _liveFetch('/live/config/auto-buy-time');
    const el = document.getElementById('lt-auto-buy-time');
    if (el && d.auto_buy_time) el.value = d.auto_buy_time;
  } catch (e) { /* ignore */ }
}

async function saveAutoBuyTime() {
  const el = document.getElementById('lt-auto-buy-time');
  const msgEl = document.getElementById('lt-auto-buy-time-msg');
  if (!el) return;
  const t = el.value;
  if (!t) return;
  try {
    const d = await _liveFetch('/live/config/auto-buy-time', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auto_buy_time: t }),
    });
    if (msgEl) { msgEl.textContent = '✓ 已保存 (' + d.auto_buy_time + ')'; msgEl.style.color = 'var(--green)'; }
  } catch (e) {
    if (msgEl) { msgEl.textContent = '保存失败: ' + e.message; msgEl.style.color = 'var(--red)'; }
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

// ─── 单只占本金比例配置 ──────────────────────────────
async function loadLiveBuyRatio() {
  try {
    const d = await _liveFetch('/live/config/buy-ratio');
    const pct = (Number(d.buy_position_ratio) * 100).toFixed(1);
    const el = document.getElementById('lt-buy-ratio');
    if (el) el.value = pct;
    const sm = document.getElementById('lt-sum-ratio'); if (sm) sm.textContent = pct + '%';
  } catch (e) { console.warn('加载买入比例失败:', e.message); }
}

async function saveLiveBuyRatio() {
  const el = document.getElementById('lt-buy-ratio');
  const msgEl = document.getElementById('lt-buy-ratio-msg');
  if (!el) return;
  const pct = parseFloat(el.value);
  if (isNaN(pct) || pct <= 0 || pct > 100) {
    if (msgEl) { msgEl.textContent = '范围 0~100%(不含0)'; msgEl.style.color = 'var(--red)'; }
    return;
  }
  const ratio = pct / 100;
  try {
    const d = await _liveFetch('/live/config/buy-ratio', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ buy_position_ratio: ratio }),
    });
    const shown = (Number(d.buy_position_ratio) * 100).toFixed(1);
    if (msgEl) { msgEl.textContent = '✓ 已保存 (' + shown + '%)'; msgEl.style.color = 'var(--green)'; }
    const sm = document.getElementById('lt-sum-ratio'); if (sm) sm.textContent = shown + '%';
  } catch (e) {
    if (msgEl) { msgEl.textContent = '保存失败: ' + e.message; msgEl.style.color = 'var(--red)'; }
  }
}

// v2: 实盘 tab 激活时加载全部(含新增展示)
async function loadLiveAll() {
  loadLiveStatus(); loadLiveAsset(); loadLivePositions(); loadLiveOrders();
  loadLiveEquity(); loadLiveDeals(); loadLiveSwitches(); loadLiveModeDisplay();
  loadLiveRiskParams(); loadScanInterval(); loadLiveBuyRatio(); loadAutoBuyTime();
  loadRiskMonitor();
}

// A3: 用 market_quotes 缓存(window._lastQuotes)实时刷新实盘持仓现价/市值/浮盈/汇总/KPI。
// 口径与后端 store.refresh_quotes 完全一致:float_profit=(price-avg)*vol, market_value=price*vol。
// 供 main.js 的 market_quotes 分支 + _renderLivePositions(重渲染后) 调用。
// 审计H1:仅当全部持仓行都拿到行情(100%覆盖)才写聚合 KPI,否则保留 _renderLiveAsset 后端值,
//         防止停牌/订阅未到时部分和把 total_asset 打成错误值。
function applyLiveQuotes() {
  try {
    const quotes = window._lastQuotes;
    if (!quotes) return;
    const rows = document.querySelectorAll('#live-positions-tbody tr.lt-pos-row');
    if (!rows.length) return;
    const fmtInt = v => (Number(v) || 0).toLocaleString(void 0, { maximumFractionDigits: 0 });
    const fmtSign = v => v > 0 ? '+' : (v < 0 ? '-' : '');
    const fmtMoney = v => fmtSign(v) + '¥' + Math.abs(v).toLocaleString(void 0, { maximumFractionDigits: 0 });
    let totalFloat = 0, totalMv = 0, totalTodayPnl = 0, hasMissingClose = false, matched = 0;
    rows.forEach(tr => {
      const bareCode = tr.getAttribute('data-code');
      if (!bareCode) return;
      const q = (typeof findQuote === 'function') ? findQuote(quotes, bareCode) : null;  // 审计M3:去掉 quotes[bareCode] 死分支(键带后缀,裸码恒 undefined)
      if (!q) return;
      const price = Number(q.price || q.lastPrice || 0);
      if (!(price > 0)) return;
      matched++;  // H1:覆盖率计数
      const avg = Number(tr.getAttribute('data-avg') || 0);
      const vol = Number(tr.getAttribute('data-vol') || 0);
      const todayBuy = Number(tr.getAttribute('data-todaybuy') || 0);
      const dataLastClose = Number(tr.getAttribute('data-lastclose') || 0);
      // 昨收优先取行情推送的 lastClose(更新),其次 data-lastclose(渲染时后端值)
      const dayClose = Number(q.lastClose || q.preClose || 0) || dataLastClose;
      const mv = price * vol;
      const fp = (price - avg) * vol;
      totalFloat += fp; totalMv += mv;
      let todayPnl = 0;
      if (vol > 0) {
        if (todayBuy > 0) todayPnl += (price - avg) * todayBuy;
        const overnight = vol - todayBuy;
        if (overnight > 0) {
          if (dayClose > 0) todayPnl += (price - dayClose) * overnight;
          else hasMissingClose = true;
        }
      }
      totalTodayPnl += todayPnl;
      // 逐行现价/市值/浮盈:未命中行保留上一次值(优雅降级)
      const curEl = tr.querySelector('.lt-cur');
      if (curEl) curEl.textContent = price.toFixed(3);
      const mvEl = tr.querySelector('.lt-mv');
      if (mvEl) mvEl.textContent = fmtInt(mv);
      const fpEl = tr.querySelector('.lt-fp');
      if (fpEl) { fpEl.textContent = fmtInt(fp); fpEl.className = 'lt-fp ' + (fp > 0 ? 'up' : (fp < 0 ? 'down' : 'muted')); }
    });
    // H1:仅 100% 覆盖才写聚合 KPI(防部分和覆盖后端正确值),否则只更新逐行现价
    if (matched !== rows.length) return;
    const sumEl = document.getElementById('lt-positions-summary');
    if (sumEl) { sumEl.textContent = fmtMoney(totalFloat); sumEl.style.color = totalFloat > 0 ? 'var(--red)' : (totalFloat < 0 ? 'var(--green)' : 'var(--text2)'); }
    // 底部总计行(实时):总市值 + 总浮盈
    const tMvEl = document.getElementById('lt-total-mv');
    if (tMvEl) tMvEl.textContent = fmtInt(totalMv);
    const tFpEl = document.getElementById('lt-total-fp');
    if (tFpEl) { tFpEl.textContent = fmtMoney(totalFloat); tFpEl.style.color = totalFloat > 0 ? 'var(--red)' : (totalFloat < 0 ? 'var(--green)' : 'var(--text2)'); }
    const mvKpi = document.getElementById('lt-kpi-mv');
    if (mvKpi) { mvKpi.textContent = fmtMoney(totalMv); mvKpi.style.color = 'var(--text2)'; }
    // 集成M2:总资产需 asset 已就绪(_liveAssetOk),否则 cash=0 低估;未就绪时保留 _renderLiveAsset 后端值
    if (window._liveAssetOk) {
      const cash = Number(window._liveCash || 0), frozen = Number(window._liveFrozen || 0);
      const totalAsset = totalMv + cash + frozen;
      const totalKpi = document.getElementById('lt-kpi-total');
      if (totalKpi) { totalKpi.textContent = fmtMoney(totalAsset); totalKpi.style.color = 'var(--text2)'; }
      if (_liveCapital > 0) {
        const pnlVsCap = totalAsset - _liveCapital;
        const pctVsCap = _liveCapital > 0 ? (pnlVsCap / _liveCapital * 100).toFixed(2) : '0';
        const sub = document.getElementById('lt-kpi-total-sub');
        if (sub) { sub.textContent = fmtMoney(pnlVsCap) + ' / ' + pctVsCap + '%'; sub.style.color = pnlVsCap > 0 ? 'var(--red)' : (pnlVsCap < 0 ? 'var(--green)' : 'var(--text2)'); }
        const mvSub = document.getElementById('lt-kpi-mv-sub');
        if (mvSub) mvSub.textContent = '仓位 ' + (totalAsset > 0 ? (totalMv / totalAsset * 100).toFixed(1) : 0) + '%';
      }
    }
    const pnlKpi = document.getElementById('lt-kpi-pnl');
    if (pnlKpi) { pnlKpi.textContent = fmtMoney(totalTodayPnl); pnlKpi.style.color = totalTodayPnl > 0 ? 'var(--red)' : (totalTodayPnl < 0 ? 'var(--green)' : 'var(--text2)'); }
    const pnlSub = document.getElementById('lt-kpi-pnl-sub');
    if (pnlSub) pnlSub.textContent = hasMissingClose ? '部分持仓缺昨收,未计入' : '过夜按昨收·当日买入按买入价';
  } catch (e) {
    console.warn('[applyLiveQuotes]', e);  // 集成M3:防 500ms 刷错中断 market_quotes 分支
  }
}
// 暴露给 main.js:market_quotes 分支调 applyLiveQuotes;switchTab 切回实盘 tab 调 resizeLiveEquityChart(D,F1)
window.applyLiveQuotes = applyLiveQuotes;
window.resizeLiveEquityChart = function () { if (_liveEquityChart) _liveEquityChart.resize(); };

// ── 风控监控（与 alerts.js 轮询模式完全对齐） ─────────────────────────
let _riskTimer = null;

async function loadRiskMonitor() {
  const el = document.getElementById('risk-monitor-body');
  if (!el) return;
  try {
    const d = await _liveFetch('/live/config/risk-status');
    renderRiskMonitor(d);
  } catch (e) {
    if (el) el.innerHTML = '<div style="color:var(--red);font-size:12px">加载失败</div>';
  }
}

function renderRiskMonitor(d) {
  const el = document.getElementById('risk-monitor-body');
  const tsEl = document.getElementById('risk-monitor-updated');
  if (!el) return;
  if (tsEl) {
    const t = d.updated_at ? d.updated_at.replace('T', ' ').substring(11, 19) : '—';
    tsEl.textContent = '刷新 ' + t;
  }
  const positions = d.positions || [];
  // ATR 模式提示
  const atrNote = d.risk_params && d.risk_params.atr_note;
  const noteHtml = atrNote
    ? '<div class="muted fs-xs mb-2" style="color:var(--yellow)">' + escHtml(atrNote) + '</div>'
    : '';
  if (!positions.length) {
    el.innerHTML = noteHtml + '<div class="muted fs-xs">暂无持仓</div>';
    return;
  }
  const STATUS_COLOR = { danger: 'var(--red)', warning: 'var(--yellow)', safe: 'var(--green)' };
  let html = noteHtml + '<table class="data-table" style="font-size:12px"><thead><tr>' +
    '<th>代码</th><th>现价</th><th>累计</th><th>进度</th><th>状态</th><th>详情</th></tr></thead><tbody>';
  for (const pos of positions) {
    const color = STATUS_COLOR[pos.global_status] || 'var(--text2)';
    const globalLabel = { danger: '⚠️ 危险', warning: '⚡ 激活', safe: '✓ 安全' }[pos.global_status] || '—';
    // 进度条：选 global_status 最高的 risk_item，跳过 FD（二元触发，无进度条概念）
    const topItem = (pos.risk_items || [])
      .filter(it => it.type !== 'FD')
      .sort((a, b) => {
        const p = { danger: 3, warning: 2, safe: 1 };
        return (p[b.status] || 0) - (p[a.status] || 0);
      })[0];
    let barHtml = '—';
    if (topItem) {
      const pct = topItem.remaining <= 0 ? 100
        : Math.min(topItem.remaining / topItem.budget * 100, 95);
      const barColor = topItem.status === 'danger' ? 'var(--red)'
        : topItem.status === 'warning' ? 'var(--yellow)'
        : topItem.remaining > 0 ? 'var(--yellow)'
        : 'var(--green)';
      barHtml = '<div style="background:var(--bg2);border-radius:3px;height:6px;width:100%">' +
        '<div style="width:' + pct + '%;background:' + barColor + ';height:6px;border-radius:3px"></div>' +
        '</div>';
    }
    // 合并所有 risk_items message 为一行摘要
    const msgs = (pos.risk_items || []).map(function (it) {
      if (it.type === 'HS') return it.message;
      if (it.type === 'TR') return it.message;
      if (it.type === 'TF') return it.message;
      if (it.type === 'FD') return it.message;
      if (it.type === 'TC') return it.message;
      if (it.type && it.type.startsWith('TP')) return it.message;
      return '';
    }).filter(Boolean).join('；');
    html += '<tr>' +
      '<td>' + escHtml(pos.code) + (pos.name ? ' ' + escHtml(pos.name) : '') + '</td>' +
      '<td>' + (pos.current_price > 0 ? pos.current_price.toFixed(2) : '—') + '</td>' +
      '<td style="color:' + (pos.profit_rate >= 0 ? 'var(--red)' : 'var(--green)') + '">' +
        (pos.profit_rate > 0 ? '+' : '') + (pos.profit_rate || 0).toFixed(1) + '%</td>' +
      '<td>' + barHtml + '</td>' +
      '<td style="color:' + color + ';font-weight:600">' + globalLabel + '</td>' +
      '<td style="color:var(--text2);font-size:11px">' + escHtml(msgs) + '</td>' +
      '</tr>';
  }
  html += '</tbody></table>';
  el.innerHTML = html;
}

// 对齐 alerts.js 模式：暴露 start/stop 给 main.js 的 switchTab 中央调度
window.startRiskPolling = function () {
  stopRiskPolling();
  loadRiskMonitor();
  _riskTimer = setInterval(loadRiskMonitor, 15000);
};
window.stopRiskPolling = function () {
  if (_riskTimer) { clearInterval(_riskTimer); _riskTimer = null; }
};
