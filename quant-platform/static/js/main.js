// ─── WebSocket ────────────────────────────────────────────────
let ws, wsReady = false;
const wsDot = document.getElementById('ws-dot');

function connectWS() {
  ws = new WebSocket('ws://' + location.host + '/ws');
  ws.onopen = () => { wsReady = true; wsDot.classList.add('connected'); addLog('info', 'WebSocket 已连接'); };
  ws.onclose = () => { wsReady = false; wsDot.classList.remove('connected'); setTimeout(connectWS, 3000); };
  ws.onerror = () => addLog('error', 'WebSocket 断开，重连中...');
  ws.onmessage = (e) => handleWS(JSON.parse(e.data));
}
connectWS();

// 指数 HTTP 兜底加载（WebSocket 无数据时）
function setIndex(id, data) {
  if (!data || typeof data !== 'object') return;
  const price = document.getElementById(id + '-price');
  const pct = document.getElementById(id + '-pct');
  const currentPrice = parseFloat(data.lastPrice || data.price || 0);
  const lastClose = parseFloat(data.lastClose || data.preClose || 0);
  // 优先使用直接字段，否则根据 lastPrice/lastClose 计算
  let changePercent = parseFloat(data.priceChangeRatio || data.change_pct || NaN);
  if (isNaN(changePercent) && lastClose > 0) {
    changePercent = (currentPrice - lastClose) / lastClose * 100;
  }
  if (isNaN(changePercent)) changePercent = 0;
  // 只在数据有效时更新（避免 HTTP 兜底返回 0 覆盖 WebSocket 正确值）
  if (price && currentPrice > 0) price.textContent = currentPrice.toFixed(2);
  if (pct) {
    pct.textContent = (changePercent > 0 ? '+' : '') + changePercent.toFixed(2) + '%';
    pct.className = changePercent >= 0 ? 'up' : 'down';
  }
}

// 大盘指数只用 WebSocket 实时推送更新，不再 HTTP 轮询
// HTTP 兜底仅在前 3 秒执行一次（WebSocket 尚未连接时）
async function refreshMarketBar() {
  if (window._mqCount && window._mqCount > 0) return; // WebSocket 已推送过，跳过
  try {
    const r = await fetch('/api/market/quotes');
    const d = await r.json();
    if (d.indices) {
      const idx = d.indices;
      if (idx['000001.SH']) setIndex('sh', idx['000001.SH']);
      if (idx['399001.SZ']) setIndex('sz', idx['399001.SZ']);
      if (idx['399006.SZ']) setIndex('cy', idx['399006.SZ']);
      if (idx['000905.SH']) setIndex('zz500', idx['000905.SH']);
      if (idx['000510.SH']) setIndex('a500', idx['000510.SH']);
    }
  } catch(e) {}
}
setTimeout(refreshMarketBar, 1000);

// ─── Security Utilities ─────────────────────────────────────────
function escHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = String(str);
  return div.innerHTML;
}
function fmtPrice(v) {
  if (v == null || v === '') return '--';
  var n = Number(v);
  return isNaN(n) ? String(v) : n.toFixed(2);
}
async function safeFetch(url, opts) {
  try { return await fetch(url, opts); }
  catch(e) { console.error('Fetch failed:', url, e); throw e; }
}
// ─── Constants & UI State ─────────────────────────────────────
let scanExchanges = new Set(['SH', 'SZ']);
let btExchanges = new Set(['SH', 'SZ']);
 
function toggleExchChip(type, val) {
  const isBt = type === 'bt';
  const set = isBt ? btExchanges : scanExchanges;
  const id = isBt ? `bt-chip-${val.toLowerCase()}` : `exc-${val}`;
  const el = document.getElementById(id);
  if (!el) return;

  if (set.has(val)) {
    set.delete(val);
    el.classList.remove('active');
  } else {
    set.add(val);
    el.classList.add('active');
  }
}

async function loadSimTraderStatus() {
  try {
    const r = await fetch('/api/sim-trader/status').then(r => r.json());
    if (r.status === 'ok') {
      document.getElementById('sim-equity').textContent = Math.round(r.equity || 0).toLocaleString();
      document.getElementById('sim-cash').textContent = Math.round(r.cash || 0).toLocaleString();
      const pnlEl = document.getElementById('sim-total-pnl-val');
      if (pnlEl && r.total_unrealized_pnl != null) {
        pnlEl.textContent = (r.total_unrealized_pnl >= 0 ? '+' : '') + Math.round(r.total_unrealized_pnl).toLocaleString();
        pnlEl.style.color = r.total_unrealized_pnl >= 0 ? 'var(--red)' : 'var(--green)';
      }
      const totalBuys = (r.trade_count || 0) + (r.position_count || 0);
      document.getElementById('sim-counts').textContent = (r.position_count || 0) + ' / ' + totalBuys;
      document.getElementById('sim-losses').textContent = r.consecutive_losses || 0;
      document.getElementById('sim-paused').textContent = r.paused ? '暂停中' : '正常';
      document.getElementById('sim-today').textContent = r.today || '--';
      // 持仓表格
      const tbody = document.getElementById('sim-pos-tbody');
      const positions = r.positions || [];
      if (tbody) {
        tbody.innerHTML = positions.length > 0
          ? positions.sort(function(a, b) { return (b.entry_date || '').localeCompare(a.entry_date || ''); }).map(p =>
            '<tr class="pos-row" data-code="' + p.code + '" data-entry="' + (p.entry_price || 0) + '" data-shares="' + (p.remaining || p.shares || 0) + '"><td>' + p.code + '</td><td>' + (p.name || '') + '</td><td>' + (p.entry_date || '--') + '</td><td>' + (p.entry_price || 0).toFixed(2) + '</td><td class="pos-price">' + (p.current_price || 0).toFixed(2) + '</td><td class="pos-pct" style="color:' + ((p.profit_pct || 0) >= 0 ? 'var(--red)' : 'var(--green)') + '">' + ((p.profit_pct || 0) >= 0 ? '+' : '') + (p.profit_pct || 0).toFixed(2) + '%</td><td>' + (p.remaining || p.shares || 0) + '</td><td class="pos-mv">' + Number(p.market_value || 0).toLocaleString() + '</td></tr>'
          ).join('')
          : '<tr><td colspan="8" style="text-align:center;color:var(--text2)">暂无持仓</td></tr>';
      }
    }
    // 订阅持仓股票的实时行情
    if (window.marketUpdater) window.marketUpdater.resubscribe();
  } catch(e) { console.error('loadSimTraderStatus:', e); }
}
function renderStages(tiers) { /* stub */ }
async function initLogDates() {
  try {
    const r = await fetch('/api/sim-trader/log-dates').then(r => r.json());
    const sel = document.getElementById('sim-log-date');
    if (sel && r.dates) {
      sel.innerHTML = '<option value="">最新</option>' + r.dates.map(d => '<option value="' + d + '">' + d + '</option>').join('');
    }
  } catch(e) {}
}
async function loadSimLogs() {
  const logDate = document.getElementById('sim-log-date')?.value || '';
  const list = document.getElementById('sim-log-list');
  if (!list) return;
  try {
    const r = await fetch('/api/sim-trader/logs?log_date=' + logDate + '&limit=200').then(r => r.json());
    if (r.entries && r.entries.length > 0) {
      list.innerHTML = r.entries.map(e => {
        const a = e.action || '';
        const ts = (e.time ? (e.date||'').substring(5)+' '+e.time : (e.date||'').substring(5)) || '';
        if (a === 'buy') {
          return '<div style="padding:2px 0;border-bottom:1px solid rgba(255,255,255,0.05);font-size:11px"><span style="color:var(--text2)">' + ts + '</span> <b style="color:var(--green)">买入</b> ' + (e.code||'') + ' ' + (e.name||'') + ' ' + (e.price||'') + '元 x' + (e.shares||0) + '股 金额' + Math.round(e.cost||0).toLocaleString() + ' 现金' + Math.round(e.cash||0).toLocaleString() + ' <span style="color:var(--text2)">' + (e.strategy||'') + '</span></div>';
        } else if (a === 'sell') {
          return '<div style="padding:2px 0;border-bottom:1px solid rgba(255,255,255,0.05);font-size:11px"><span style="color:var(--text2)">' + ts + '</span> <b style="color:var(--red)">卖出</b> ' + (e.code||'') + ' ' + (e.name||'') + ' ' + (e.price||'') + '元 x' + (e.shares||0) + '股 盈亏' + (e.ret_pct!=null?(e.ret_pct>=0?'+':'')+e.ret_pct+'%':'') + ' ' + Math.round(e.profit||0).toLocaleString() + '元 现金' + Math.round(e.cash||0).toLocaleString() + ' <span style="color:var(--text2)">' + (e.reason||'') + '</span></div>';
        } else if (a === 'snapshot') {
          return '<div style="padding:2px 0;border-bottom:1px solid rgba(255,255,255,0.05);font-size:11px"><span style="color:var(--text2)">' + ts + '</span> <b style="color:var(--accent)">快照</b> 净值' + Math.round(e.equity||0).toLocaleString() + ' 现金' + Math.round(e.cash||0).toLocaleString() + ' 持仓' + (e.positions||0) + '只</div>';
        }
        return '';
      }).join('');
      filterSimLogs();
    } else {
      list.innerHTML = '<div style="color:var(--text2);text-align:center;padding:8px">无日志</div>';
    }
  } catch(e) { list.innerHTML = '<div style="color:var(--red);text-align:center;padding:8px">加载失败</div>'; }
}
function appendSimLog(msg) { addLog('info', msg.msg || JSON.stringify(msg)); }
function filterSimLogs() {
  const q = (document.getElementById('sim-log-search')?.value || '').toLowerCase();
  if (!q) {
    document.querySelectorAll('#sim-log-list div').forEach(d => d.style.display = '');
    return;
  }
  document.querySelectorAll('#sim-log-list div').forEach(d => {
    d.style.display = d.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}
async function loadSimRiskParams() {
  try {
    const resp = await fetch('/api/backtest/simple-config').then(r => r.json());
    const sys = resp.system_config || {};
    const tp = (sys.take_profit_tiers || [{}])[0];
    const tags = [
      { color: '#f85149', label: 'HS', text: '硬止损 ' + ((sys.hard_stop||0)*100).toFixed(0) + '%' },
      { color: '#d29922', label: 'TP1', text: '止盈 +' + ((tp.profit_pct||0)*100).toFixed(0) + '% 卖' + ((tp.sell_ratio||0)*100).toFixed(0) + '%' },
      { color: '#58a6ff', label: 'TR', text: '移动止盈 激活' + ((sys.trail_activate||0)*100).toFixed(0) + '% 回撤' + ((sys.trail_dd||0)*100).toFixed(0) + '%' },
      { color: '#a371f7', label: 'TC', text: '时间退出 ' + (sys.time_exit_days||0) + '天 盈利>' + ((sys.time_exit_profit||0)*100).toFixed(0) + '%' },
      { color: '#8b949e', label: 'TF', text: '强制退出 ' + (sys.time_force_days||0) + '天' },
    ];
    const el = document.getElementById('sim-risk-params');
    if (el) el.innerHTML = tags.map(t =>
      '<span style=\"white-space:nowrap\"><span class=\"reason-tag\" style=\"color:' + t.color + ';font-weight:600\">' + t.label + '</span> ' + t.text + '</span>'
    ).join(' &nbsp;|&nbsp; ');
  } catch(e) {}
}

function switchSimStrategy(val) { /* deprecated - use edit/save */ }

async function loadSimTrades() {
  try {
    const r = await fetch('/api/sim-trader/trades').then(r => r.json());
    const tbody = document.getElementById('sim-trade-tbody');
    if (tbody && r.trades) {
      tbody.innerHTML = r.trades.length > 0
        ? r.trades.sort(function(a, b) { return (b.entry || '').localeCompare(a.entry || ''); }).map(t => {
            const isHolding = t.status === '持仓中';
            const statusColor = isHolding ? 'var(--accent)' : 'var(--text2)';
            const rowClass = isHolding ? ' class="pos-row"' : '';
            const rowData = isHolding ? ' data-code="' + t.code + '" data-entry="' + (t.entry_px || 0) + '" data-shares="' + (t.shares || 0) + '" data-type="trade"' : '';
            const pxClass = isHolding ? ' class="pos-price"' : '';
            const pctClass = isHolding ? ' class="pos-pct"' : '';
            const mvClass = isHolding ? ' class="pos-mv"' : '';
            const shares = t.shares || 0;
            const entryAmt = Math.round(shares * (t.entry_px || 0));
            return '<tr' + rowClass + rowData + '><td>' + t.code + '</td><td>' + (t.name || '') + '</td><td>' + t.entry + ' ' + (t.entry_time||'') + '</td><td>' + (t.exit || (isHolding ? '持仓中' : '--')) + ' ' + (isHolding ? '' : (t.exit_time||'')) + '</td><td>' + t.entry_px + '</td><td' + pxClass + '>' + t.exit_px + '</td><td>' + shares + '</td><td>' + entryAmt.toLocaleString() + '</td><td' + pctClass + ' style="color:' + (t.ret_pct >= 0 ? 'var(--up)' : 'var(--down)') + '">' + (t.ret_pct >= 0 ? '+' : '') + t.ret_pct.toFixed(2) + '%</td><td' + mvClass + '>' + Math.round(t.profit).toLocaleString() + '</td><td>' + t.hold_days + '</td><td>' + (t.entry_reason || '') + '</td><td style="color:' + statusColor + '">' + (t.status || '') + '</td></tr>';
          }).join('')
        : '<tr><td colspan="11" style="text-align:center;color:var(--text2)">暂无记录</td></tr>';
    }
  } catch(e) {}
}

var _simEquityChart = null;
async function renderSimEquityChart() {
  var dom = document.getElementById('sim-equity-chart');
  if (!dom) return;
  try {
    var r = await fetch('/api/sim-trader/equity').then(function(resp) { return resp.json(); });
    if (!r.equity || r.equity.length < 2) { dom.innerHTML = '<div style="color:var(--text2);text-align:center;padding:40px">暂无足够数据绘制曲线</div>'; return; }

    var eqDates = r.equity.map(function(e) { return e.date; });
    var eqValues = r.equity.map(function(e) { return e.equity; });
    var baseVal = eqValues[0];
    // 归一化到 1.0
    var eqNorm = eqValues.map(function(v) { return v / baseVal; });

    if (_simEquityChart) _simEquityChart.dispose();
    _simEquityChart = echarts.init(dom);

    var totalRet = ((eqValues[eqValues.length-1] / baseVal - 1) * 100).toFixed(1);
    var idxColors = ['#ef4444','#f97316','#22c55e'];
    var series = [{
      name: '总资产 (' + (totalRet>=0?'+':'') + totalRet + '%)', type: 'line',
      data: eqNorm, smooth: true,
      lineStyle: { color: '#f59e0b', width: 2.5 }, symbol: 'none',
    }];

    var wantIndices = {'上证指数':1,'创业板指':1,'中证A500':1};
    var idxI = 0;
    if (r.indices) {
      for (var name in r.indices) {
        if (!wantIndices[name]) continue;
        var data = r.indices[name];
        if (!data || data.length === 0) continue;
        var idxMap = {};
        data.forEach(function(d) { idxMap[d.date] = d.close; });
        var aligned = eqDates.map(function(d) { return idxMap[d] || null; });
        var firstIdx = null;
        for (var k = 0; k < aligned.length; k++) {
          if (aligned[k] !== null) { firstIdx = aligned[k]; break; }
        }
        if (firstIdx) {
          var norm = aligned.map(function(v) { return v !== null ? v / firstIdx : null; });
          var idxRet = aligned.filter(function(v){return v!==null});
          var retPct = idxRet.length>0 ? ((idxRet[idxRet.length-1]/firstIdx-1)*100).toFixed(1) : '?';
          series.push({
            name: name + ' (' + (retPct>=0?'+':'') + retPct + '%)', type: 'line',
            data: norm, smooth: true,
            lineStyle: { color: idxColors[idxI % idxColors.length], width: 1, type: 'dashed' },
            symbol: 'none',
          });
        }
        idxI++;
      }
    }

    _simEquityChart.setOption({
      tooltip: { trigger: 'axis', formatter: function(params) {
        var s = params[0].axisValue + '<br/>';
        for (var i=0; i<params.length; i++) {
          s += params[i].marker + ' ' + params[i].seriesName.split(' (')[0] + ': ' + ((params[i].value-1)*100).toFixed(1) + '%<br/>';
        }
        return s;
      }},
      legend: { top: 5, textStyle: { color: '#aaa', fontSize: 10 } },
      grid: { top: 40, right: 20, bottom: 30, left: 55 },
      xAxis: { type: 'category', data: eqDates, axisLabel: { color: '#888', fontSize: 10, rotate: 30 } },
      yAxis: { type: 'value', axisLabel: { color: '#888', formatter: function(v) { return ((v-1)*100).toFixed(0)+'%'; } } },
      series: series,
    });

    window.addEventListener('resize', function() { if (_simEquityChart) _simEquityChart.resize(); });
  } catch(e) {}
}

var _simCalData = {};
var _simCalMonth = new Date().getMonth() + 1;
var _simCalYear = new Date().getFullYear();

async function renderSimCalendar() {
  try {
    var r = await fetch('/api/sim-trader/equity').then(function(resp) { return resp.json(); });
    if (!r.equity || r.equity.length < 2) return;

    // 构建日期→权益映射，并前向填充所有缺失日期
    var eqMap = {};
    r.equity.forEach(function(e) { eqMap[e.date] = e.equity; });
    var dates = Object.keys(eqMap).sort();

    // 前向填充：补全从第一天到最后一天的所有日期
    _simCalData = {};
    var maxAbs = 0;
    var lastEq = eqMap[dates[0]];
    var startD = new Date(dates[0]);
    var endD = new Date(dates[dates.length-1]);
    for (var d = new Date(startD); d <= endD; d.setDate(d.getDate()+1)) {
      var ds = d.toISOString().substring(0,10);
      if (eqMap.hasOwnProperty(ds)) lastEq = eqMap[ds];
      _simCalData[ds] = { equity: lastEq, pnl: 0, isReal: eqMap.hasOwnProperty(ds) };
    }

    // 计算日盈亏
    var sortedDates = Object.keys(_simCalData).sort();
    for (var i = 1; i < sortedDates.length; i++) {
      var prev = _simCalData[sortedDates[i-1]].equity;
      var curr = _simCalData[sortedDates[i]].equity;
      var pnl = curr - prev;
      _simCalData[sortedDates[i]].pnl = Math.round(pnl);
      if (Math.abs(pnl) > maxAbs) maxAbs = Math.abs(pnl);
    }
    _simCalData._maxAbs = maxAbs || 1;

    // 默认显示最后一个月
    _simCalYear = parseInt(dates[dates.length-1].substring(0,4));
    _simCalMonth = parseInt(dates[dates.length-1].substring(5,7));
    drawSimCalendar();
  } catch(e) {}
}

function drawSimCalendar() {
  var dom = document.getElementById('sim-calendar-chart');
  if (!dom) return;
  var titleEl = document.getElementById('sim-cal-title');
  if (titleEl) titleEl.textContent = _simCalYear + '年' + _simCalMonth + '月';

  var firstDay = new Date(_simCalYear, _simCalMonth-1, 1);
  var totalDays = new Date(_simCalYear, _simCalMonth, 0).getDate();

  // 计算当月汇总
  var monthPnl = 0, monthStartEq = null, monthEndEq = null;
  var d2 = new Date(firstDay);
  while (d2.getMonth() === _simCalMonth - 1) {
    var ds2 = _simCalYear + '-' + String(_simCalMonth).padStart(2,'0') + '-' + String(d2.getDate()).padStart(2,'0');
    var inf = _simCalData[ds2];
    if (inf) {
      if (monthStartEq === null) monthStartEq = inf.equity - inf.pnl;
      monthPnl += inf.pnl;
      monthEndEq = inf.equity;
    }
    d2.setDate(d2.getDate() + 1);
  }
  var monthPct = monthStartEq && monthStartEq > 0 ? (monthPnl / monthStartEq * 100) : 0;
  var summaryHtml = '<div style=\"text-align:center; margin-bottom:6px; font-size:13px\">' +
    '月盈亏: <b style=\"color:' + (monthPnl>=0?'#ef4444':'#22c55e') + '\">' + (monthPnl>=0?'+':'') + Math.round(monthPnl).toLocaleString() + ' 元 (' + (monthPct>=0?'+':'') + monthPct.toFixed(1) + '%)</b>' +
    ' | 月初资产: ' + (monthStartEq ? Math.round(monthStartEq).toLocaleString() : '--') + ' 元 → 月末: ' + (monthEndEq ? Math.round(monthEndEq).toLocaleString() : '--') + ' 元' +
    '</div>';

  var weekNames = ['一','二','三','四','五','六','日'];
  var html = '<table style=\"width:100%; border-collapse:collapse; font-size:11px\">';
  html += '<tr style=\"color:#888\">';
  for (var w = 0; w < 7; w++) html += '<th style=\"padding:3px 0; font-weight:normal; text-align:center; ' + (w >= 5 ? 'opacity:0.5' : '') + '\">' + weekNames[w] + '</th>';
  html += '</tr>';

  // 逐日渲染，全部 7 天
  var d = new Date(firstDay);
  // 从当月第一天是周几开始，补齐前面空白
  var startDow = firstDay.getDay(); // 0=Sun
  var startCol = startDow === 0 ? 6 : startDow - 1; // 周一=0, 周日=6

  html += '<tr>';
  for (var c = 0; c < startCol; c++) html += '<td></td>';

  while (d.getMonth() === _simCalMonth - 1) {
    var dow = d.getDay(); // 0=Sun, 6=Sat
    var day = d.getDate();
    var dateStr = _simCalYear + '-' + String(_simCalMonth).padStart(2,'0') + '-' + String(day).padStart(2,'0');
    var info = _simCalData[dateStr];
    var isWeekend = (dow === 0 || dow === 6);
    var bg = isWeekend ? '#111' : '#1a1a2e', color = isWeekend ? '#444' : '#555', pnlText = '', titleText = '';

    if (info) {
      var pnl = info.pnl;
      var equity = info.equity;
      var pct = equity > 0 ? (pnl / (equity - pnl) * 100) : 0;
      if (!isWeekend && pnl !== 0) {
        var ratio = Math.min(1, Math.abs(pnl) / (_simCalData._maxAbs || 1));
        if (pnl > 0) {
          bg = 'rgb(' + Math.round(200+55*ratio) + ',' + Math.round(60-30*ratio) + ',' + Math.round(60-30*ratio) + ')';
          color = '#fff';
        } else {
          bg = 'rgb(' + Math.round(30+20*ratio) + ',' + Math.round(170-50*ratio) + ',' + Math.round(80-30*ratio) + ')';
          color = '#fff';
        }
        pnlText = '<span style=\"font-size:10px\">' + (pnl>=0?'+':'') + (Math.abs(pnl)>=10000?(pnl/10000).toFixed(1)+'万':pnl) + '<br><span style=\"font-size:9px;opacity:0.8\">' + (pct>=0?'+':'') + pct.toFixed(1) + '%</span></span>';
      }
      titleText = dateStr + (isWeekend ? ' [周末]' : '') + ' | 资产: ' + Math.round(equity).toLocaleString() + ' 元';
      if (!isWeekend) titleText += ' | 盈亏: ' + (pnl>=0?'+':'') + pnl.toLocaleString() + ' 元 (' + (pct>=0?'+':'') + pct.toFixed(1) + '%)';
    }
    html += '<td title=\"' + titleText + '\" style=\"padding:4px 2px; text-align:center; background:' + bg + '; color:' + color + '; border-radius:3px; cursor:default; line-height:1.3;' + (isWeekend ? ' opacity:0.5' : '') + '\"><div style=\"font-weight:bold\">' + day + '</div>' + pnlText + '</td>';

    if (dow === 0) { html += '</tr><tr>'; } // 周日换行
    d.setDate(d.getDate() + 1);
  }
  // 补齐最后一行
  var lastDow = new Date(_simCalYear, _simCalMonth-1, totalDays).getDay();
  for (var c = lastDow === 0 ? 6 : lastDow - 1; c < 6; c++) html += '<td></td>';
  html += '</tr>';
  html += '</table>';
  dom.innerHTML = summaryHtml + html;
}

function simCalPrevMonth() {
  _simCalMonth--;
  if (_simCalMonth < 1) { _simCalMonth = 12; _simCalYear--; }
  drawSimCalendar();
}
function simCalNextMonth() {
  _simCalMonth++;
  if (_simCalMonth > 12) { _simCalMonth = 1; _simCalYear++; }
  drawSimCalendar();
}

var _simTreeChart = null;
async function renderSimStockAnalysis() {
  var dom = document.getElementById('sim-stock-treemap');
  if (!dom) return;
  try {
    var r = await fetch('/api/sim-trader/trades?limit=600').then(function(resp) { return resp.json(); });
    if (!r.trades || r.trades.length === 0) return;

    var closed = r.trades.filter(function(t) { return t.status === '已平仓'; });
    if (closed.length === 0) return;

    var stockMap = {};
    closed.forEach(function(t) {
      var code = t.code;
      if (!stockMap[code]) stockMap[code] = { name: t.name||'', profit: 0, trades: 0, retSum: 0 };
      stockMap[code].profit += t.profit || 0;
      stockMap[code].trades += 1;
      stockMap[code].retSum += t.ret_pct || 0;
    });

    var stocks = [];
    for (var code in stockMap) {
      var s = stockMap[code];
      stocks.push({ code: code, name: s.name, profit: s.profit, trades: s.trades, avgRet: (s.retSum/s.trades).toFixed(1) });
    }
    stocks.sort(function(a,b) { return b.profit - a.profit; });

    var top10 = stocks.slice(0, 10);
    var bot10 = stocks.slice(-10).reverse();
    var items = top10.concat(bot10);
    // 盈亏金额为负的用绿色
    var treeData = items.map(function(s) {
      return {
        name: s.code + ' ' + s.name + '\n' + (s.profit>=0?'+':'') + Math.round(s.profit).toLocaleString() + ' (' + (s.avgRet>=0?'+':'') + s.avgRet + '%)',
        value: Math.abs(s.profit),
        itemStyle: { color: s.profit >= 0 ? '#ef4444' : '#22c55e' }
      };
    });

    if (_simTreeChart) _simTreeChart.dispose();
    _simTreeChart = echarts.init(dom);

    _simTreeChart.setOption({
      tooltip: { formatter: function(p) { return p.name.replace(/\n/g, '<br/>'); } },
      series: [{
        type: 'treemap', data: treeData, roam: false,
        label: { show: true, formatter: function(p) { return p.name; }, fontSize: 10, color: '#fff' },
        levels: [{ itemStyle: { gapWidth: 2 } }]
      }]
    });

    window.addEventListener('resize', function() { if (_simTreeChart) _simTreeChart.resize(); });
  } catch(e) {}
}

async function loadSimStrategy() {
  try {
    const r = await fetch('/api/sim-trader/config').then(r => r.json());
    if (r.status === 'ok') {
      document.getElementById('sim-strategy-display').textContent = r.current_strategy || '--';
      const sel = document.getElementById('sim-strategy-select');
      if (sel && r.strategies) {
        const pyOpts = r.strategies.map(s => '<option value=\"' + s.name + '\" data-type=\"python\">' + s.name + '</option>').join('');
        sel.innerHTML = pyOpts + '<optgroup label=\"TDX 策略\"><option value=\"QUANTQQ\" data-type=\"tdx\">QUANTQQ</option></optgroup>';
      }
    }
  } catch(e) {}
}

function editSimStrategy() {
  const sel = document.getElementById('sim-strategy-select');
  sel.value = document.getElementById('sim-strategy-display').textContent;
  document.getElementById('sim-strategy-display').style.display = 'none';
  document.getElementById('sim-strategy-edit').style.display = 'block';
  document.getElementById('btn-sim-save-strategy').style.display = 'inline';
}

async function saveSimStrategy() {
  const sel = document.getElementById('sim-strategy-select');
  const name = sel.value;
  const opt = sel.selectedOptions[0];
  const type = opt ? opt.dataset.type : 'python';
  try {
    const r = await fetch('/api/settings/sim-switches', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({strategy_name: name, strategy_type: type}) }).then(r => r.json());
    document.getElementById('sim-strategy-display').textContent = name;
    document.getElementById('sim-strategy-display').style.display = 'inline';
    document.getElementById('sim-strategy-edit').style.display = 'none';
    document.getElementById('btn-sim-save-strategy').style.display = 'none';
    const msg = document.getElementById('sim-strategy-msg');
    if (msg) { msg.textContent = r.status === 'ok' ? '已保存' : '失败'; msg.style.color = r.status === 'ok' ? 'var(--green)' : 'var(--red)'; }
  } catch(e) {
    document.getElementById('sim-strategy-msg').textContent = '网络错误';
  }
}
async function loadSimMonitor() {
  try {
    const r = await fetch('/api/sim-trader/status').then(r => r.json());
    if (r.status === 'ok') {
      document.getElementById('sim-mon-val').textContent = r.monitor_enabled ? '开启' : '关闭';
      document.getElementById('sim-mon-mode-val').textContent = r.monitor_mode === 'intraday' ? '盘中执行' : '仅告警';
    }
  } catch(e) {}
}

function editSimMonitor() {
  document.getElementById('sim-monitor-display').style.display = 'none';
  document.getElementById('sim-monitor-edit').style.display = 'flex';
  document.getElementById('btn-sim-save-monitor').style.display = 'inline';
  document.getElementById('sim-edit-mon').value = document.getElementById('sim-mon-val').textContent === '开启' ? 'true' : 'false';
  document.getElementById('sim-edit-mon-mode').value = document.getElementById('sim-mon-mode-val').textContent === '盘中执行' ? 'intraday' : 'close';
}

async function saveSimMonitor() {
  const body = {
    monitor_enabled: document.getElementById('sim-edit-mon').value === 'true',
    monitor_mode: document.getElementById('sim-edit-mon-mode').value,
  };
  try {
    const r = await fetch('/api/settings/sim-switches', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
    const msg = document.getElementById('sim-monitor-msg');
    if (msg) { msg.textContent = r.status === 'ok' ? '已保存' : '失败'; msg.style.color = r.status === 'ok' ? 'var(--green)' : 'var(--red)'; }
    document.getElementById('sim-mon-val').textContent = body.monitor_enabled ? '开启' : '关闭';
    document.getElementById('sim-mon-mode-val').textContent = body.monitor_mode === 'intraday' ? '盘中执行' : '仅告警';
    document.getElementById('sim-monitor-display').style.display = 'block';
    document.getElementById('sim-monitor-edit').style.display = 'none';
    document.getElementById('btn-sim-save-monitor').style.display = 'none';
  } catch(e) {
    const msg = document.getElementById('sim-monitor-msg');
    if (msg) { msg.textContent = '网络错误'; msg.style.color = 'var(--red)'; }
  }
}

async function loadSimSwitches() {
  try {
    const r = await fetch('/api/sim-trader/status').then(r => r.json());
    if (r.status === 'ok') {
      document.getElementById('sim-sell-val').textContent = r.auto_sell ? '开' : '关';
      document.getElementById('sim-scan-val').textContent = r.auto_scan ? '开' : '关';
      document.getElementById('sim-buy-val').textContent = r.auto_buy ? '开' : '关';
    }
  } catch(e) {}
}

function editSimSwitches() {
  const disp = document.getElementById('sim-switches-display');
  const edit = document.getElementById('sim-switches-edit');
  const saveBtn = document.getElementById('btn-sim-save-switches');
  if (disp) disp.style.display = 'none';
  if (edit) edit.style.display = 'flex';
  if (saveBtn) saveBtn.style.display = 'inline';
  // pre-fill from current display values
  document.getElementById('sim-edit-sell').value = document.getElementById('sim-sell-val').textContent === '开' ? 'true' : 'false';
  document.getElementById('sim-edit-scan').value = document.getElementById('sim-scan-val').textContent === '开' ? 'true' : 'false';
  document.getElementById('sim-edit-buy').value = document.getElementById('sim-buy-val').textContent === '开' ? 'true' : 'false';
}

async function saveSimSwitches() {
  const body = {
    auto_sell: document.getElementById('sim-edit-sell').value === 'true',
    auto_scan: document.getElementById('sim-edit-scan').value === 'true',
    auto_buy: document.getElementById('sim-edit-buy').value === 'true',
  };
  try {
    const r = await fetch('/api/settings/sim-switches', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
    const msg = document.getElementById('sim-switches-msg');
    if (msg) { msg.textContent = r.status === 'ok' ? '已保存' : '失败'; msg.style.color = r.status === 'ok' ? 'var(--green)' : 'var(--red)'; }
    // update display
    document.getElementById('sim-sell-val').textContent = body.auto_sell ? '开' : '关';
    document.getElementById('sim-scan-val').textContent = body.auto_scan ? '开' : '关';
    document.getElementById('sim-buy-val').textContent = body.auto_buy ? '开' : '关';
    // hide edit
    document.getElementById('sim-switches-display').style.display = 'flex';
    document.getElementById('sim-switches-edit').style.display = 'none';
    document.getElementById('btn-sim-save-switches').style.display = 'none';
  } catch(e) {
    const msg = document.getElementById('sim-switches-msg');
    if (msg) { msg.textContent = '网络错误'; msg.style.color = 'var(--red)'; }
  }
}

async function executeSimTrader() {
  const btn = document.getElementById('btn-sim-execute');
  const resultDiv = document.getElementById('sim-execute-result');
  if (btn) btn.disabled = true;
  if (resultDiv) { resultDiv.style.display = 'block'; resultDiv.innerHTML = '<span style=\"color:var(--text2)\">⏳ 正在执行...</span>'; }
  try {
    const r = await fetch('/api/sim-trader/execute', { method: 'POST' }).then(r => r.json());
    if (resultDiv) {
      const ok = r.status === 'ok';
      resultDiv.style.background = ok ? 'rgba(63,185,80,0.1)' : 'rgba(248,81,73,0.1)';
      resultDiv.innerHTML = '<span style=\"color:' + (ok ? 'var(--green)' : 'var(--red)') + '\">' + (r.summary || r.message || JSON.stringify(r)) + '</span>';
    }
    loadSimTraderStatus();
  } catch(e) {
    if (resultDiv) { resultDiv.style.background = 'rgba(248,81,73,0.1)'; resultDiv.innerHTML = '<span style=\"color:var(--red)\">网络错误: ' + e.message + '</span>'; }
  }
  if (btn) btn.disabled = false;
}

async function resetSimTrader() {
  if (!confirm('确认重置模拟盘？所有持仓和交易记录将被清空。')) return;
  try {
    await fetch('/api/sim-trader/reset', { method: 'POST' });
    loadSimTraderStatus();
    addLog('ok', '模拟盘已重置');
  } catch(e) { addLog('error', '重置失败: ' + e.message); }
}

function renderRiskTiers(tiers) {
  const container = document.getElementById('set-tp-tiers-container');
  if (!container) return;
  const colors = ['#d29922','#3fb950','#58a6ff','#a371f7','#f59e0b'];
  container.innerHTML = tiers.map((t, i) => {
    const c = colors[i % colors.length];
    return '<div class=\"bt-cfg-grid\" style=\"margin-bottom:4px\">' +
      '<div class=\"bt-cfg-item\"><label><span class=\"reason-tag\" style=\"color:'+c+'\">TP'+(i+1)+'</span> 止盈%</label><input type=\"number\" class=\"risk-tier-pct\" step=\"0.5\" value=\"'+(t.profit_pct*100).toFixed(1)+'\"></div>' +
      '<div class=\"bt-cfg-item\"><label><span class=\"reason-tag\" style=\"color:'+c+'\">TP'+(i+1)+'</span> 卖出%</label><input type=\"number\" class=\"risk-tier-ratio\" step=\"5\" value=\"'+(t.sell_ratio*100).toFixed(0)+'\"></div>' +
      '</div>';
  }).join('');
}

function addRiskTier() {
  const container = document.getElementById('set-tp-tiers-container');
  if (!container) return;
  const idx = container.querySelectorAll('.bt-cfg-grid').length;
  const colors = ['#d29922','#3fb950','#58a6ff','#a371f7','#f59e0b'];
  const c = colors[idx % colors.length];
  const div = document.createElement('div');
  div.className = 'bt-cfg-grid'; div.style.marginBottom = '4px';
  div.innerHTML =
    '<div class=\"bt-cfg-item\"><label><span class=\"reason-tag\" style=\"color:'+c+'\">TP'+(idx+1)+'</span> 止盈%</label><input type=\"number\" class=\"risk-tier-pct\" step=\"0.5\" value=\"'+(3+idx*3)+'.0\"></div>' +
    '<div class=\"bt-cfg-item\"><label><span class=\"reason-tag\" style=\"color:'+c+'\">TP'+(idx+1)+'</span> 卖出%</label><input type=\"number\" class=\"risk-tier-ratio\" step=\"5\" value=\"'+(Math.min(15+idx*10,50))+'\"</div>';
  container.appendChild(div);
}

function delRiskTier() {
  const container = document.getElementById('set-tp-tiers-container');
  if (!container) return;
  const rows = container.querySelectorAll('.bt-cfg-grid');
  if (rows.length <= 1) return;
  rows[rows.length - 1].remove();
}

async function saveRiskSettings() {
  const getv = (id, fn) => { const el = document.getElementById(id); if (!el || el.value === '') return null; return fn ? fn(el.value) : el.value; };
  const tiers = [];
  document.querySelectorAll('#set-tp-tiers-container .bt-cfg-grid').forEach(row => {
    const pct = parseFloat(row.querySelector('.risk-tier-pct')?.value);
    const ratio = parseFloat(row.querySelector('.risk-tier-ratio')?.value);
    if (!isNaN(pct) && !isNaN(ratio) && pct > 0 && ratio > 0) {
      tiers.push({ profit_pct: pct / 100, sell_ratio: ratio / 100 });
    }
  });
  const body = {
    hard_stop: getv('set-stop', v => -Math.abs(parseFloat(v)) / 100),
    take_profit_tiers: tiers.length > 0 ? tiers : [{ profit_pct: 0.03, sell_ratio: 0.30 }],
    trail_activate: getv('set-trail-act', v => parseFloat(v) / 100),
    trail_dd: getv('set-trail-dd', v => parseFloat(v) / 100),
    time_exit_days: getv('set-days', v => parseInt(v)),
    time_exit_profit: getv('set-days-min-pnl', v => parseFloat(v) / 100),
    time_force_days: getv('set-force-days', v => parseInt(v)),
    loss_streak_halve: getv('set-streak-halve', v => parseInt(v)),
    loss_streak_pause: getv('set-streak-pause', v => parseInt(v)),
    pause_days: getv('set-pause-days', v => parseInt(v)),
    same_stock_cooldown: getv('set-cooldown', v => parseInt(v)),
    first_day_exit_min_profit: (function() { var ck = document.getElementById('set-fd-enable'); if (ck && !ck.checked) return 0; var el = document.getElementById('set-fd-profit'); if (!el || el.value === '') return 0.03; return parseFloat(el.value) / 100; })(),
    first_day_exit_days: getv('set-fd-days', v => parseInt(v)) || 1,
  };
  try {
    const r = await fetch('/api/settings/risk-params', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
    const msg = document.getElementById('save-risk-msg');
    if (msg) { msg.textContent = r.status === 'ok' ? '✓ 已保存' : '✗ 失败'; msg.style.color = r.status === 'ok' ? 'var(--green)' : 'var(--red)'; }
  } catch(e) {
    const msg = document.getElementById('save-risk-msg');
    if (msg) { msg.textContent = '✗ 网络错误'; msg.style.color = 'var(--red)'; }
  }
}
function renderSearchSpace(data) { /* stub - AI optimizer card */ }
function saveDataSettings() {
  var times = [];
  document.querySelectorAll('.cron-check:checked').forEach(function(cb) { times.push(cb.value); });
  var data = {
    cron: {
      enabled: document.getElementById('set-cron-enable').value === 'true',
      sync_times: times
    },
    data: {
      auto_sync: document.getElementById('set-auto-sync').value
    }
  };
  var msg = document.getElementById('save-data-msg');
  fetch('/api/settings', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({data: data})
  }).then(function(r) { return r.json(); }).then(function(res) {
    if (msg) { msg.textContent = res.message || '已保存'; msg.style.color = 'var(--green)'; }
  }).catch(function() {
    if (msg) { msg.textContent = '保存失败'; msg.style.color = 'var(--red)'; }
  });
}

async function loadDataSettings() {
  try {
    var r = await fetch('/api/settings').then(function(resp) { return resp.json(); });
    var cron = r.cron || {};
    var data = r.data || {};
    document.getElementById('set-cron-enable').value = cron.enabled ? 'true' : 'false';
    document.getElementById('set-auto-sync').value = data.auto_sync || 'off';
    var times = cron.sync_times || [];
    document.querySelectorAll('.cron-check').forEach(function(cb) {
      cb.checked = times.indexOf(cb.value) >= 0;
    });
  } catch(e) {}
}
function saveSettings() { /* stub */ }
function saveSearchSpace() { /* stub */ }
function saveGatewaySettings() { /* stub */ }
function loadReportsPage() { /* stub */ }
function loadReportsList() { /* stub */ }
function downloadModalMD() { /* stub */ }
function downloadViewerMD() { /* stub */ }
async function searchStockForReport() {
  var q = document.getElementById('new-report-search')?.value?.trim();
  if (!q) return;
  var resultsDiv = document.getElementById('new-report-results');
  if (!resultsDiv) return;
  resultsDiv.innerHTML = '<p style="color:var(--text2)">⏳ 检索中...</p>';
  try {
    var r = await fetch('/api/meta/stocks/search?query=' + encodeURIComponent(q)).then(function(resp) { return resp.json(); });
    if (r.status === 'ok' && r.data && r.data.length > 0) {
      resultsDiv.innerHTML = r.data.slice(0, 20).map(function(s) {
        return '<div style="display:flex; justify-content:space-between; align-items:center; padding:8px 12px; border-bottom:1px solid var(--border); cursor:pointer" onclick="generateAIReport(\'' + s.code + '\',\'' + (s.name||'') + '\', this)">' +
          '<span><b>' + s.code + '</b> ' + (s.name||'') + '</span>' +
          '<span style="font-size:11px; color:var(--text2)">' + (s.sector||'') + '</span>' +
          '<span class="tag tag-buy" style="cursor:pointer">生成报告</span>' +
        '</div>';
      }).join('');
    } else {
      resultsDiv.innerHTML = '<p style="color:var(--text2)">未找到匹配标的</p>';
    }
  } catch(e) { resultsDiv.innerHTML = '<p style="color:var(--red)">检索失败</p>'; }
}

async function generateAIReport(code, name, el) {
  if (el) { el.innerHTML = '<span style="color:var(--yellow)">⏳ 生成中...</span>'; el.onclick = null; }
  try {
    var r = await fetch('/api/agents/analyze/' + encodeURIComponent(code) + '?name=' + encodeURIComponent(name)).then(function(resp) { return resp.json(); });
    if (r.status === 'ok') {
      // Show report in viewer
      document.getElementById('report-empty-state').style.display = 'none';
      var container = document.getElementById('report-viewer-container');
      container.style.display = 'flex';
      document.getElementById('viewer-report-title').textContent = code + ' ' + name + ' AI 深度报告';
      document.getElementById('viewer-report-content').innerHTML = '<pre style="white-space:pre-wrap;font-size:13px;line-height:1.8">' + (r.report||'') + '</pre>';
    } else {
      if (el) { el.innerHTML = '<span style="color:var(--red)">失败</span>'; el.onclick = function() { generateAIReport(code, name, el); }; }
    }
  } catch(e) {
    if (el) { el.innerHTML = '<span style="color:var(--red)">网络错误</span>'; el.onclick = function() { generateAIReport(code, name, el); }; }
  }
}

function openNewReportSearch() {
  document.getElementById('report-viewer-container').style.display = 'none';
  document.getElementById('report-empty-state').style.display = '';
}
function closeAiReport() {
  document.getElementById('report-viewer-container').style.display = 'none';
  document.getElementById('report-empty-state').style.display = '';
}
async function addWatchlist() {
  let code = document.getElementById('add-wl-code')?.value?.trim();
  if (!code) { addLog('warn', '请输入股票代码'); return; }
  // 去掉 .SH/.SZ 后缀
  code = code.split('.')[0];
  // 拼音或简称：通过搜索API解析
  if (!/^\d{6}$/.test(code)) {
    try {
      const sr = await fetch('/api/stock/search?q=' + code).then(r => r.json());
      const items = Array.isArray(sr) ? sr : (sr.items || []);
      if (items.length > 0) code = items[0].code;
      else { addLog('warn', '未找到匹配股票: ' + code); return; }
    } catch(e) { addLog('error', '搜索失败'); return; }
  }
  try {
    const r = await fetch('/api/watchlist', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({code: code}),
    }).then(r => r.json());
    if (r.status === 'ok') addLog('ok', r.message);
    else addLog('error', r.message || '添加失败');
    document.getElementById('add-wl-code').value = '';
    loadWatchlist();
  } catch(e) { addLog('error', '网络错误: ' + e.message); }
}
async function loadWatchlist() {
  try {
    const r = await fetch('/api/watchlist?limit=50').then(r => r.json());
    const tbody = document.getElementById('watchlist-tbody');
    const items = Array.isArray(r) ? r : (r.items || []);
    if (tbody) {
      tbody.innerHTML = items.map(w => '<tr class=\"wl-row\" data-code=\"' + w.code + '\"><td>' + w.code + '</td><td>' + (w.name || '') + '</td><td class=\"live-price\">--</td><td class=\"live-pct\">--</td><td>' + (w.sector || '') + '</td><td>' + (w.added_at || '') + '</td><td><button class=\"btn btn-ghost btn-sm\" onclick=\"removeWatchlist(\'' + w.code + '\')\" style=\"color:var(--red)\">删除</button></td></tr>').join('') || '<tr><td colspan=\"7\" style=\"text-align:center;color:var(--text2)\">暂无自选</td></tr>';
    }
    // 重新订阅行情，确保新增自选股被纳入
    if (window.marketUpdater) window.marketUpdater.resubscribe();
  } catch(e) {}
}
async function removeWatchlist(code) {
  try {
    await fetch('/api/watchlist/' + encodeURIComponent(code), { method: 'DELETE' });
    loadWatchlist();
  } catch(e) {}
}
// ─── 热点板块个股搜索 ──────────────────────────────
var _hotSearchIdx = -1;
async function onHotStockSearch(val) {
  if (!val || val.length < 1) { closeHotStockDropdown(); return; }
  try {
    var r = await fetch('/api/stock/search?q=' + encodeURIComponent(val)).then(function(rr){return rr.json()});
    var dd = document.getElementById('hot-stock-dropdown');
    if (!dd) return;
    var items = Array.isArray(r) ? r : (r.items || []);
    if (items.length === 0) { dd.style.display = 'none'; return; }
    _hotSearchIdx = -1;
    dd.innerHTML = '';
    items.forEach(function(s) {
      var div = document.createElement('div');
      div.style.cssText = 'padding:6px 10px;cursor:pointer;border-bottom:1px solid var(--border);font-size:12px';
      div.textContent = s.code + ' ' + (s.name||'');
      div.addEventListener('mousedown', function() {
        document.getElementById('hot-stock-code').value = s.code.split('.')[0];
        closeHotStockDropdown();
      });
      dd.appendChild(div);
    });
    dd.style.display = 'block';
  } catch(e) {}
}
function onHotStockKeydown(e) {
  var dd = document.getElementById('hot-stock-dropdown');
  var items = dd ? dd.querySelectorAll('div') : [];
  if (!dd || dd.style.display === 'none' || items.length === 0) return;
  if (e.key === 'ArrowDown') { e.preventDefault(); _hotSearchIdx = Math.min(_hotSearchIdx+1, items.length-1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); _hotSearchIdx = Math.max(_hotSearchIdx-1, 0); }
  else if (e.key === 'Enter') { e.preventDefault(); if (_hotSearchIdx>=0) items[_hotSearchIdx].click(); else if (items.length>0) items[0].click(); }
  else if (e.key === 'Escape') { closeHotStockDropdown(); }
}
function closeHotStockDropdown() { var dd = document.getElementById('hot-stock-dropdown'); if (dd) dd.style.display = 'none'; }
async function refreshHotSector() {
  try {
    document.getElementById('btn-force-recalc-hot').disabled = true;
    await fetch('/api/hot/refresh', { method: 'POST' });
    addLog('ok', '热度重算已触发，请稍后刷新');
    setTimeout(function() { loadHotSectorData(); }, 3000);
  } catch(e) { addLog('error', '重算失败: ' + e.message); }
  setTimeout(function() { document.getElementById('btn-force-recalc-hot').disabled = false; }, 5000);
}
async function loadHotSectorData() {
  try {
    var sRes = await fetch('/api/hot/sectors?limit=20').then(function(r){return r.json()});
    var cRes = await fetch('/api/hot/concepts?limit=20&min_stocks=3').then(function(r){return r.json()});
    var status = await fetch('/api/hot/last-updated').then(function(r){return r.json()});

    // 更新日期
    var dateEl = document.getElementById('hot-sector-trade-date');
    if (dateEl && status.last_updated) dateEl.textContent = status.last_updated;

    // 渲染板块表
    var sTbody = document.getElementById('hot-sector-tbody');
    var sectors = Array.isArray(sRes) ? sRes : (sRes.data || []);
    if (sTbody && sectors.length > 0) {
      sTbody.innerHTML = sectors.map(function(s, i) {
        return '<tr><td>' + (i+1) + '</td><td>' + (s.name||'') + '</td><td style="color:var(--accent)">' + (s.hotness||0).toFixed(1) + '%</td><td style="color:var(--red)">' + (s.advance_count||0) + '</td><td style="color:var(--green)">' + (s.decline_count||0) + '</td><td>' + (s.count||0) + '</td></tr>';
      }).join('');
    }

    // 渲染概念表
    var cTbody = document.getElementById('hot-concept-tbody');
    var concepts = Array.isArray(cRes) ? cRes : (cRes.data || []);
    if (cTbody && concepts.length > 0) {
      cTbody.innerHTML = concepts.map(function(c, i) {
        return '<tr><td>' + (i+1) + '</td><td>' + (c.name||'') + '</td><td style="color:var(--accent)">' + (c.hotness||0).toFixed(1) + '%</td><td style="color:var(--red)">' + (c.advance_count||0) + '</td><td style="color:var(--green)">' + (c.decline_count||0) + '</td><td>' + (c.count||0) + '</td></tr>';
      }).join('');
    }

    // 更新计数
    var scEl = document.getElementById('sector-count');
    if (scEl) scEl.textContent = sectors.length + '个';
    var ccEl = document.getElementById('concept-count');
    if (ccEl) ccEl.textContent = concepts.length + '个';
  } catch(e) { console.error('loadHotSectorData:', e); }
}
async function queryHotStockScore() {
  var code = document.getElementById('hot-stock-code')?.value?.trim();
  if (!code) { addLog('warn', '请输入股票代码'); return; }
  code = code.split('.')[0];
  try {
    var r = await fetch('/api/hot/stock/' + code).then(function(r){return r.json()});
    var el = document.getElementById('hot-stock-status');
    var d = (r && r.data) ? r.data : r;
    if (el && d.composite_score !== undefined) {
      el.innerHTML = '<b>' + code + '</b> 综合评分: <b style="color:' + (d.composite_score>=50?'var(--red)':'var(--green)') + '">' + (d.composite_score||0).toFixed(1) + '</b> 板块: ' + (d.sector||d.sector_name||'-') + ' 热度' + (d.sector_hotness||0).toFixed(1) + '%';
    }
  } catch(e) { addLog('error', '查询失败'); }
}
function doTdxTranslate() { /* stub */ }
function closeTdxModal() { /* stub */ }
function closeConstituentModal() { /* stub */ }
// ─── Reports 页面搜索函数 ──────────────────────────────────────
let _reportSearchIdx = -1;
async function onReportSearchInput(val) {
  if (!val || val.length < 1) { closeReportDropdown(); return; }
  try {
    const r = await fetch('/api/stock/search?q=' + encodeURIComponent(val)).then(r => r.json());
    const dd = document.getElementById('report-search-dropdown');
    if (!dd) return;
    const items = Array.isArray(r) ? r : (r.items || []);
    if (items.length === 0) { dd.style.display = 'none'; return; }
    _reportSearchIdx = -1;
    dd.innerHTML = items.map(s => '<div style=\"padding:6px 10px;cursor:pointer;border-bottom:1px solid var(--border);font-size:12px\" onmousedown=\"selectReportCode(\'' + s.code + '\',\'' + (s.name || '') + '\')\">' + s.code + ' <span style=\"color:var(--text2)\">' + (s.name || '') + '</span></div>').join('');
    dd.style.display = 'block';
  } catch(e) {}
}
function onReportSearchKeydown(e) {
  const dd = document.getElementById('report-search-dropdown');
  const items = dd ? dd.querySelectorAll('div') : [];
  if (!dd || dd.style.display === 'none' || items.length === 0) return;
  if (e.key === 'ArrowDown') { e.preventDefault(); _reportSearchIdx = Math.min(_reportSearchIdx + 1, items.length - 1); updateReportHighlight(items); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); _reportSearchIdx = Math.max(_reportSearchIdx - 1, 0); updateReportHighlight(items); }
  else if (e.key === 'Enter') { e.preventDefault(); if (_reportSearchIdx >= 0) items[_reportSearchIdx].click(); else if (items.length > 0) items[0].click(); }
  else if (e.key === 'Escape') { closeReportDropdown(); }
}
function updateReportHighlight(items) {
  items.forEach((it, i) => it.style.background = i === _reportSearchIdx ? 'var(--accent)' : '');
}
function closeReportDropdown() {
  const dd = document.getElementById('report-search-dropdown');
  if (dd) dd.style.display = 'none';
}
function selectReportCode(code, name) {
  document.getElementById('new-report-search').value = code.split('.')[0];
  closeReportDropdown();
}
async function onWlSearchInput(val) {
  if (!val || val.length < 1) { closeWlDropdown(); return; }
  try {
    const r = await fetch('/api/stock/search?q=' + encodeURIComponent(val)).then(r => r.json());
    const dd = document.getElementById('wl-dropdown');
    if (!dd) return;
    const items = Array.isArray(r) ? r : (r.items || []);
    if (items.length === 0) { dd.style.display = 'none'; return; }
    dd.innerHTML = items.map(s => '<div style=\"padding:6px 10px;cursor:pointer;border-bottom:1px solid var(--border);font-size:12px\" onmousedown=\"selectWlCode(\'' + s.code + '\',\'' + (s.name || '') + '\')\">' + s.code + ' <span style=\"color:var(--text2)\">' + (s.name || '') + '</span></div>').join('');
    dd.style.display = 'block';
  } catch(e) {}
}
function onWlSearchKeydown(e) {
  if (e.key === 'Enter') { e.preventDefault(); addWatchlist(); }
  if (e.key === 'Escape') closeWlDropdown();
}
function closeWlDropdown() {
  const dd = document.getElementById('wl-dropdown');
  if (dd) dd.style.display = 'none';
}
function selectWlCode(code, name) {
  document.getElementById('add-wl-code').value = code.split('.')[0];
  closeWlDropdown();
}
function loadSectorHierarchy() { /* stub */ }

function switchTab(name) {
  console.log('switchTab called with name:', name);
  // 更可靠的按钮激活判断方法
  document.querySelectorAll('nav button').forEach(btn => {
    const buttonName = btn.getAttribute('onclick')?.match(/switchTab\('([^']+)'\)/)?.[1];
    btn.classList.toggle('active', buttonName === name);
  });
  document.querySelectorAll('.tab-panels > div').forEach(panel => {
    panel.classList.toggle('active', panel.id === `tab-${name}`);
  });
  if (name === 'data') loadFundamentalsPreview();
  if (name === 'factory') initMonaco();
  if (name === 'reports') loadReportsList();
  if (name === 'settings') loadSettings();
  if (name === 'watchlist') { try { if (typeof loadWatchlist === 'function') loadWatchlist(); } catch(e) {} }
  if (name === 'trades') loadTrades();
  if (name === 'positions') loadPositions();
  if (name === 'backtest' || name === 'scan') loadSectorHierarchy();
  if (name === 'backtest') { loadBacktestCapitalDefaults(); loadBtSimpleConfig(); loadSimpleBtHistory(); }
  if (name === 'ai-backtest') { loadBacktestCapitalDefaults(); initAIBacktest(); }
  if (name === 'radar') loadHotSectorData();
  if (name === 'sim-trader') { loadSimTraderStatus(); initLogDates(); loadSimLogs(); loadSimRiskParams(); loadSimSwitches(); loadSimMonitor(); loadSimStrategy(); loadSimTrades(); renderSimEquityChart(); renderSimCalendar(); renderSimStockAnalysis(); }
  if (name === 'tqsdk') { initTqsdkTab(); }
}

// ─── AI 回测 JS ────────────────────────────────────────────

let _aiExchanges = new Set(['SH', 'SZ']);
let _aiPollTimer = null;
let _aiPendingParams = null;

function initAIBacktest() {
  // ── 默认日期：用本地时间拼接，避免 toISOString() UTC 偏移 ──
  function localDateStr(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }
  const today = new Date();
  const oneMonthAgo = new Date(today.getFullYear(), today.getMonth() - 1, today.getDate());
  document.getElementById('ai-start-date').value = localDateStr(oneMonthAgo);
  document.getElementById('ai-end-date').value   = localDateStr(today);
}

function aiToggleExchange(ex) {
  if (_aiExchanges.has(ex)) { _aiExchanges.delete(ex); }
  else { _aiExchanges.add(ex); }
  document.getElementById(`ai-btn-${ex.toLowerCase()}`).classList.toggle('active', _aiExchanges.has(ex));
}

async function startAIBacktest() {
  const strategy = document.getElementById('ai-bt-strategy').value;
  const start    = document.getElementById('ai-start-date').value;
  const end      = document.getElementById('ai-end-date').value;
  const useLLM   = document.getElementById('ai-use-llm').checked;
  const nExp     = parseInt(document.getElementById('ai-n-exploration').value) || 12;
  const nBay     = parseInt(document.getElementById('ai-n-bayesian').value)    || 50;

  if (!strategy) { alert('请选择策略'); return; }
  if (!start || !end) { alert('请填写起止日期'); return; }
  if (_aiExchanges.size === 0) { alert('请至少选择一个市场'); return; }

  // 重置 UI
  document.getElementById('ai-bt-start-btn').style.display = 'none';
  document.getElementById('ai-bt-stop-btn').style.display  = '';
  document.getElementById('ai-progress-card').style.display = '';
  document.getElementById('ai-results-card').style.display  = 'none';
  document.getElementById('ai-report-card').style.display   = 'none';
  document.getElementById('ai-log-box').innerHTML = '';
  document.getElementById('ai-progress-bar').style.width = '0%';
  document.getElementById('ai-phase-label').textContent = '🔄 启动中...';
  document.getElementById('ai-trial-label').textContent = '-- / --';
  // 立即更新 badge 让用户感知任务已启动
  const badge = document.getElementById('ai-status-badge');
  if (badge) {
    badge.textContent = '🔄 运行中';
    badge.style.color = 'var(--blue)';
  }

  const resp = await fetch('/api/backtest/ai/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      strategy_name: strategy,
      strategy_params: collectStrategyParams('ai'),
      risk_params: collectRiskParams('ai'),
      start, end,
      exchanges: Array.from(_aiExchanges),
      use_llm: useLLM,
      n_exploration: nExp,
      n_bayesian: nBay,
      index_filter: getIndexFilter('ai'),
      min_mv: document.getElementById('ai-min-mv').value ? parseFloat(document.getElementById('ai-min-mv').value) : null,
      max_mv: document.getElementById('ai-max-mv').value ? parseFloat(document.getElementById('ai-max-mv').value) : null,
      initial_capital: parseFloat(document.getElementById('ai-capital').value) || null,
      position_size: parseFloat(document.getElementById('ai-position-size').value) || null,
      use_portfolio: document.getElementById('ai-use-portfolio').checked,
      use_hot_concept: document.getElementById('ai-use-hot-concept').checked,
      hot_concept_top_n: parseInt(document.getElementById('ai-hot-concept-top-n').value) || 5,
    })
  });
  const data = await resp.json();
  if (data.status !== 'started') {
    alert(data.message || '启动失败');
    document.getElementById('ai-bt-start-btn').style.display = '';
    document.getElementById('ai-bt-stop-btn').style.display  = 'none';
    return;
  }
  _startAIPoll();
}

function _startAIPoll() {
  if (_aiPollTimer) clearInterval(_aiPollTimer);
  _aiPollTimer = setInterval(_pollAIStatus, 1500);
}

// ── 实时行情轮询兜底（WebSocket 不通时也能更新自选股+持仓价格）──
var _quotePollTimer = null;
function startQuotePolling() {
  if (_quotePollTimer) return;
  _quotePollTimer = setInterval(pollLiveQuotes, 5000);
}
async function pollLiveQuotes() {
  try {
    var codes = [];
    document.querySelectorAll('#watchlist-tbody tr.wl-row, #sim-pos-tbody tr.pos-row').forEach(function(tr) {
      var c = tr.getAttribute('data-code');
      if (c) codes.push(c);
    });
    // 加入指数
    ['000001.SH','399001.SZ','399006.SZ'].forEach(function(c) { codes.push(c); });
    codes = [...new Set(codes)];
    if (codes.length === 0) return;

    var r = await fetch('/api/quotes/live', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({codes: codes})
    }).then(function(r) { return r.json(); });
    if (r.status !== 'ok' || !r.data) return;
    var d = r.data;
    window._lastQuotes = d;

    // 更新指数
    if (d['000001.SH']) setIndex('sh', d['000001.SH']);
    if (d['399001.SZ']) setIndex('sz', d['399001.SZ']);
    if (d['399006.SZ']) setIndex('cy', d['399006.SZ']);

    // 更新自选股和持仓行
    document.querySelectorAll('#watchlist-tbody tr.wl-row, #sim-pos-tbody tr.pos-row').forEach(function(tr) {
      var c = tr.getAttribute('data-code');
      if (!c || !d[c]) return;
      var q = d[c];
      var pEl = tr.querySelector('.live-price') || tr.querySelector('.pos-price');
      var pctEl = tr.querySelector('.live-pct') || tr.querySelector('.pos-pct');
      if (pEl && q.price > 0) {
        pEl.textContent = q.price.toFixed(2);
        pEl.style.color = q.change_pct >= 0 ? '#ef232a' : '#14b143';
      }
      if (pctEl) {
        pctEl.textContent = (q.change_pct >= 0 ? '+' : '') + q.change_pct.toFixed(2) + '%';
        pctEl.style.color = q.change_pct >= 0 ? '#ef232a' : '#14b143';
      }
    });
  } catch(e) {}
}
// 页面加载后自动启动
startQuotePolling();

async function _pollAIStatus() {
  try {
    const resp = await fetch('/api/backtest/ai/status');
    const data = await resp.json();

    // 更新进度
    const phaseLabelMap = {
      'idle':           '⏸ 空闲',
      'loading':        '📊 加载数据与生成信号...',
      'llm_cold_start': '🤖 LLM 分析策略，设计搜索空间...',
      'exploring':      '🔍 拉丁超立方探索采样中',
      'llm_refine':     '🤖 LLM 精化搜索空间...',
      'refining':       '⚙️ Optuna 贝叶斯精化中',
      'wfo':            '📈 Walk-Forward 稳健性验证...',
      'reporting':      '🤖 LLM 生成分析报告...',
      'done':           '✅ 优化完成',
      'stopped':        '⏹ 已停止',
      'error':          '❌ 发生错误',
    };
    const phaseEl = document.getElementById('ai-phase-label');
    const phaseText = phaseLabelMap[data.phase] || data.phase;
    phaseEl.textContent = data.phase_detail ? `${phaseText} — ${data.phase_detail}` : phaseText;

    // 更新阶段状态标记
    const badge = document.getElementById('ai-status-badge');
    if (!data.running) {
      badge.textContent = data.phase === 'done' ? '✅ 完成' : data.phase === 'error' ? '❌ 失败' : '⏸ 空闲';
      badge.style.color = data.phase === 'done' ? 'var(--green)' : data.phase === 'error' ? 'var(--red)' : '#888';
    } else {
      badge.textContent = '🔄 运行中';
      badge.style.color = 'var(--blue)';
    }

    const cur = data.trial_current || 0;
    const tot = data.trial_total   || 62;
    document.getElementById('ai-trial-label').textContent = `${cur} / ${tot}`;
    document.getElementById('ai-progress-bar').style.width = `${Math.min(100, Math.max(0, cur/tot*100))}%`;

    // 实时日志（由 WebSocket 推送，此处仅做进度补全）

    // 渲染结果表
    if (data.top10 && data.top10.length > 0) {
      _renderAIResults(data.top10);
      document.getElementById('ai-results-card').style.display = '';
    }

    // LLM 报告
    if (data.llm_report) {
      document.getElementById('ai-llm-report').textContent = data.llm_report;
      document.getElementById('ai-report-card').style.display = '';
    }

    // 完成/停止/错误
    if (!data.running) {
      clearInterval(_aiPollTimer);
      _aiPollTimer = null;
      document.getElementById('ai-bt-start-btn').style.display = '';
      document.getElementById('ai-bt-stop-btn').style.display  = 'none';
      document.getElementById('ai-progress-bar').style.width = '100%';

      if (data.error) {
        document.getElementById('ai-phase-label').textContent = `❌ ${data.error}`;
      } else {
        // 自动刷新AI回测历史列表
        setTimeout(() => loadAIBacktestHistory(), 1200);
      }
    }
  } catch(e) {
    console.warn('AI 状态轮询失败:', e);
  }
}

function _renderAIResults(top10) {
  const tbody = document.getElementById('ai-results-tbody');
  const medals = ['🥇','🥈','🥉'];
  tbody.innerHTML = top10.map((r, i) => {
    const pnl    = r.avg_pnl != null ? (r.avg_pnl > 0 ? `+${r.avg_pnl.toFixed(2)}%` : `${r.avg_pnl.toFixed(2)}%`) : '--';
    const pnlCls = r.avg_pnl > 0 ? 'style="color:var(--red)"' : (r.avg_pnl < 0 ? 'style="color:var(--green)"' : '');
    const wfe    = r.wfe != null ? r.wfe : 'N/A';
    const wfeSt  = r.wfe_status || '';
    const oos    = r.oos_pnl != null ? `${r.oos_pnl > 0 ? '+':'' }${r.oos_pnl.toFixed(2)}%` : '--';
    return `<tr>
      <td>${medals[i] || `#${r.rank}`}</td>
      <td ${pnlCls}><b>${pnl}</b></td>
      <td>${r.win_rate != null ? r.win_rate.toFixed(1)+'%' : '--'}</td>
      <td style="color:var(--red)">${r.max_dd != null ? '-'+r.max_dd.toFixed(2)+'%' : '--'}</td>
      <td title="${wfeSt}">${wfe} ${wfeSt ? (wfe >= 0.6 ? '✅' : wfe >= 0.4 ? '⚠️' : '❌') : ''}</td>
      <td>${oos}</td>
      <td>${r.n_trades || '--'}</td>
      <td><button class="btn btn-sm btn-success" onclick="showApplyModal(${i})">✅ 应用</button></td>
    </tr>`;
  }).join('');
  window._aiTop10Cache = top10;
}

function showApplyModal(idx) {
  const r = (window._aiTop10Cache || [])[idx];
  if (!r) return;
  _aiPendingParams = r.params;
  const p = r.params;
  const preview = [
    `止损:          ${p.hard_stop_loss_pct?.toFixed(2)}%`,
    `转保本触发:    ${p.breakeven_threshold_pct?.toFixed(2)}%`,
    `保本止损位:    ${p.breakeven_stop_pnl_pct?.toFixed(2)}%`,
    `移动止盈激活:  ${p.trailing_activate_pct?.toFixed(2)}%`,
    `移动止盈回撤:  ${p.trailing_drawdown_pct?.toFixed(2)}%`,
    `时间止盈天数:  ${p.time_exit_days} 天`,
    `第一档止盈:    ${p.tp1_profit?.toFixed(2)}% (卖 ${(p.tp1_ratio*100).toFixed(0)}%)`,
    `第二档止盈:    ${p.tp2_profit?.toFixed(2)}% (全清)`,
  ].join('\n');
  document.getElementById('ai-apply-params-preview').textContent = preview;
  document.getElementById('ai-apply-modal').style.display = 'flex';
}

async function confirmApplyParams() {
  if (!_aiPendingParams) return;
  document.getElementById('ai-apply-modal').style.display = 'none';
  const resp = await fetch('/api/backtest/ai/apply', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ params: _aiPendingParams })
  });
  const data = await resp.json();
  if (data.status === 'ok') {
    showToast(`✅ ${data.message}`);
  } else {
    showToast(`❌ 应用失败: ${data.message}`, 'error');
  }
}

async function stopAIBacktest() {
  await fetch('/api/backtest/ai/stop', { method: 'POST' });
  document.getElementById('ai-phase-label').textContent = '⏹ 停止中...';
}

// 用 WebSocket 推送更新进度日志
document.addEventListener('DOMContentLoaded', () => {
  const origWsMsg = window._handleWsMessage;
  window._handleWsMessage = function(msg) {
    if (origWsMsg) origWsMsg(msg);
    if (msg && (msg.type === 'log' || msg.type === 'done')) {
      const logBox = document.getElementById('ai-log-box');
      if (logBox) {
        const txt = msg.msg || '';
        if (txt) {
          logBox.innerHTML += txt + '\n';
          logBox.scrollTop = logBox.scrollHeight;
        }
      }
    }
  };
  // 页面初始化时加载策略列表
  loadStrategies();
});


async function startScan() {
  const strategy = document.getElementById('scan-strategy').value;
  const payload = {
    strategy,
    exchanges: Array.from(scanExchanges),
    sectors: Array.from(scanSelectedSectors),
    is_hot: document.getElementById('scan-hot') ? document.getElementById('scan-hot').checked : false,
    advanced_factors: {
      min_mcap: document.getElementById('fac-mcap').value,
      max_pe: document.getElementById('fac-pe').value
    }
  };
  addLog('info', '正在启动全量市场扫描...');
  fetch('/api/screener/start', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).then(r => r.json()).then(res => {
     if(res.status==='ok') toggleTaskButtons('scan', true);
     else addLog('error', res.message);
  }).catch(e => addLog('error', '发送扫描请求失败: ' + e));
}

// ─── UI 辅助 ──────────────────────────────────────────────────
function toggleCollapse(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
  const arrow = document.getElementById(id.replace('panel','arrow'));
  if (arrow) arrow.style.transform = el.style.display === 'none' ? 'rotate(-90deg)' : 'rotate(0deg)';
}
function toggleRSIRange(prefix) {
  const ck = document.getElementById(prefix+'-use-rsi');
  const range = document.getElementById(prefix+'-rsi-range');
  if (range) range.style.display = ck && ck.checked ? 'inline' : 'none';
}
function toggleWRParam(prefix) {
  const ck = document.getElementById(prefix+'-use-wr');
  const el = document.getElementById(prefix+'-wr-max');
  if (el) el.disabled = !(ck && ck.checked);
}

// 收集回测风险参数（仅当用户填写了才覆盖）
function collectRiskParams(prefix) {
  const rp = {};
  const getv = (id, fn) => { const el = document.getElementById(prefix+'-'+id); if (!el || el.value === '') return; if (fn) rp[id] = fn(el.value); else rp[id] = el.value; };
  const getf = (id) => getv(id, parseFloat);
  const geti = (id) => getv(id, v => parseInt(v));

  getf('hard-sl'); geti('time-exit'); geti('force-exit'); getf('time-min-pnl');
  getf('trail-act'); getf('trail-dd'); getf('be-act'); getf('be-stop');

  const atrCk = document.getElementById(prefix+'-atr-stop');
  if (atrCk && atrCk.checked) {
    rp['use_atr_stop'] = true;
    rp['atr_stop_multiplier'] = parseFloat(document.getElementById(prefix+'-atr-mul').value) || 2.5;
  }

  // Staged TP
  const tp1El = document.getElementById(prefix+'-tp1-pct');
  const tp2El = document.getElementById(prefix+'-tp2-pct');
  const tp1Pct = tp1El ? parseFloat(tp1El.value) : NaN;
  const tp1Ratio = parseFloat((document.getElementById(prefix+'-tp1-ratio') || {}).value);
  const tp2Pct = tp2El ? parseFloat(tp2El.value) : NaN;
  const tp2Ratio = parseFloat((document.getElementById(prefix+'-tp2-ratio') || {}).value);
  const tp2All = document.getElementById(prefix+'-tp2-all');
  if (!isNaN(tp1Pct) || !isNaN(tp2Pct)) {
    rp['staged_take_profit'] = [];
    if (!isNaN(tp1Pct)) rp['staged_take_profit'].push({profit_pct:tp1Pct, sell_ratio:tp1Ratio||0, label:'TP1'});
    if (!isNaN(tp2Pct)) rp['staged_take_profit'].push({profit_pct:tp2Pct, sell_ratio:tp2Ratio||0, label:'TP2', sell_all: tp2All ? tp2All.checked : false});
  }
  return rp;
}

// 收集策略参数（仅当用户填写了才覆盖）
function collectStrategyParams(prefix) {
  const sp = {};
  const getv = (id, fn) => { const el = document.getElementById(prefix+'-'+id.replace(/_/g,'-')); if (!el || el.value === '') return; if (fn) sp[id] = fn(el.value); else sp[id] = el.value; };
  getv('vol_threshold', parseFloat);
  const capEl = document.getElementById(prefix+'-daily-cap');
  if (capEl && capEl.value !== '') sp['daily_signal_cap'] = parseInt(capEl.value);
  getv('close_position_threshold', parseFloat);
  getv('breadth_threshold', parseFloat);

  ['use-rsi','use-macd','use-wr'].forEach(id => {
    const el = document.getElementById(prefix+'-'+id);
    if (!el) return;
    const key = id.replace(/-/g,'_');
    sp[key] = el.checked;
  });
  if (sp['use_rsi']) {
    const minEl = document.getElementById(prefix+'-rsi-min'), maxEl = document.getElementById(prefix+'-rsi-max');
    if (minEl) sp['rsi_min'] = parseInt(minEl.value) || 40;
    if (maxEl) sp['rsi_max'] = parseInt(maxEl.value) || 75;
  }
  if (sp['use_wr']) {
    const wrMax = document.getElementById(prefix+'-wr-max');
    if (wrMax) sp['wr_max'] = parseInt(wrMax.value) || -20;
  }
  return sp;
}

async function runBacktest() {
  const strategy_name = document.getElementById('bt-strategy').value;
  const payload = {
    strategy_name,
    strategy_params: collectStrategyParams('bt'),
    risk_params: collectRiskParams('bt'),
    exchanges: Array.from(btExchanges),
    sectors: Array.from(btSelectedSectors),
    start: document.getElementById('bt-start').value,
    end: document.getElementById('bt-end').value,
    index_filter: getIndexFilter('bt'),
    min_mv: document.getElementById('bt-min-mv').value ? parseFloat(document.getElementById('bt-min-mv').value) : null,
    max_mv: document.getElementById('bt-max-mv').value ? parseFloat(document.getElementById('bt-max-mv').value) : null,
    initial_capital: parseFloat(document.getElementById('bt-capital').value) || null,
    position_size: parseFloat(document.getElementById('bt-position-size').value) || null,
    use_portfolio: document.getElementById('bt-use-portfolio').checked,
    use_hot_concept: document.getElementById('bt-use-hot-concept').checked,
    hot_concept_top_n: parseInt(document.getElementById('bt-hot-concept-top-n').value) || 5,
  };
  addLog('info', `正在启动回测 (策略:${strategy_name}, 期间:${payload.start}~${payload.end})...`);
  fetch('/api/backtest', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).then(r => r.json()).then(res => {
     if(res.status==='started') toggleTaskButtons('backtest', true);
     else addLog('error', res.message || '启动失败');
  }).catch(e => addLog('error', '启动回测任务失败: ' + e));
}

// 获取指定 tab 的指数过滤选项
function getIndexFilter(prefix) {
  const ids = [`${prefix}-idx-hs300`, `${prefix}-idx-zz500`, `${prefix}-idx-zz1000`];
  return ids
    .map(id => document.getElementById(id))
    .filter(el => el && el.checked)
    .map(el => el.value);
}

// 同步指数成分股
async function syncIndexMembers() {
  const btn = document.getElementById('sync-index-members-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 同步中...'; }
  try {
    const res = await fetch('/api/data/sync_index_members', { method: 'POST' });
    const data = await res.json();
    addLog('info', data.message || '指数成分同步任务已启动');
    setTimeout(() => loadIndexMemberStatus(), 3000);
  } catch(e) {
    addLog('error', '同步指数成分失败: ' + e);
  } finally {
    if (btn) { setTimeout(() => { btn.disabled = false; btn.textContent = '🔄 同步指数成分股'; }, 5000); }
  }
}

// 加载指数成分状态
async function loadIndexMemberStatus() {
  try {
    const [statusRes, nameMapRes] = await Promise.all([
      fetch('/api/data/index_members/status'),
      fetch('/api/data/index_display_map'),
    ]);
    const data = await statusRes.json();
    const nameMapData = await nameMapRes.json();
    const nameMap = nameMapData.status === 'ok' ? nameMapData.data : {};
    const tbody = document.getElementById('index-members-tbody');
    if (!tbody) return;
    if (!data.data || data.data.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3" style="color:var(--text2);text-align:center;padding:8px">暂无数据，请先同步</td></tr>';
      return;
    }
    tbody.innerHTML = data.data.map(r => `
      <tr>
        <td style="padding:4px 8px">${nameMap[r.index_code] || r.index_code}</td>
        <td style="text-align:right;padding:4px 8px;color:var(--green);font-weight:bold">${r.count}</td>
        <td style="text-align:right;padding:4px 8px;color:var(--text2);font-size:11px">${(r.last_updated||'').substring(0,16)}</td>
      </tr>`).join('');
  } catch(e) { console.error('loadIndexMemberStatus:', e); }
}

// 加载回测历史
async function loadBacktestHistory() {
  const container = document.getElementById('bt-history-list');
  if (!container) return;
  container.innerHTML = '<div style="color:var(--text2);font-size:12px;text-align:center;padding:8px">加载中...</div>';
  try {
    const res = await fetch('/api/backtest/history?limit=20');
    const data = await res.json();
    if (!data.data || data.data.length === 0) {
      container.innerHTML = '<div style="color:var(--text2);font-size:12px;text-align:center;padding:8px">暂无回测历史</div>';
      return;
    }
    container.innerHTML = `<table style="font-size:12px;width:100%;border-collapse:collapse">
      <thead><tr style="border-bottom:1px solid var(--border)">
        <th style="text-align:left;padding:4px 8px">时间</th>
        <th style="text-align:left;padding:4px 8px">策略</th>
        <th style="text-align:center;padding:4px 8px">股票池</th>
        <th style="text-align:right;padding:4px 8px">交易数</th>
        <th style="text-align:right;padding:4px 8px">胜率</th>
        <th style="text-align:right;padding:4px 8px">平盈率</th>
        <th style="padding:4px 8px">操作</th>
      </tr></thead>
      <tbody>${data.data.map(r => {
        const idx = JSON.parse(r.index_filter||'[]').join('+') || '全市场';
        const pnlColor = parseFloat(r.avg_pnl_pct) > 0 ? 'var(--red)' : 'var(--green)';
        return `<tr style="border-bottom:1px solid rgba(255,255,255,0.05)">
          <td style="padding:4px 8px;color:var(--text2)">${(r.created_at||'').substring(0,16)}</td>
          <td style="padding:4px 8px">${r.strategy_name}</td>
          <td style="text-align:center;padding:4px 8px;font-size:11px;color:var(--accent)">${idx}</td>
          <td style="text-align:right;padding:4px 8px">${r.total_trades}</td>
          <td style="text-align:right;padding:4px 8px">${parseFloat(r.win_rate||0).toFixed(1)}%</td>
          <td style="text-align:right;padding:4px 8px;color:${pnlColor};font-weight:bold">${parseFloat(r.avg_pnl_pct||0).toFixed(2)}%</td>
          <td style="padding:4px 8px"><button class="btn btn-sm" onclick="viewBacktestHistory(${r.id})" style="font-size:11px;padding:2px 6px">详情</button>
          <button class="btn btn-sm" onclick="deleteBacktestHistory(${r.id})" style="font-size:11px;padding:2px 6px;color:var(--red);border-color:var(--red)">删除</button></td>
        </tr>`;
      }).join('')}</tbody></table>`;
  } catch(e) { container.innerHTML = `<div style="color:var(--red);font-size:12px;text-align:center">${e}</div>`; }
}

async function viewBacktestHistory(id) {
  try {
    const res = await fetch(`/api/backtest/history/${id}`);
    const data = await res.json();
    if (data.status !== 'ok') { alert('加载失败'); return; }
    const d = data.data;
    const idx = JSON.parse(d.index_filter||'[]').join(', ') || '全市场';
    const trades = JSON.parse(d.trades_json||'[]');
    const html = `<div style="font-size:13px"><b>策略：</b>${d.strategy_name} &nbsp; <b>期间：</b>${d.start_date} ~ ${d.end_date}</div>
      <div style="font-size:12px;color:var(--text2);margin:4px 0"><b>股票池：</b>${idx} | <b>市值：</b>${d.min_mv||'不限'}~${d.max_mv||'不限'}亿 | <b>交易数：</b>${d.total_trades} | <b>胜率：</b>${parseFloat(d.win_rate).toFixed(1)}% | <b>平盈率：</b>${parseFloat(d.avg_pnl_pct).toFixed(2)}%</div>
      <div id="bt-modal-chart" style="width:100%; height:220px; margin-top:10px"></div>
      <div style="max-height:300px;overflow-y:auto;margin-top:8px"><table style="font-size:11px;width:100%"><thead><tr><th>代码</th><th>名称</th><th>买入</th><th>卖出</th><th>盈亏%</th><th>原因</th></tr></thead>
      <tbody>${trades.slice(0,50).map(t=>`<tr><td>${t.code}</td><td>${t.name}</td><td>${t.entry_price}</td><td>${t.exit_price||'-'}</td><td style="color:${parseFloat(t.pnl_pct)>0?'var(--red)':'var(--green)'}">${parseFloat(t.pnl_pct||0).toFixed(2)}%</td><td>${t.exit_reason||'-'}</td></tr>`).join('')}</tbody></table></div>`;
    showModal(`📊 回测历史 #${id}`, html);
    setTimeout(() => renderMonthlyInsight(trades, 'bt-modal-chart'), 100);
  } catch(e) { alert('加载失败: ' + e); }
}

async function deleteBacktestHistory(id) {
  if (!confirm(`确认删除回测历史 #${id}？`)) return;
  await fetch(`/api/backtest/history/${id}`, { method: 'DELETE' });
  loadBacktestHistory();
}

// ─── AI 回测历史 JS ──────────────────────────────────────────────
async function loadAIBacktestHistory() {
  const container = document.getElementById('ai-bt-history-list');
  if (!container) return;
  container.innerHTML = '<div style="color:var(--text2);font-size:12px;text-align:center;padding:8px">加载中...</div>';
  try {
    const res = await fetch('/api/backtest/ai/history?limit=20');
    const data = await res.json();
    if (!data.data || data.data.length === 0) {
      container.innerHTML = '<div style="color:var(--text2);font-size:12px;text-align:center;padding:8px">暂无AI回测历史</div>';
      return;
    }
    container.innerHTML = `<table style="font-size:12px;width:100%;border-collapse:collapse">
      <thead><tr style="border-bottom:1px solid var(--border)">
        <th style="text-align:left;padding:4px 8px">时间</th>
        <th style="text-align:left;padding:4px 8px">策略</th>
        <th style="text-align:center;padding:4px 8px">股票池</th>
        <th style="text-align:right;padding:4px 8px">最优平盈率</th>
        <th style="text-align:right;padding:4px 8px">最优胜率</th>
        <th style="text-align:center;padding:4px 8px">LLM</th>
        <th style="padding:4px 8px">操作</th>
      </tr></thead>
      <tbody>${data.data.map(r => {
        const idx = JSON.parse(r.index_filter||'[]').join('+') || '全市场';
        const pnlColor = parseFloat(r.best_avg_pnl||0) > 0 ? 'var(--red)' : 'var(--green)';
        const hasLLM = r.llm_report && r.llm_report.length > 10;
        return `<tr style="border-bottom:1px solid rgba(255,255,255,0.05)">
          <td style="padding:4px 8px;color:var(--text2)">${(r.created_at||'').substring(0,16)}</td>
          <td style="padding:4px 8px">${r.strategy_name}</td>
          <td style="text-align:center;padding:4px 8px;font-size:11px;color:var(--accent)">${idx}</td>
          <td style="text-align:right;padding:4px 8px;color:${pnlColor};font-weight:bold">${parseFloat(r.best_avg_pnl||0).toFixed(2)}%</td>
          <td style="text-align:right;padding:4px 8px">${parseFloat(r.best_win_rate||0).toFixed(1)}%</td>
          <td style="text-align:center;padding:4px 8px">${hasLLM ? '✅' : '—'}</td>
          <td style="padding:4px 8px">
            <button class="btn btn-sm" onclick="viewAIBacktestHistory(${r.id})" style="font-size:11px;padding:2px 6px">详情</button>
            <button class="btn btn-sm" onclick="deleteAIBacktestHistory(${r.id})" style="font-size:11px;padding:2px 6px;color:var(--red);border-color:var(--red)">删除</button>
          </td>
        </tr>`;
      }).join('')}</tbody></table>`;
  } catch(e) { container.innerHTML = `<div style="color:var(--red);font-size:12px;text-align:center">${e}</div>`; }
}

async function viewAIBacktestHistory(id) {
  try {
    const res = await fetch(`/api/backtest/ai/history/${id}`);
    const data = await res.json();
    if (data.status !== 'ok') { alert('加载失败'); return; }
    const d = data.data;
    const idx = JSON.parse(d.index_filter||'[]').join(', ') || '全市场';
    const top10 = JSON.parse(d.top10_json||'[]');
    const bestParams = JSON.parse(d.best_params||'{}');
    const html = `
      <div style="margin-bottom:10px">
        <div style="font-size:13px"><b>策略：</b>${d.strategy_name} &nbsp; <b>期间：</b>${d.start_date} ~ ${d.end_date}</div>
        <div style="font-size:12px;color:var(--text2);margin-top:4px">
          <b>股票池：</b>${idx} | <b>市值：</b>${d.min_mv||'不限'}~${d.max_mv||'不限'}亿 |
          <b>最优平盈率：</b><span style="color:var(--green)">${parseFloat(d.best_avg_pnl||0).toFixed(2)}%</span> |
          <b>最优胜率：</b>${parseFloat(d.best_win_rate||0).toFixed(1)}%
        </div>
      </div>
      ${top10.length > 0 ? `<div style="margin-bottom:10px">
        <b style="font-size:13px">Top-10 参数组合：</b>
        <table style="font-size:11px;width:100%;margin-top:6px;border-collapse:collapse">
          <thead><tr style="border-bottom:1px solid var(--border)">
            <th style="padding:3px 6px">排名</th><th style="padding:3px 6px">平盈率</th>
            <th style="padding:3px 6px">胜率</th><th style="padding:3px 6px">WFE</th>
            <th style="padding:3px 6px">参数</th>
          </tr></thead>
          <tbody>${top10.slice(0,10).map((t,i)=>{
            // 参数汉化映射表
            const paramNameMap = {
              'hard_stop_loss_pct': '硬止损',
              'breakeven_threshold_pct': '保本触发',
              'breakeven_stop_pnl_pct': '保本止盈',
              'trailing_activate_pct': '追踪激活',
              'trailing_drawdown_pct': '回撤止盈',
              'time_exit_days': '时间清仓',
              'time_exit_min_profit_pct': '清仓要求',
              'tp1_profit': '一阶止盈',
              'tp2_profit': '二阶止盈',
              'tp1_ratio': '一阶比例',
              'tp2_ratio': '二阶比例'
            };

            const renderedParams = Object.entries(t.params || {}).map(([k, v]) => {
              const label = paramNameMap[k] || k;
              const val = typeof v === 'number' ? v.toFixed(1) : v;
              const unit = k.includes('days') ? '天' : '%';
              return `<span title="${k}: ${v}" style="background:rgba(255,255,255,0.08);color:#eee;padding:1px 5px;border-radius:3px;margin:2px;display:inline-block;border:1px solid rgba(255,255,255,0.1);font-size:10px"><b>${label}</b>:${val}${unit}</span>`;
            }).join('');
            
            const pnlColor = parseFloat(t.avg_pnl||0) > 0 ? 'var(--red)' : 'var(--green)';
            
            return `<tr style="border-bottom:1px solid rgba(255,255,255,0.04)">
              <td style="padding:3px 6px">${['🥇','🥈','🥉'][i]||'#'+(i+1)}</td>
              <td style="padding:3px 6px;color:${pnlColor};font-weight:bold">${parseFloat(t.avg_pnl||0).toFixed(2)}%</td>
              <td style="padding:3px 6px">${parseFloat(t.win_rate||0).toFixed(1)}%</td>
              <td style="padding:3px 6px">${t.wfe!=null?parseFloat(t.wfe).toFixed(3):'—'}</td>
              <td style="padding:3px 6px">
                <div style="display:flex; align-items:center; justify-content:space-between">
                   <div style="flex:1">${renderedParams}</div>
                   <button onclick='applyAIParams(${JSON.stringify(t.params)})' style="margin-left:8px; padding:3px 10px; font-size:11px; font-weight:bold; background:var(--accent); border:none; color:#fff; border-radius:3px; cursor:pointer; flex-shrink:0">写入配置</button>
                </div>
              </td>
            </tr>`;
          }).join('')}</tbody>
        </table>
      </div>` : ''}
      ${d.llm_report ? `<div style="margin-top:10px"><b style="font-size:13px">LLM 分析报告：</b>
        <div style="background:rgba(0,0,0,0.3);border-radius:4px;padding:10px;margin-top:6px;font-size:12px;white-space:pre-wrap;max-height:200px;overflow-y:auto">${d.llm_report}</div>
      </div>` : ''}`;
    showModal(`🤖 AI 回测历史 #${id}`, html);
  } catch(e) { alert('加载失败: ' + e); }
}

async function applyAIParams(params) {
  if(!confirm('确定要将该组 AI 优化参数写入系统实时风控配置吗？这将覆盖现有设置。')) return;
  try {
    const res = await fetch('/api/backtest/ai/apply', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ params })
    }).then(r => r.json());
    
    if(res.status === 'ok') {
        alert('✅ 参数已成功写入系统配置并立即生效！\n' + (res.applied || []).join('\n'));
        // 自动刷新一下设置面板（如果有的话）
        if(typeof loadSettings === 'function') loadSettings();
    } else {
        alert('❌ 写入失败: ' + res.message);
    }
  } catch(err) { alert('请求异常: ' + err); }
}

async function deleteAIBacktestHistory(id) {
  if (!confirm(`确认删除AI回测历史 #${id}？`)) return;
  await fetch(`/api/backtest/ai/history/${id}`, { method: 'DELETE' });
  loadAIBacktestHistory();
}

// ─── Task Control ─────────────────────────────────────────────
function toggleTaskButtons(type, isRunning) {
  const mapping = {
    'scan': { start: 'btn-start-scan', stop: 'btn-stop-scan' },
    'backtest': { start: 'btn-start-backtest', stop: 'btn-stop-backtest' },
    'dl': { start: null, stop: null } // dl uses different progress UI
  };
  const ids = mapping[type] || { start: 'btn-start-' + type, stop: 'btn-stop-' + type };
  const startBtn = ids.start ? document.getElementById(ids.start) : null;
  const stopBtn = ids.stop ? document.getElementById(ids.stop) : null;
  if (startBtn) startBtn.style.display = isRunning ? 'none' : 'inline-block';
  if (stopBtn) stopBtn.style.display = isRunning ? 'inline-block' : 'none';
}

async function stopTask(type) {
  try {
    const res = await fetch('/api/tasks/stop', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ type })
    }).then(r => r.json());
    
    if (res.status === 'ok') {
      addLog('warn', `已发送中止信号给 ${type} 引擎，请稍候...`);
    } else {
      alert(res.message);
    }
  } catch(e) { alert('停止请求失败: ' + e); }
}

function handleWS(msg) {
  function getMsg(m) { return m.msg || m.message || m.content || ""; }

  if (msg.type === 'progress') {
    const ctx = msg.context || 'scan';
    updateProgress(ctx, msg.step, msg.total, getMsg(msg));
  } else if (msg.type === 'screener_progress') {
    updateProgress('scan', msg.step, msg.total, getMsg(msg));
  } else if (msg.type === 'scan_done') {
    console.log('Received scan_done', msg);
    try {
      renderScanResults(msg.results);
      addLog('ok', `选股完成: 成功加载 ${msg.count || (msg.results ? msg.results.length : 0)} 条数据`);
    } catch (e) {
      console.error('Render results failed:', e);
      addLog('error', '渲染结果失败: ' + e.message);
    }
    hideProgress('scan');
    loadScanHistory(); 
    toggleTaskButtons('scan', false);
  } else if (msg.type === 'backtest_done') {
    renderBacktestResults(msg.summary, msg.stocks);
    if (msg.portfolio) renderPortfolioResults(msg.portfolio);
    addLog('ok', `回测完成: 胜率${msg.summary.win_rate}% 平均${msg.summary.avg_pnl_pct}%`);
    hideProgress('backtest');
    toggleTaskButtons('backtest', false);
    // 自动刷新回测历史列表
    setTimeout(() => loadBacktestHistory(), 800);
  } else if (msg.type === 'simple_bt_done') {
    _lastSimpleBtResult = { summary: msg.summary, equity: msg.equity, trades: msg.trades, indices: msg.indices, dailyTrades: msg.daily_trades, resultId: msg.result_id };
    renderSimpleBtResults(msg.summary, msg.equity, msg.trades, msg.indices);
    _simpleBtDailyTrades = msg.daily_trades || {};
    setTimeout(() => loadSimpleBtHistory(), 500);
    addLog('ok', '回测完成: 收益' + msg.summary.total_return + '% DD' + msg.summary.max_drawdown + '% Calmar' + msg.summary.calmar.toFixed(2));
    hideProgress('simple-bt');
  } else if (msg.type === 'backtest_progress' && msg.context === 'simple_bt') {
    showProgress('simple-bt', msg.msg);
    updateProgressFill('simple-bt', msg.step, msg.total);
  } else if (msg.type === 'done') {
    addLog('ok', getMsg(msg));
    hideProgress('dl');
    const dlDone = document.getElementById('dl-done');
    if (dlDone) dlDone.style.display = 'block';
  } else if (msg.type === 'log') {
    addLog(msg.level || 'info', getMsg(msg));
  } else if (msg.type === 'info') {
    addLog('ok', getMsg(msg)); 
  } else if (msg.type === 'error') {
    addLog('error', getMsg(msg));
    toggleTaskButtons('scan', false);
    toggleTaskButtons('backtest', false);
  } else if (msg.type === 'risk') {
    addLog('warn', `风控触发: ${msg.code} ${msg.reason}`);
    toggleTaskButtons('scan', false);
    toggleTaskButtons('backtest', false);
  } else if (msg.type === 'strategy_test_result') {
    addLog(msg.status, `策略 [${msg.strategy_name}] 试跑结果: ${msg.message}`);
  } else if (msg.type === 'strategy_saved') {
    addLog('ok', `策略 [${msg.strategy_name}] 已保存并热加载`);
    loadStrategies();
  } else if (msg.type === 'qmt_sync_done') {
    try {
      addLog(msg.level || 'ok', getMsg(msg));
      const startBtn = document.getElementById('qmt-start-btn');
      const stopBtn = document.getElementById('qmt-stop-btn');
      if (startBtn) { startBtn.style.display = 'inline-flex'; } else { addLog('error', 'startBtn not found!'); }
      if (stopBtn) { stopBtn.style.display = 'none'; } else { addLog('error', 'stopBtn not found!'); }
      
      const pBar = document.getElementById('qmt-dl-progress');
      if (pBar) { pBar.classList.remove('active'); } else { addLog('error', 'qmt-dl-progress not found!'); }
    } catch (e) {
      addLog('error', 'qmt_sync_done Error: ' + e.toString());
    }
  } else if (msg.type === 'sim_trader_daily' || msg.type === 'sim_trader_update') {
    addLog('ok', `模拟盘: ${msg.today} 买入${msg.buy_count}笔 卖出${msg.sell_count}笔 净值${msg.equity} 持仓${msg.positions}`);
    if (msg.intraday_sell) {
      addLog('warn', `盘中卖出: ${msg.code} ${msg.reason}`);
    }
    loadSimTraderStatus();
  } else if (msg.type === 'sim_trader_log') {
    appendSimLog(msg);
    // 实时推送到交易日志卡片（仅当查看"最新"时）
    var logList = document.getElementById('sim-log-list');
    var logDate = document.getElementById('sim-log-date');
    if (logList && logDate && !logDate.value) {
      var a = msg.action || '';
      var ts = msg.time ? (msg.date||'').substring(5)+' '+msg.time : (msg.date||'').substring(5) || '';
      var line = '';
      if (a === 'buy') {
        line = '<div style="padding:2px 0;border-bottom:1px solid rgba(255,255,255,0.05);font-size:11px"><span style="color:var(--text2)">' + ts + '</span> <b style="color:var(--green)">买入</b> ' + (msg.code||'') + ' ' + (msg.name||'') + ' ' + (msg.price||'') + '元 x' + (msg.shares||0) + '股 金额' + Math.round(msg.cost||0).toLocaleString() + ' 现金' + Math.round(msg.cash||0).toLocaleString() + ' <span style="color:var(--text2)">' + (msg.strategy||'') + '</span></div>';
      } else if (a === 'sell') {
        line = '<div style="padding:2px 0;border-bottom:1px solid rgba(255,255,255,0.05);font-size:11px"><span style="color:var(--text2)">' + ts + '</span> <b style="color:var(--red)">卖出</b> ' + (msg.code||'') + ' ' + (msg.name||'') + ' ' + (msg.price||'') + '元 x' + (msg.shares||0) + '股 盈亏' + (msg.ret_pct!=null?(msg.ret_pct>=0?'+':'')+msg.ret_pct+'%':'') + ' ' + Math.round(msg.profit||0).toLocaleString() + '元 现金' + Math.round(msg.cash||0).toLocaleString() + ' <span style="color:var(--text2)">' + (msg.reason||'') + '</span></div>';
      } else if (a === 'snapshot') {
        line = '<div style="padding:2px 0;border-bottom:1px solid rgba(255,255,255,0.05);font-size:11px"><span style="color:var(--text2)">' + ts + '</span> <b style="color:var(--accent)">快照</b> 净值' + Math.round(msg.equity||0).toLocaleString() + ' 现金' + Math.round(msg.cash||0).toLocaleString() + ' 持仓' + (msg.positions||0) + '只</div>';
      }
      if (line) {
        logList.insertAdjacentHTML('afterbegin', line);
        if (logList.children.length > 200) logList.lastElementChild.remove();
      }
    }
  } else if (msg.type === 'risk_alert') {
    addLog('warn', `[风险告警] ${msg.code} ${msg.reason}（${msg.mode}）`);
    createNotification('warn', `风险告警: ${msg.code} ${msg.reason}`);
  } else if (msg.type === 'tdx_translated') {
    if (msg.status === 'ok') {
      addLog('ok', '通达信公式转译成功');
      newStrategy(msg.python_code);
      closeTdxModal();
    } else {
      addLog('error', `通达信公式转译失败: ${msg.message}`);
    }
  } else if (msg.type === 'tqsdk_progress') {
    showProgress('tqsdk', msg.msg);
    updateProgressFill('tqsdk', msg.step, msg.total);
  } else if (msg.type === 'tqsdk_screen_done') {
    hideProgress('tqsdk');
    toggleTqsdkButtons(false);
    if (msg.status === 'ok') {
      _tqsdkCurrentResults = msg.results || [];
      _tqsdkCurrentResultId = msg.result_id;
      renderTqsdkResults(msg.results);
      addLog('ok', 'QUANTQQ选股完成: ' + (msg.count || 0) + '只');
    } else if (msg.status === 'stopped') {
      addLog('warn', '选股已停止');
    } else {
      addLog('error', '选股失败: ' + (msg.message || '未知错误'));
    }
    loadTqsdkHistory();
  } else if (msg.type === 'market_quotes') {
    const data = msg.data;
    window._lastQuotes = data;  // 缓存给 processPosRows 使用
    window._mqCount = (window._mqCount || 0) + 1;
    if (window._mqCount % 30 === 1) console.log('[行情] WebSocket第' + window._mqCount + '次推送, ' + Object.keys(data).length + '只');
    // 1. 更新顶部指数栏 (如果有数据)
    if (data['000001.SH']) setIndex('sh', data['000001.SH']);
    if (data['399001.SZ']) setIndex('sz', data['399001.SZ']);
    if (data['399006.SZ']) setIndex('cy', data['399006.SZ']);
    if (data['000905.SH']) setIndex('zz500', data['000905.SH']);
    if (data['000510.SH']) setIndex('a500', data['000510.SH']);

    // 2. 更新表格行
    const processRows = (rows) => {
      rows.forEach(tr => {
        const c = tr.getAttribute('data-code');
        const quote = findQuote(data, c);
        if (quote) updateQuoteRow(tr, quote);
      });
    };
    processRows(document.querySelectorAll('#watchlist-tbody tr.wl-row'));
    processRows(document.querySelectorAll('.radar-stock-link'));
    processPosRows(document.querySelectorAll('#sim-pos-tbody tr.pos-row'));
    processPosRows(document.querySelectorAll('#sim-trade-tbody tr.pos-row'));
  } else if (msg.type === 'portfolio_snapshot') {
    // 服务端 10s 计算的实时投资组合快照
    window._portfolioSeen = true;
    const s = msg.data;
    const eqEl = document.getElementById('sim-equity');
    const cashEl = document.getElementById('sim-cash');
    const pnlEl = document.getElementById('sim-total-pnl-val');
    if (eqEl) {
      eqEl.textContent = Math.round(s.equity).toLocaleString();
      eqEl.style.color = s.total_unrealized_pnl >= 0 ? 'var(--red)' : 'var(--green)';
    }
    if (cashEl) cashEl.textContent = Math.round(s.cash).toLocaleString();
    if (pnlEl) {
      pnlEl.textContent = (s.total_unrealized_pnl >= 0 ? '+' : '') + Math.round(s.total_unrealized_pnl).toLocaleString();
      pnlEl.style.color = s.total_unrealized_pnl >= 0 ? 'var(--red)' : 'var(--green)';
    }
    // 更新持仓表每行的 current_price / profit_pct / market_value
    if (s.positions) {
      s.positions.forEach(ps => {
        const row = document.querySelector('#sim-pos-tbody tr.pos-row[data-code="' + ps.code + '"]');
        if (!row) return;
        const pEl = row.querySelector('.pos-price');
        const pctEl = row.querySelector('.pos-pct');
        const mvEl = row.querySelector('.pos-mv');
        if (pEl) pEl.textContent = ps.current_price.toFixed(2);
        if (pctEl) {
          pctEl.textContent = (ps.profit_pct >= 0 ? '+' : '') + ps.profit_pct.toFixed(2) + '%';
          pctEl.style.color = ps.profit_pct >= 0 ? '#ef232a' : '#14b143';
        }
        if (mvEl) mvEl.textContent = Math.round(ps.market_value).toLocaleString();
      });
    }
  }
}

function updateQuoteRow(tr, info) {
    const pEl = tr.querySelector('.live-price');
    const pctEl = tr.querySelector('.live-pct');
    if (!pEl && !pctEl) return;

    const price = parseFloat(info.lastPrice || info.price || 0);
    const preClose = parseFloat(info.lastClose || info.preClose || 0);
    if (price <= 0) return;

    const pct = preClose > 0 ? (price - preClose) / preClose * 100 : 0;
    const color = pct >= 0 ? '#ef232a' : '#14b143';

    if (pEl) {
        pEl.textContent = price.toFixed(2);
        pEl.style.color = color;
    }

    if (pctEl) {
        pctEl.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
        pctEl.style.color = color;
    }
}

// 按代码前缀匹配正确交易所后缀，避免 000905 股票被 000905.SH 指数覆盖
function findQuote(data, code) {
  if (!code) return null;
  // 裸代码直接匹配
  if (data[code]) return data[code];
  // 带后缀的代码，剥离后缀重试
  if (code.includes('.')) {
    const bare = code.split('.')[0];
    if (data[bare]) return data[bare];
    code = bare;
  }
  // 按前缀判断交易所：6开头→上海，0/3开头→深圳
  if (code.startsWith('6')) {
    return data[code + '.SH'] || null;
  }
  if (code.startsWith('0') || code.startsWith('3')) {
    return data[code + '.SZ'] || null;
  }
  return null;
}

function processPosRows(rows) {
  window._posCallCount = (window._posCallCount || 0) + 1;
  if (rows.length > 0 && window._posCallCount % 30 === 1) {
    const qKeys = Object.keys(window._lastQuotes || {});
    console.log('[行情] 第' + window._posCallCount + '次更新, 持仓' + rows.length + '行, 行情' + qKeys.length + '只', qKeys.slice(0, 5));
  }
  rows.forEach(tr => {
    const c = tr.getAttribute('data-code');
    if (!c) return;
    const entryPrice = parseFloat(tr.getAttribute('data-entry') || 0);
    const shares = parseFloat(tr.getAttribute('data-shares') || 0);
    const data = window._lastQuotes || {};
    const quote = findQuote(data, c);
    if (quote) updatePositionRow(tr, quote, entryPrice, shares);
  });
}

function updatePositionRow(tr, info, entryPrice, shares) {
    const pEl = tr.querySelector('.pos-price');
    const pctEl = tr.querySelector('.pos-pct');
    const mvEl = tr.querySelector('.pos-mv');

    const price = parseFloat(info.lastPrice || info.price || 0);
    if (price <= 0) return;

    const profitPct = entryPrice > 0 ? (price - entryPrice) / entryPrice * 100 : 0;
    const color = profitPct >= 0 ? '#ef232a' : '#14b143';
    // 持仓表: pos-mv = 市值;  交易记录表: pos-mv = 盈亏额
    const isTrade = tr.getAttribute('data-type') === 'trade';
    const thirdVal = isTrade ? (price - entryPrice) * shares : price * shares;

    if (pEl) {
        pEl.textContent = price.toFixed(2);
        pEl.style.color = color;
    }
    if (pctEl) {
        pctEl.textContent = (profitPct >= 0 ? '+' : '') + profitPct.toFixed(2) + '%';
        pctEl.style.color = color;
    }
    if (mvEl) {
        mvEl.textContent = Math.round(thirdVal).toLocaleString();
        mvEl.style.color = color;
    }
}

// ─── Sector Logic (Search & Chips) ──────────────────────────────────
let scanSelectedSectors = new Set();
let btSelectedSectors = new Set();
const sectorLabelMap = new Map(); // { id => label }
let curSearchIdx = -1;

function handleSectorSearch(el, type) {
  const q = el.value.trim().toLowerCase();
  const resDiv = document.getElementById(`${type}-search-results`);
  if (!q) {
    resDiv.classList.remove('active');
    return;
  }
  
  const matches = [];
  sectorLabelMap.forEach((label, id) => {
    if (label.toLowerCase().includes(q)) {
      matches.push({ id, label });
    }
  });
  
  // 截取前 10 个匹配项
  const finalists = matches.slice(0, 10);

  if (finalists.length > 0) {
    curSearchIdx = -1;
    resDiv.innerHTML = finalists.map((m, i) => `
      <div class="search-item" onclick="selectSearchMatch('${type}', '${m.id}', '${m.label}')" data-index="${i}">
        ${m.label} <span class="cat">${m.id}</span>
      </div>
    `).join('');
    resDiv.classList.add('active');
  } else {
    resDiv.classList.remove('active');
  }
}

function handleSearchNav(el, e, type) {
  const resDiv = document.getElementById(`${type}-search-results`);
  const items = resDiv.querySelectorAll('.search-item');
  if (!resDiv.classList.contains('active') || items.length === 0) return;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    curSearchIdx = Math.min(curSearchIdx + 1, items.length - 1);
    updateHighlight(items);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    curSearchIdx = Math.max(curSearchIdx - 1, 0);
    updateHighlight(items);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (curSearchIdx >= 0) items[curSearchIdx].click();
    else if (items.length > 0) items[0].click();
  } else if (e.key === 'Escape') {
    resDiv.classList.remove('active');
    el.blur();
  }
}

function updateHighlight(items) {
  items.forEach((it, i) => it.classList.toggle('selected', i === curSearchIdx));
  if (curSearchIdx >= 0) items[curSearchIdx].scrollIntoView({ block: 'nearest' });
}

function selectSearchMatch(type, id, label) {
  const set = type === 'bt' ? btSelectedSectors : scanSelectedSectors;
  if (!set.has(id)) {
    set.add(id);
    renderSectorChips(type);
    addLog('info', `已添加板块: ${label}`);
  }
  const input = document.getElementById(`${type}-sector-search`);
  input.value = '';
  document.getElementById(`${type}-search-results`).classList.remove('active');
}

function removeSectorChip(val, type = 'scan') {
  const set = type === 'bt' ? btSelectedSectors : scanSelectedSectors;
  set.delete(val);
  renderSectorChips(type);
}

function renderSectorChips(type = 'scan') {
  const container = document.getElementById(type === 'bt' ? 'bt-sector-tags' : 'sector-chips');
  const set = type === 'bt' ? btSelectedSectors : scanSelectedSectors;
  let html = "";
  set.forEach(val => {
    let label = sectorLabelMap.get(val) || val;
    html += `
      <div class="chip active" style="min-width:auto; height:28px; padding:0 10px; font-size:12px; display:inline-flex; align-items:center; gap:6px">
        ${label}
        <span style="cursor:pointer; font-weight:bold; font-size:14px; line-height:1" onclick="removeSectorChip('${val}', '${type}')">&times;</span>
      </div>`;
  });
  container.innerHTML = html;
}

let hierarchyTree = []; // 全局缓存行业树

function addSectorChip(type = 'scan') {
  const select = document.getElementById(type === 'bt' ? 'bt-sector' : 'scan-sector');
  const val = select.value;
  if (!val) return;
  
  const set = type === 'bt' ? btSelectedSectors : scanSelectedSectors;
  
  if (val.startsWith('CAT:')) {
    // 根节点点击：自动添加所有子项
    const cat = hierarchyTree.find(c => c.value === val);
    if (cat && cat.children) {
      cat.children.forEach(sub => {
        selectSearchMatch(type, sub.value, sub.label);
      });
      addLog('ok', `已批量添加 [${cat.label}] 下的 ${cat.children.length} 个子板块`);
    }
  } else {
    // 普通单选
    const label = sectorLabelMap.get(val) || val;
    selectSearchMatch(type, val, label);
  }
  select.value = ""; 
}

async function loadSectorHierarchy() {
  try {
    const tree = await fetch('/api/meta/sectors/hierarchy').then(r => r.json());
    hierarchyTree = tree; // 保存到全局
    const scanSelect = document.getElementById('scan-sector');
    const btSelect = document.getElementById('bt-sector');
    
    let scanHtml = '<option value="">📂 浏览行业树...</option>';
    let btHtml = '<option value="">📂 浏览...</option>';

    tree.forEach(cat => {
      sectorLabelMap.set(cat.value, cat.label);
      // 分类根节点，value 以 CAT: 开头
      let groupHtml = `<optgroup label="${cat.label}"><option value="${cat.value}">+ 全选这个分类: ${cat.label}</option>`;
      
      cat.children.forEach(sub => {
        sectorLabelMap.set(sub.value, sub.label);
        groupHtml += `<option value="${sub.value}">${sub.label}</option>`;
      });
      groupHtml += `</optgroup>`;
      scanHtml += groupHtml;
      btHtml += groupHtml;
    });
    
    if (scanSelect) scanSelect.innerHTML = scanHtml;
    if (btSelect) btSelect.innerHTML = btHtml;
    console.log(`已加载 ${sectorLabelMap.size} 个行业节点`);
  } catch (e) { console.error('加载行业树失败', e); }
}

// ─── Log & Progress ───────────────────────────────────────────
function addLog(level, msg) {
  const box = document.getElementById('log-box');
  const div = document.createElement('div');
  div.className = `log-line ${level}`;
  div.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}
function clearLog() { document.getElementById('log-box').innerHTML = ''; }

function updateProgress(ctx, step, total, msg) {
  const prefix = ctx === 'backtest' ? 'bt' : ctx === 'dl' ? 'dl' : 'scan';
  const wrap = document.getElementById(`${prefix}-progress`);
  const fill = document.getElementById(`${prefix}-fill`);
  const msgEl = document.getElementById(`${prefix}-msg`);
  if (!wrap) return;
  wrap.classList.add('active');
  if (fill) fill.style.width = `${Math.round(step / total * 100)}%`;
  if (msgEl) msgEl.textContent = msg;
}
function hideProgress(ctx) {
  // 统一处理所有上下文的进度条隐藏
  if (ctx === 'simple-bt') {
    const wrap = document.getElementById('simple-bt-progress');
    if (wrap) wrap.style.display = 'none';
    const runBtn = document.getElementById('btn-simple-bt-run');
    if (runBtn) runBtn.disabled = false;
    return;
  }
  const prefix = ctx === 'backtest' ? 'bt' : ctx === 'dl' ? 'dl' : 'scan';
  const wrap = document.getElementById(`${prefix}-progress`);
  if (wrap) setTimeout(() => wrap.classList.remove('active'), 1500);
}

// ─── Strategy Factory ─────────────────────────────────────────
let currentStrategies = [];
let factoryStrategies = [];
let selectedStrategy = null;

function initMonaco() {
  if (window.monacoEditor) {
    window.monacoEditor.layout();
    return;
  }
  const LOCAL_VS_PATH = '/static/lib/monaco';
  require.config({ paths: { 'vs': LOCAL_VS_PATH }});
  require(['vs/editor/editor.main'], function() {
    window.monacoEditor = monaco.editor.create(document.getElementById('monaco-editor-box'), {
      value: '# 策略加载并准备就绪 (本地模式)...\n',
      language: 'python',
      theme: 'vs-dark',
      automaticLayout: true,
      fontSize: 14,
      minimap: { enabled: false }
    });
    if (selectedStrategy) applyStrategyToUI();
  });
}

async function loadStrategies() {
  try {
    const res = await fetch(window.location.origin + '/api/factory/strategies').then(r => r.json());
    factoryStrategies = JSON.parse(JSON.stringify(res));
    currentStrategies = JSON.parse(JSON.stringify(res));
    renderStrategyList();
  } catch (e) {
    addLog('error', `🚫 策略列表加载失败: ${e.message}`);
  }
}

function renderStrategyList() {
  const container = document.getElementById('strategy-list');
  if (!container) return;
  container.innerHTML = factoryStrategies.map(s => `
    <div class="strategy-card ${selectedStrategy && selectedStrategy.name === s.name ? 'active' : ''}" data-name="${s.name}" onclick="selectStrategy('${s.name}')">
      <div style="display:flex; flex-direction:column; gap:2px">
        <h4 style="margin:0">${s.name}</h4>
        <div style="display:flex; align-items:center; gap:5px">
           ${s.is_active ? '<span class="tag-active">● 有效</span>' : '<span class="tag-deprecated">○ 废弃</span>'}
           <span style="font-size:10px; color:rgba(255,255,255,0.25)">| Python</span>
        </div>
      </div>
      <button class="btn-item-delete" onclick="handleDeleteClick('${s.name}', event, this)" title="物理销毁策略文件">
        <span>🗑️</span><span class="del-text">确认删除?</span>
      </button>
    </div>
  `).join('');
  
  const pyOpts = factoryStrategies.filter(s => s.is_active).map(s => `<option value="${s.name}">${s.name}</option>`).join('');
  const tdxOpt = '<optgroup label=\"TDX 策略\"><option value=\"QUANTQQ\" data-strategy-type=\"tdx\">QUANTQQ</option></optgroup>';
  // AI 回测不支持 TDX，仅 Python
  document.querySelectorAll('#bt-strategy, #scan-strategy, #sim-strategy, #sim-strategy-select').forEach(el => {
    if(el) el.innerHTML = pyOpts + tdxOpt;
  });
  document.querySelectorAll('#ai-bt-strategy').forEach(el => {
    if(el) el.innerHTML = pyOpts;
  });
}

function handleDeleteClick(name, event, btn) {
  if (event) event.stopPropagation();
  
  if (!btn.classList.contains('confirming')) {
    // 第一阶段：展示确认状态
    document.querySelectorAll('.btn-item-delete').forEach(b => b.classList.remove('confirming')); 
    btn.classList.add('confirming');
    
    // 3秒后如果不点，自动恢复
    setTimeout(() => { if(btn) btn.classList.remove('confirming'); }, 3000);
    return;
  }
  
  // 第二阶段：确认为真，发起删除
  performPhysicalDelete(name, btn);
}

async function performPhysicalDelete(name, btn) {
  btn.innerHTML = '<span>⏳</span><span class="del-text">正在销毁...</span>';
  btn.disabled = true;
  
  const targetUrl = window.location.origin + '/api/factory/destroy_physical_strategy';
  addLog('info', `🚀 全力销毁指令已下达: ${name}`);
  
  try {
    const response = await fetch(targetUrl, { 
      method: 'POST', 
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name })
    });
    
    if (!response.ok) {
       const errTxt = await response.text();
       throw new Error(`网络层拒绝 (${response.status}): ${errTxt}`);
    }
    
    const res = await response.json();
    
    if (res.status === 'ok') {
      addLog('warn', `🔥 物理销毁已成功落盘: ${name}`);
      if (selectedStrategy && selectedStrategy.name === name) {
        selectedStrategy = null;
        document.getElementById('factory-empty-state').style.display = 'block';
        document.getElementById('factory-editor-container').style.display = 'none';
      }
      loadStrategies(); 
    } else {
      const errMsg = res.message || "后端接口逻辑拒绝";
      addLog('error', `❌ 销毁失败: ${errMsg}`);
      alert("销毁失败: " + errMsg);
      loadStrategies();
    }
  } catch (e) {
    addLog('error', `🚫 物理交互崩溃: ${e.message}`);
    alert("删除请求异常: " + e.message);
    loadStrategies();
  }
}

function selectStrategy(name) {
  selectedStrategy = factoryStrategies.find(s => s.name === name);
  document.querySelectorAll('.strategy-card').forEach(c => {
    c.classList.toggle('active', c.getAttribute('data-name') === name);
  });
  applyStrategyToUI();
}

function applyStrategyToUI() {
  if (!selectedStrategy) return;
  document.getElementById('factory-empty-state').style.display = 'none';
  document.getElementById('factory-editor-container').style.display = 'flex';
  document.getElementById('edit-strategy-name').value = selectedStrategy.name;
  document.getElementById('edit-strategy-file').textContent = selectedStrategy.code_path || "";
  document.getElementById('strategy-summary-view').textContent = selectedStrategy.description || "暂无描述";
  if (window.monacoEditor) window.monacoEditor.setValue(selectedStrategy.code_content || "");
}

function newStrategy() {
  selectedStrategy = null;
  document.getElementById('factory-empty-state').style.display = 'none';
  document.getElementById('factory-editor-container').style.display = 'flex';
  document.getElementById('edit-strategy-name').value = "";
  document.getElementById('edit-strategy-file').textContent = "新建策略文件 (保存后生成)";
  document.getElementById('strategy-summary-view').textContent = "新策略描述";
  if (window.monacoEditor) window.monacoEditor.setValue("# 在此编写您的 Python 策略逻辑...\nimport pandas as pd\n\ndef signal(df: pd.DataFrame) -> pd.DataFrame:\n    # 示例逻辑: 收盘价站上 20 日均线\n    df['ma20'] = df['close'].rolling(20).mean()\n    df['buy_signal'] = df['close'] > df['ma20']\n    return df[df['buy_signal']].copy()\n");
  
  // 取消列表中的所有选中状态
  document.querySelectorAll('.strategy-card').forEach(c => c.classList.remove('active'));
}

async function saveStrategy() {
  const name = document.getElementById('edit-strategy-name').value.trim();
  if (!name) { 
    addLog('warn', '⚠️ 保存拒绝: 请先填写策略标识 (文件名)');
    return; 
  }
  const code = window.monacoEditor.getValue();
  addLog('info', `💾 正在物理落盘: ${name}`);
  
  try {
    const res = await fetch(window.location.origin + '/api/factory/save_physical_strategy', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name, code_content: code })
    }).then(r => r.json());
    
    if (res.status === 'ok') {
      addLog('ok', `✅ 策略 [${name}] 物理保存成功`);
      loadStrategies();
    } else {
      addLog('error', `❌ 保存失败: ${res.message || '后端拒绝'}`);
    }
  } catch (e) {
    addLog('error', `🚫 保存连接异常: ${e.message}`);
  }
}

async function testStrategy() {
  const name = document.getElementById('edit-strategy-name').value.trim();
  const code = window.monacoEditor.getValue();
  const btn = document.getElementById('btn-test-strategy');
  
  if (!code) return;
  btn.disabled = true;
  btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 测试中...';
  
  try {
    const res = await fetch(window.location.origin + '/api/factory/test_syntax_secure', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name: name || "new_test", code_content: code })
    }).then(r => r.json());
    
    if (res.status === 'ok') {
      const sigCount = res.total_signals || res.signal_count || 0;
      addLog('ok', `✅ 语法通过！样本中发现信号: ${sigCount} 个`);
      if (typeof showStrategyStats === 'function') showStrategyStats(res);
    } else {
      const errMsg = res.message || "未知逻辑错误";
      addLog('error', `❌ 引擎报错: ${errMsg}`);
    }
  } catch (e) {
    addLog('error', `🚫 测算引擎通讯故障: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="fas fa-play"></i> 语法测试';
  }
}


async function syncStrategies() {
  addLog('info', '🔄 正在触发物理目录全量扫描...');
  try {
    const res = await fetch(window.location.origin + '/api/factory/sync_strategies', { method: 'POST' }).then(r => r.json());
    if (res.status === 'ok') {
      await loadStrategies();
      addLog('ok', '✅ 策略库同步扫描完成');
    } else {
      addLog('error', `❌ 同步失败: ${res.message}`);
    }
  } catch (e) {
    addLog('error', `🚫 同步连接异常: ${e.message}`);
  }
}

async function runScan() {
  const strategy = document.getElementById('scan-strategy').value;
  if (!strategy) return;
  toggleTaskButtons('scan', true);
  updateProgress('scan', 0, 10, '启动引擎...');
  
  const f_roe = document.getElementById('mf-min-roe').value;
  const fundamentals = {};
  if (f_roe) fundamentals.min_roe = parseFloat(f_roe);

  const body = {
    strategy_name: strategy,
    freq: document.getElementById('scan-freq').value,
    start: document.getElementById('scan-start').value || null,
    end: document.getElementById('scan-end').value || null,
    exchanges: Array.from(scanExchanges),
    sectors: scanSelectedSectors.size > 0 ? Array.from(scanSelectedSectors) : null,
    hot_only: document.getElementById('scan-hot') ? document.getElementById('scan-hot').checked : false,
    params: fundamentals,
    index_filter: getIndexFilter('scan'),
    min_mv: document.getElementById('scan-min-mv').value ? parseFloat(document.getElementById('scan-min-mv').value) : null,
    max_mv: document.getElementById('scan-max-mv').value ? parseFloat(document.getElementById('scan-max-mv').value) : null,
  };
  
  document.getElementById('scan-tbody').innerHTML = '<tr><td colspan="9" style="color:var(--text2)">扫描启动中...</td></tr>';
  
  try {
    const res = await fetch('/api/screener/scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    }).then(r => r.json());
    
    if (res.error) {
      addLog('warn', `选股失败: ${res.error}`);
      toggleTaskButtons('scan', false);
    } else {
      // 这里的 res 只是 {"status":"started"}，不含数据，所以不能 renderScanResults
      addLog('info', '扫描任务已在后台启动...');
    }
  } catch(e) {
    addLog('warn', `选股请求异常: ${e}`);
    toggleTaskButtons('scan', false);
  }
}

async function viewScanHistory(id) {
  try {
    const res = await fetch(`/api/scan/history/${id}`);
    const data = await res.json();
    if (data.results && data.results.length > 0) {
      document.getElementById('scan-result-label').textContent = `查看历史快照 (共 ${data.results.length} 只)`;
      renderScanResults(data.results);
      const container = document.querySelector('.card:nth-child(2)'); // Results card
      if(container) container.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else {
      alert("该历史快照无对应数据或已被清空");
    }
  } catch (err) {
    console.error(err);
    alert("加载选股历史详情失败");
  }
}

async function loadScanHistory() {
  try {
    const res = await fetch('/api/scan/history').then(r => r.json());
    const tbody = document.getElementById('scan-history-tbody');
    tbody.innerHTML = res.map(r => `
      <tr>
        <td>${fmtDt(r.created_at)}</td>
        <td>${r.strategy_name}</td>
        <td>${r.result_count}</td>
        <td><button class="btn btn-ghost btn-sm" onclick="viewScanHistory(${r.id})">查看</button></td>
      </tr>`).join('') || '<tr><td colspan="4" style="color:var(--text2)">无记录</td></tr>';
  } catch(e) { console.error('Load history failed', e); }
}

function renderScanResults(results) {
  const tbody = document.getElementById('scan-tbody');
  if (!tbody) return;
  if (!results || !Array.isArray(results) || results.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" style="color:var(--text2)">本日未发现符合条件的信号</td></tr>';
    return;
  }
  try {
    tbody.innerHTML = results.map(r => {
      const code = r.code || '';
      const thsUrl = `https://stockpage.10jqka.com.cn/${code.split('.')[0]}/`;
      const tradeJson = encodeURIComponent(JSON.stringify({...r, type:'signal'}));
      // 容错处理：确保 buy_date 是字符串
      let dateStr = '--';
      if (r.buy_date && typeof r.buy_date === 'string') {
        dateStr = r.buy_date.split(' ')[0];
      } else if (r.signal_date) {
        dateStr = String(r.signal_date).split(' ')[0];
      } else if (r.date) {
        dateStr = String(r.date).split(' ')[0];
      }

      const ss = r.sector_score !== undefined ? r.sector_score : '';
      const cs = r.concept_score !== undefined ? r.concept_score : '';
      const ts = r.total_score !== undefined ? r.total_score : '';
      const ssHtml = ss !== '' ? `<span style="color:${ss>=0?'var(--red)':'var(--green)'}">${ss>0?'+':''}${ss}</span>` : '--';
      const csHtml = cs !== '' ? `<span style="color:${cs>=0?'var(--red)':'var(--green)'}">${cs>0?'+':''}${cs}</span>` : '--';
      const tsHtml = ts !== '' ? `<span style="color:${ts>=0?'var(--red)':'var(--green)'};font-weight:bold">${ts>0?'+':''}${ts}</span>` : '--';

      return `<tr>
        <td><a href="${thsUrl}" target="_blank" style="color:#3b82f6;text-decoration:none">${code}</a></td>
        <td>${r.name||''}</td>
        <td style="font-weight:bold">${fmtPrice(r.entry_price || r.price || r.close)}</td>
        <td>${dateStr}</td>
        <td style="font-size:11px;color:var(--text2)">${r.sector||''}</td>
        <td style="text-align:center">${ssHtml}</td>
        <td style="text-align:center">${csHtml}</td>
        <td style="text-align:center">${tsHtml}</td>
        <td>
          <button class="btn btn-primary btn-sm" onclick="showChart('${tradeJson}')">查看图表</button>
          <button class="btn btn-ghost btn-sm" onclick="addWatchlistFromScan('${code}')">⭐ 自选</button>
        </td>
      </tr>`;
    }).join('');
  } catch(e) {
    console.error('Map results error:', e);
    tbody.innerHTML = `<tr><td colspan="9" style="color:var(--red)">渲染数据出错: ${e.message}</td></tr>`;
  }
}

async function addWatchlistFromScan(code) {
  const res = await fetch('/api/watchlist', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({code, source: 'screener'})
  }).then(r => r.json());
  if (res.status === 'ok') addLog('ok', `已加入自选: ${code}`);
}

// ─── Sentiment ────────────────────────────────────────────────


async function viewSentimentDetail(id) {
  try {
    const data = await fetch(`/api/sentiment/history/${id}`).then(r => r.json());
    renderSentimentDetail(data);
  } catch (e) {
    console.error(e);
    alert('提取详情失败');
  }
}





// ─── Positions ────────────────────────────────────────────────
async function loadPositions() {
  const data = await fetch('/api/positions').then(r => r.json()).catch(() => []);
  const tbody = document.getElementById('pos-tbody');
  document.getElementById('pos-count').textContent = data.length;
  let totalCost = 0, trailingCount = 0;
  if (!data.length) {
    tbody.innerHTML = '<tr><td colspan="11" style="color:var(--text2)">暂无持仓</td></tr>';
    return;
  }
  tbody.innerHTML = data.map(p => {
    totalCost += p.cost || 0;
    if (p.trailing_activated) trailingCount++;
    const openP = p.open_price || 0;
    const code = p.code || '';
    const thsUrl = `https://stockpage.10jqka.com.cn/${code.split('.')[0]}/`;
    return `<tr>
      <td><a href="${thsUrl}" target="_blank" style="color:#3b82f6;text-decoration:none">${code}</a></td>
      <td>${p.name||code}</td>
      <td>${fmtPrice(openP)}</td>
      <td>--</td>
      <td>--</td>
      <td>${p.volume||0}</td>
      <td>${p.remain_volume||0}</td>
      <td>${fmtPrice(p.highest_price)}</td>
      <td>${p.trailing_activated ? '<span class="tag tag-buy">已激活</span>' : '<span style="color:var(--text2)">未激活</span>'}</td>
      <td><span class="tag ${p.source==='manual'?'tag-manual':'tag-strategy'}">${p.source||'手工'}</span></td>
      <td>
        <button class="btn btn-sm" style="background:var(--purple);color:#000" onclick="showAiReport('${code}', '${p.name||code}')">AI 体检</button>
        <button class="btn btn-danger btn-sm" onclick="sellPosition(${p.id},'${code}',${openP},${p.remain_volume})">卖出</button>
      </td>
    </tr>`;
  }).join('');
  document.getElementById('pos-cost').textContent = `¥${Math.round(totalCost).toLocaleString()}`;
  document.getElementById('pos-trailing').textContent = trailingCount;
}

function calcBuyVol() {
  const price = parseFloat(document.getElementById('buy-price').value) || 0;
  if (!price) return;
  const maxBuy = 8000; // kept simple; settings loaded async
  const vol = Math.floor(maxBuy / price / 100) * 100;
  document.getElementById('buy-vol-hint').textContent = vol > 0 ? `预计 ${vol} 股 ≈ ¥${Math.round(price*vol)}` : '价格超出单笔限额';
}
var buyPriceEl = document.getElementById('buy-price');
if (buyPriceEl) buyPriceEl.addEventListener('input', calcBuyVol);

async function confirmBuy() {
  const code = document.getElementById('buy-code').value.trim();
  const price = parseFloat(document.getElementById('buy-price').value);
  if (!code || !price) { addLog('warn', '请填写代码和参考价格'); return; }
  const r = await fetch('/api/trade/buy', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({code, price, reason:'手工买入'})
  }).then(r => r.json());
  if (r.status === 'ok') {
    addLog('ok', `买入委托: ${r.code} x ${r.volume} 股 ≈¥${Math.round(r.amount)}`);
    loadPositions();
  } else {
    addLog('error', r.message);
  }
}

async function sellPosition(posId, code, openPrice, volume) {
  const price = parseFloat(prompt(`卖出 ${code}，请输入参考价格（同花顺将自动使用委托价）:`, openPrice));
  if (!price) return;
  const sellVol = parseInt(prompt(`卖出数量（最多 ${volume} 股）:`, volume));
  if (!sellVol) return;
  const reason = prompt('卖出原因:', '手工卖出') || '手工卖出';
  await fetch('/api/trade/sell', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({position_id: posId, code, price, volume: sellVol, reason})
  });
  addLog('ok', `卖出 ${code} x ${sellVol} @ ${price}`);
  loadPositions();
}

// ─── Trades ───────────────────────────────────────────────────
async function loadTrades() {
  const data = await fetch('/api/trades').then(r => r.json()).catch(() => []);
  const tbody = document.getElementById('trade-tbody');
  if (!data.length) { tbody.innerHTML = '<tr><td colspan="9" style="color:var(--text2)">暂无记录</td></tr>'; return; }
  tbody.innerHTML = data.map(t => {
    const code = t.code || '';
    const thsUrl = `https://stockpage.10jqka.com.cn/${code.split('.')[0]}/`;
    const isBuy = t.direction === 'BUY';
    return `<tr>
      <td>${fmtDt(t.trade_time)}</td>
      <td><a href="${thsUrl}" target="_blank" style="color:#3b82f6;text-decoration:none">${code}</a></td>
      <td>${t.name||''}</td>
      <td><span class="tag ${isBuy?'tag-buy':'tag-sell'}">${isBuy?'买入':'卖出'}</span></td>
      <td>${fmtPrice(t.price)}</td>
      <td>${t.volume}</td>
      <td>¥${Math.round(t.amount).toLocaleString()}</td>
      <td><span class="tag ${t.trade_type==='manual'?'tag-manual':'tag-strategy'}">${t.trade_type==='manual'?'手工':'策略'}</span></td>
      <td style="color:var(--text2);max-width:250px;overflow:hidden;text-overflow:ellipsis" title="${t.reason||''}">${t.reason||''}</td>
    </tr>`;
  }).join('');
}

// ─── showModal 通用工具 (Backtest / AI 历史详情弹窗) ─────────────────────
function showModal(title, htmlContent) {
  let modal = document.getElementById('generic-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'generic-modal';
    modal.style.cssText = 'display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.82);z-index:2000;align-items:center;justify-content:center;';
    modal.innerHTML = `<div class="card" style="width:95%;max-width:1200px;max-height:92vh;display:flex;flex-direction:column;padding:20px;position:relative;overflow:hidden">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;border-bottom:1px solid var(--border);padding-bottom:10px">
        <h3 id="generic-modal-title" style="margin:0;font-size:15px"></h3>
        <button class="btn btn-ghost btn-sm" onclick="document.getElementById('generic-modal').style.display='none'" style="min-width:70px">✕ 关闭</button>
      </div>
      <div id="generic-modal-body" style="overflow-y:auto;flex:1;font-size:13px"></div>
    </div>`;
    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => { if (e.target === modal) modal.style.display='none'; });
  }
  document.getElementById('generic-modal-title').textContent = title;
  document.getElementById('generic-modal-body').innerHTML = htmlContent;
  modal.style.display = 'flex';
}

function renderBacktestResults(summary, stocks) {
  document.getElementById('bt-summary').style.display = 'grid';
  document.getElementById('bt-result-card').style.display = 'block';
  document.getElementById('bt-total').textContent = summary.total_trades;
  const winsCount = stocks.filter(s => s.pnl_pct > 0).length;
  const lossesCount = stocks.filter(s => s.pnl_pct < 0).length;
  const flatsCount = stocks.length - winsCount - lossesCount;
  document.getElementById('bt-winrate').innerHTML = `${summary.win_rate}% <span style="font-size:12px; font-weight:normal; opacity:0.7">(${winsCount}赢 / ${lossesCount}亏 / ${flatsCount}平)</span>`;
  const avgEl = document.getElementById('bt-avgpnl');
  avgEl.textContent = summary.avg_pnl_pct + '%';
  avgEl.className = 'value ' + (summary.avg_pnl_pct >= 0 ? 'up' : 'down');
  
  document.getElementById('bt-analysis-card').style.display = 'block';
  renderMonthlyInsight(stocks, 'bt-monthly-chart');

  const tbody = document.getElementById('bt-tbody');
  if (!stocks.length) { tbody.innerHTML = '<tr><td colspan="9" style="color:var(--text2)">无数据</td></tr>'; return; }
  tbody.innerHTML = stocks.sort((a,b) => b.pnl_pct - a.pnl_pct).map((s, idx) => {
    const pnl = s.pnl_pct || 0;
    const code = s.code || '';
    const rowId = `bt-row-${idx}`;
    const detailId = `bt-detail-${idx}`;
    const thsUrl = `https://stockpage.10jqka.com.cn/${code}/`;
    const tradeJson = encodeURIComponent(JSON.stringify(s));
    
    // 生成详情表格内容
    let detailHtml = '';
    if (s.sell_events && s.sell_events.length) {
      detailHtml = `
        <div class="trade-log-wrap">
          <table class="detail-tbl">
            <thead><tr><th>动作</th><th>时间</th><th>价格</th><th>比例/仓位</th><th>原因</th></tr></thead>
            <tbody>
              ${s.sell_events.map(ev => `
                <tr>
                  <td class="${ev.type=='buy'?'up':'down'}">${ev.type=='buy'?'买入':'卖出'}</td>
                  <td>${ev.date}</td>
                  <td>${ev.price.toFixed(2)}</td>
                  <td>${(ev.ratio*100).toFixed(0)}%</td>
                  <td style="text-align:left">${ev.reason}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>`;
    }

    return `
    <tr id="${rowId}">
      <td style="cursor:pointer; color:var(--primary)" onclick="toggleBtDetail('${detailId}', this)">✚</td>
      <td><a href="${thsUrl}" target="_blank">${code}</a></td>
      <td>${s.name||''}</td>
      <td>${fmtPrice(s.entry_price)}</td>
      <td>${fmtPrice(s.exit_price)}</td>
      <td>${s.buy_date||''} ${s.buy_time||''}</td>
      <td>${s.sell_date||''} ${s.sell_time||''}</td>
      <td>${s.hold_days||0}</td>
      <td class="${pnl === 0 ? '' : (pnl > 0 ? 'up' : 'down')}">${pnl > 0 ? '+' : ''}${pnl.toFixed(2)}%</td>
      <td style="font-size:11px; opacity:0.8; max-width:150px; overflow:hidden; text-overflow:ellipsis">${s.exit_reason||''}</td>
      <td>
        <button class="btn btn-primary btn-sm" onclick="showChart('${tradeJson}')">图表</button>
      </td>
    </tr>
    <tr id="${detailId}" style="display:none; background:rgba(255,255,255,0.03)">
      <td colspan="11" style="padding:15px !important">${detailHtml}</td>
    </tr>`;
  }).join('');
}

function renderPortfolioResults(pf) {
  // 动态追加投资组合统计卡片到 bt-summary 区域
  const summaryDiv = document.getElementById('bt-summary');
  if (!summaryDiv || !pf) return;
  // 移除旧的投资组合卡片（如果存在）
  const oldPf = summaryDiv.querySelector('.pf-stats');
  if (oldPf) oldPf.remove();

  const pfCard = document.createElement('div');
  pfCard.className = 'pf-stats';
  pfCard.style.cssText = 'grid-column:1/-1;display:flex;gap:20px;flex-wrap:wrap;padding:12px 16px;background:rgba(34,197,94,0.08);border-radius:6px;border:1px solid rgba(34,197,94,0.2);margin-top:4px';
  const retClass = pf.total_return >= 0 ? 'up' : 'down';
  const retSign = pf.total_return >= 0 ? '+' : '';
  pfCard.innerHTML = `
    <div style="min-width:100px"><span style="font-size:11px;color:var(--text2)">最终资金</span><br><b style="font-size:16px">${(pf.final_value/10000).toFixed(1)}万</b></div>
    <div style="min-width:80px"><span style="font-size:11px;color:var(--text2)">总收益率</span><br><b class="${retClass}" style="font-size:16px">${retSign}${pf.total_return.toFixed(2)}%</b></div>
    <div style="min-width:80px"><span style="font-size:11px;color:var(--text2)">实际成交</span><br><b style="font-size:16px">${pf.funded_trades}笔</b></div>
    <div style="min-width:80px"><span style="font-size:11px;color:var(--text2)">资金不足跳过</span><br><b style="font-size:16px;color:var(--orange)">${pf.skipped}笔</b></div>
  `;
  summaryDiv.appendChild(pfCard);
}

function renderMonthlyInsight(trades, containerId) {
  const chartDom = document.getElementById(containerId);
  if (!chartDom) return;
  const myChart = echarts.init(chartDom);
  const months = {};
  trades.forEach(t => {
    const date = t.sell_date || t.exit_date || '';
    if (!date) return;
    const month = date.substring(0, 7);
    if (!months[month]) months[month] = { win: 0, loss: 0, pnls: [] };
    const pnl = t.pnl_pct || 0;
    if (pnl > 0) months[month].win++;
    else if (pnl < 0) months[month].loss++;
    months[month].pnls.push(pnl);
  });
  const sortedMonths = Object.keys(months).sort();
  const winData = sortedMonths.map(m => months[m].win);
  const lossData = sortedMonths.map(m => months[m].loss);
  const avgPnLData = sortedMonths.map(m => {
    const sum = months[m].pnls.reduce((a, b) => a + b, 0);
    return (sum / months[m].pnls.length).toFixed(2);
  });
  const option = {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { data: ['盈利笔数', '亏损笔数', '月均收益率'], bottom: 0, textStyle: { color: '#888', fontSize: 10 } },
    grid: { top: 30, left: 40, right: 40, bottom: 40 },
    xAxis: { type: 'category', data: sortedMonths, axisLabel: { fontSize: 10, color: '#666' } },
    yAxis: [
      { type: 'value', name: '笔数', splitLine: { lineStyle: { type: 'dashed', color: '#333' } } },
      { type: 'value', name: '收益率(%)', axisLabel: { formatter: '{value}%' }, splitLine: { show: false } }
    ],
    series: [
      { name: '盈利笔数', type: 'bar', stack: 'total', itemStyle: { color: 'rgba(239, 68, 68, 0.7)' }, data: winData },
      { name: '亏损笔数', type: 'bar', stack: 'total', itemStyle: { color: 'rgba(34, 197, 94, 0.7)' }, data: lossData },
      { name: '月均收益率', type: 'line', yAxisIndex: 1, symbol: 'circle', symbolSize: 8, itemStyle: { color: '#f59e0b' }, lineStyle: { width: 3 }, data: avgPnLData }
    ]
  };
  myChart.setOption(option);
  window.addEventListener('resize', () => myChart.resize());
}

function toggleBtDetail(did, btn) {
    const el = document.getElementById(did);
    if (el.style.display === 'none') {
        el.style.display = 'table-row';
        btn.textContent = '▬';
    } else {
        el.style.display = 'none';
        btn.textContent = '✚';
    }
}

// ─── Chart Logic ──────────────────────────────────────────────
let activeChart = null;
let activeCandlestickSeries = null;

async function showChart(tradeJson) {
  const trade = JSON.parse(decodeURIComponent(tradeJson));
  const isSignal = trade.type === 'signal';
  const code = trade.code;
  const modal = document.getElementById('chart-modal');
  
  // 1. 强制排版重绘，修复 0x0 渲染白屏 BUG
  modal.style.display = 'flex';
  await new Promise(r => setTimeout(r, 50));
  
  // Set headers dynamically based on if it's a Screener signal or a Backtest trade
  document.getElementById('chart-title').textContent = `${trade.name} (${code}) ${isSignal ? '触发信号' : '回测交易详情'} 【同花顺经典皮肤】`;
  
  if (isSignal) {
    document.getElementById('chart-pnl').innerHTML = `信号触发价: <b>${trade.entry_price}</b>`;
    document.getElementById('chart-days').textContent = `触点日期: ${trade.buy_date ? trade.buy_date.split(' ')[0] : '未知'}`;
    document.getElementById('chart-reason').textContent = '';
  } else {
    document.getElementById('chart-pnl').innerHTML = `盈亏: <b class="${trade.pnl_pct>=0?'up':'down'}">${trade.pnl_pct.toFixed(2)}%</b>`;
    document.getElementById('chart-days').textContent = `持仓: ${trade.hold_days} 天`;
    document.getElementById('chart-reason').textContent = `退出原因: ${trade.exit_reason || '正常'}`;
  }

  const container = document.getElementById('chart-container');
  container.innerHTML = ''; 
  
  try {
    const bars = await fetch(`/api/bars/${code}`).then(r => r.json());
    if (!bars || bars.length === 0) {
      addLog('error', `未找到 ${code} 的历史 K 线，请先同步数据`);
      container.innerHTML = '<div style="padding:20px;text-align:center;">暂无数据</div>';
      return;
    }

    if (window.activeChart) { window.activeChart.dispose(); }
    window.activeChart = echarts.init(container);

    if (!window.__echarts_resize_bind__) {
      new ResizeObserver(() => { if(window.activeChart) window.activeChart.resize(); }).observe(container);
      window.__echarts_resize_bind__ = true;
    }

    const categoryData = [];
    const values = [];    // [open, close, lowest, highest, pct_chg, amplitude, amount]
    const volumes = [];
    
    let preClose = null;
    bars.forEach((b, index) => {
      const rawDate = b.date || b.datetime || "";
      const t = rawDate.split(' ')[0];
      const o = parseFloat(b.open) || 0;
      const c = parseFloat(b.close) || o;
      const h = parseFloat(b.high) || Math.max(o, c);
      const l = parseFloat(b.low) || Math.min(o, c);
      const v = Math.round(parseFloat(b.volume) || 0);
      const amt = parseFloat(b.amount) || 0;
      
      let pct_chg = 0;
      let amplitude = 0;
      let change_val = 0;
      if (preClose !== null && preClose > 0) {
          change_val = c - preClose;
          pct_chg = (change_val / preClose) * 100;
          amplitude = ((h - l) / preClose) * 100;
      } else if (o > 0) {
          change_val = c - o;
          pct_chg = (change_val / o) * 100;
          amplitude = ((h - l) / o) * 100;
      }
      preClose = c;

      categoryData.push(t);
      values.push([o, c, l, h, pct_chg.toFixed(2), amplitude.toFixed(2), amt, change_val.toFixed(2), v]);
      volumes.push([index, v, c >= o ? 1 : -1]);
    });

    function calculateMA(dayCount, data) {
      let result = [];
      for (let i = 0, len = data.length; i < len; i++) {
        if (i < dayCount - 1) { result.push('-'); continue; }
        let sum = 0;
        for (let j = 0; j < dayCount; j++) sum += data[i - j][1];
        result.push((sum / dayCount).toFixed(3));
      }
      return result;
    }

    const markPointData = [];
    const signalBuyDate = trade.buy_date || trade.date || trade.signal_date;
    if (isSignal && signalBuyDate) {
        let buyDate = String(signalBuyDate).split(' ')[0];
        let idx = categoryData.indexOf(buyDate);
        if(idx === -1 && categoryData.length > 0) {
            buyDate = categoryData[categoryData.length - 1];
            idx = categoryData.length - 1;
        }
        let yPrice = trade.entry_price || trade.close || values[idx]?.[2] || 0;

        markPointData.push({
            name: '买入', xAxis: buyDate, yAxis: yPrice,
            value: `B ${trade.entry_price || trade.close || yPrice}`,
            itemStyle: { color: '#ef232a' },
            label: { formatter: 'B', color: '#fff', fontSize: 11, fontWeight: 'bold' }
        });
    }

    if (!isSignal && trade.sell_date) {
        let sellDate = trade.sell_date.split(' ')[0];
        let idx = categoryData.indexOf(sellDate);
        if(idx === -1 && categoryData.length > 0) {
            sellDate = categoryData[categoryData.length - 1];
            idx = categoryData.length - 1;
        }
        let yPrice = trade.exit_price || values[idx]?.[3] || 0;
        
        markPointData.push({
            name: '卖出', xAxis: sellDate, yAxis: yPrice,
            value: `S ${trade.exit_price || yPrice}`, 
            itemStyle: { color: '#14b143' },
            label: { formatter: 'S', color: '#fff', fontSize: 11, fontWeight: 'bold' }
        });
    }

    const upColor = '#ef232a'; const upBorderColor = '#8A0000';
    const downColor = '#14b143'; const downBorderColor = '#008F28';

    const option = {
      animation: false,
      legend: {
        data: ['日线', 'MA5', 'MA10', 'MA20', 'MA60'],
        top: 2,
        left: 'center',
        textStyle: { color: '#666' }
      },
      tooltip: { 
        trigger: 'axis', 
        axisPointer: { type: 'cross' },
        borderWidth: 1, borderColor: '#ccc', padding: 10, textStyle: { color: '#000', fontSize: 12 },
        formatter: function (params) {
          let res = '';
          let kData = null;
          let maData = [];
          for (let i = 0; i < params.length; i++) {
              if (params[i].seriesName === '日线') kData = params[i];
              else if (params[i].seriesName.startsWith('MA')) maData.push(params[i]);
          }
          if (kData) {
              const date = kData.axisValue;
              const data = kData.data;
              const o = data[1], c = data[2], l = data[3], h = data[4];
              const pct_chg = data[5], amp = data[6], amt = data[7], chg_val = data[8], vol = data[9];
              
              const color = pct_chg >= 0 ? '#ef232a' : '#14b143';
              const preClose = c - chg_val;
              
              const getPct = (val) => {
                  if(!preClose) return '--';
                  const p = ((val - preClose) / preClose * 100).toFixed(2);
                  return (p >= 0 ? '+' : '') + p + '%';
              };

              // 格式化成交额 (以亿或万为单位)
              let amtStr = amt >= 100000000 ? (amt/100000000).toFixed(2) + '亿' : (amt/10000).toFixed(0) + '万';
              let volStr = vol.toLocaleString();
              
              res += `<div style="margin-bottom:5px;font-weight:bold;">${date}</div>`;
              res += `<table style="width:100%; border-collapse:collapse; font-size:12px;">`;
              res += `<tr><td style="padding-right:10px;color:#666;">开盘</td><td style="color:${o >= preClose ? '#ef232a' : '#14b143'}">${o.toFixed(2)} <span style="font-size:10px">(${getPct(o)})</span></td></tr>`;
              res += `<tr><td style="color:#666;">最高</td><td style="color:#ef232a">${h.toFixed(2)} <span style="font-size:10px">(${getPct(h)})</span></td></tr>`;
              res += `<tr><td style="color:#666;">最低</td><td style="color:#14b143">${l.toFixed(2)} <span style="font-size:10px">(${getPct(l)})</span></td></tr>`;
              res += `<tr><td style="color:#666;">收盘</td><td style="font-weight:bold;color:${color}">${c.toFixed(2)} <span style="font-size:10px">(${getPct(c)})</span></td></tr>`;
              res += `<tr><td style="color:#666;padding-top:4px;">涨跌额</td><td style="padding-top:4px;color:${color}">${chg_val > 0 ? '+' : ''}${chg_val}</td></tr>`;
              res += `<tr><td style="color:#666;">涨跌幅</td><td style="color:${color}">${pct_chg}%</td></tr>`;
              res += `<tr><td style="color:#666;">振幅</td><td>${amp}%</td></tr>`;
              res += `<tr><td style="color:#666;">成交量</td><td style="color:#e08412">${volStr}</td></tr>`;
              res += `<tr><td style="color:#666;">成交额</td><td>${amtStr}</td></tr>`;
              res += `</table>`;
          }
          if (maData.length > 0) {
              res += `<div style="margin-top:5px; padding-top:5px; border-top:1px solid #ddd; font-size:11px;">`;
              maData.forEach(m => {
                  res += `<span style="color:${m.color}; margin-right:8px">${m.seriesName}: ${m.data}</span>`;
              });
              res += `</div>`;
          }
          return res;
        }
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [
        { left: '8%', right: '8%', height: '55%' },
        { left: '8%', right: '8%', top: '70%', height: '16%' }
      ],
      xAxis: [
        { type: 'category', data: categoryData, boundaryGap: false, axisLine: { onZero: false }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
        { type: 'category', gridIndex: 1, data: categoryData, axisLabel: { show: false } }
      ],
      yAxis: [
        { scale: true, splitArea: { show: true } },
        { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: false }, axisLine: { show: false }, splitLine: { show: false } }
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: Math.max(0, 100 - (10000 / categoryData.length)), end: 100 },
        { show: true, type: 'slider', top: '90%', xAxisIndex: [0, 1], start: Math.max(0, 100 - (10000 / categoryData.length)), end: 100 }
      ],
      series: [
        {
          name: '日线', type: 'candlestick', data: values,
          itemStyle: { color: upColor, color0: downColor, borderColor: upBorderColor, borderColor0: downBorderColor },
          markPoint: { 
            data: markPointData, 
            symbol: 'pin', 
            symbolSize: function(val, params) { return 45; }, 
            label: { show:true, color: '#fff', fontSize: 10, offset: [0, 0] } 
          }
        },
        { name: 'MA5', type: 'line', data: calculateMA(5, values), smooth: true, lineStyle: { opacity: 0.5 } },
        { name: 'MA10', type: 'line', data: calculateMA(10, values), smooth: true, lineStyle: { opacity: 0.5 } },
        { name: 'MA20', type: 'line', data: calculateMA(20, values), smooth: true, lineStyle: { opacity: 0.5 } },
        { name: 'MA60', type: 'line', data: calculateMA(60, values), smooth: true, lineStyle: { opacity: 0.5, type: 'dashed' } },
        {
          name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
          data: volumes.map(item => ({ value: item[1], itemStyle: { color: item[2] > 0 ? upColor : downColor } }))
        }
      ]
    };

    window.activeChart.setOption(option, true);
    
    // 键盘导航支持
    window.__echartsCurrentIndex = categoryData.length - 1;
    window.__echartsMaxIndex = categoryData.length - 1;
    window.activeChart.on('updateAxisPointer', function(event) {
        if (event.axesInfo && event.axesInfo.length > 0) {
            window.__echartsCurrentIndex = event.axesInfo[0].value;
        }
    });

  } catch (err) {
    console.error('Chart Draw Error (ECharts):', err);
    alert('渲染崩溃: ' + (err.message || err));
    document.getElementById('chart-container').innerHTML = `<div style="padding:20px; color:red; font-size:16px;">ECharts渲染崩溃: ${err.message || err}</div>`;
  }
}

function closeChart() {
  document.getElementById('chart-modal').style.display = 'none';
  if (window.activeChart) {
    window.activeChart.dispose();
    window.activeChart = null;
  }
}

function showToast(msg, type='info') {
  const el = document.getElementById('toast') || document.createElement('div');
  if (!el.id) { el.id = 'toast'; el.style.cssText = 'position:fixed;bottom:20px;right:20px;padding:10px 20px;border-radius:8px;z-index:9999;font-size:13px;transition:opacity .3s'; document.body.appendChild(el); }
  el.style.background = type==='error'?'var(--red)' : type==='warn'?'var(--yellow)' : 'var(--primary)';
  el.style.color = '#fff'; el.textContent = msg; el.style.opacity = '1';
  clearTimeout(el._t); el._t = setTimeout(() => { el.style.opacity = '0'; }, 3000);
}

async function loadSectors() {
  const data = await fetch('/api/market/sectors').then(r => r.json()).catch(() => []);
  const tbody = document.getElementById('sector-tbody');
  tbody.innerHTML = data.map(s => {
    const pct = s.change_pct || 0;
    return `<tr>
      <td>${s.name}</td>
      <td class="${pct>=0?'up':'down'}">${pct>0?'+':''}${(+pct).toFixed(2)}%</td>
      <td>${s.leading_stock||''}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="3" style="color:var(--text2)">无数据（请在交易时间刷新）</td></tr>';
}

// ─── Data ─────────────────────────────────────────────────────
function toggleCustomDates() {
  const mode = document.getElementById('dl-mode').value;
  document.getElementById('dl-custom-dates').style.display = mode === 'custom' ? 'flex' : 'none';
}

// ─── 市场指数同步 ─────────────────────────────────────────
async function startIndexSync() {
  const btn = document.getElementById('sync-index-btn');
  btn.disabled = true;
  btn.textContent = '⏳ 同步中...';
  try {
    const r = await fetch('/api/data/sync_index', { method: 'POST' });
    const d = await r.json();
    if (d.status === 'started') {
      showToast('📈 指数同步已启动，请查看日志...');
      // 同步完成后自动刷新状态（延迟 30s）
      setTimeout(() => checkIndexData(), 30000);
    } else {
      showToast(`⚠️ ${d.message}`, 'warning');
    }
  } catch(e) {
    showToast('❌ 请求失败: ' + e.message, 'error');
  } finally {
    setTimeout(() => { btn.disabled = false; btn.textContent = '⬇️ 一键同步指数'; }, 3000);
  }
}

async function checkIndexData() {
  const panel = document.getElementById('index-status-panel');
  const tbody = document.getElementById('index-status-tbody');
  panel.style.display = '';
  tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text2)">查询中...</td></tr>';
  try {
    const r = await fetch('/api/data/check_index');
    const d = await r.json();
    if (!d.indices) return;
    tbody.innerHTML = d.indices.map(idx => {
      const isRegime = idx.name.includes('Regime');
      const statusIcon = idx.status === 'ok' ? '✅' : idx.status === 'missing' ? '❌ 缺失' : '⚠️ 错误';
      const range = idx.status === 'ok' ? `${idx.start} ~ ${idx.end}` : '--';
      const rows  = idx.status === 'ok' ? idx.rows : '--';
      const size  = idx.status === 'ok' ? `${idx.size_kb} KB` : '--';
      return `<tr style="${isRegime ? 'background:rgba(34,197,94,0.06);font-weight:600' : ''}">
        <td>${isRegime ? '⭐ ' : ''}${idx.name}</td>
        <td>${range}</td>
        <td>${rows}</td>
        <td>${size}</td>
        <td>${statusIcon}</td>
      </tr>`;
    }).join('');
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="5" style="color:var(--red)">查询失败: ${e.message}</td></tr>`;
  }
}

async function startDownload() {
  const mode = document.getElementById('dl-mode').value;
  let url = `/api/data/download?mode=${mode}`;
  
  if (mode === 'custom') {
     const start = document.getElementById('dl-start').value.replace(/-/g, '');
     const end = document.getElementById('dl-end').value.replace(/-/g, '');
     if (!start || !end) { 
         alert("请先选择完整的开始和结束日期！"); 
         return; 
     }
     url += `&start_date=${start}&end_date=${end}`;
  }
  
  await fetch(url, {method:'POST'});
  addLog('info', `数据同步已启动 [模式: ${mode === 'full' ? '全量同步' : (mode === 'custom' ? '区间段特种覆盖' : '⚡ 极速增量')}]...`);
}

function toggleQMTMode() {
  const mode = document.getElementById('qmt-sync-mode').value;
  document.getElementById('qmt-days-wrap').style.display = mode === 'days' ? 'flex' : 'none';
  document.getElementById('qmt-custom-wrap').style.display = mode === 'custom' ? 'flex' : 'none';
}
let qmtStatusAbortController = null;

async function queryQMTStatus() {
  const btn = document.getElementById('qmt-query-btn');
  const panel = document.getElementById('qmt-status-panel');
  
  if (qmtStatusAbortController) {
      qmtStatusAbortController.abort();
      qmtStatusAbortController = null;
      btn.innerHTML = '🔍 查询本地状态';
      btn.style.borderColor = 'var(--purple)';
      btn.style.color = 'var(--purple)';
      panel.innerHTML = `<span style="color:var(--orange)">⚠️ 查询已由用户中止。</span>`;
      return;
  }

  qmtStatusAbortController = new AbortController();
  btn.innerHTML = '⏹ 停止查询';
  btn.style.borderColor = 'var(--red)';
  btn.style.color = 'var(--red)';

  const freq = document.getElementById('qmt-sync-freq').value;
  const mode = document.getElementById('qmt-sync-mode').value;
  let qArgs = `freq=${freq}`;
  
  let sDate = new Date();
  let eDate = new Date();
  
  if (mode === 'days') {
      const days = document.getElementById('qmt-sync-days').value || 30;
      qArgs += `&days=${days}`;
      sDate.setDate(eDate.getDate() - days + 1);
  } else {
      const st = document.getElementById('qmt-start').value;
      const et = document.getElementById('qmt-end').value;
      if(st) { qArgs += `&start_date=${st}`; sDate = new Date(st); }
      if(et) { qArgs += `&end_date=${et}`; eDate = new Date(et); }
  }

  panel.style.display = 'block';
  panel.innerHTML = `<i>正在扫描本地 ${freq} 存储阵列，请稍候...</i>`;
  
  try {
      const res = await fetch(`/api/data/qmt_intra_status?${qArgs}`, { 
          signal: qmtStatusAbortController.signal 
      }).then(r => r.json());
      
      if (res.error) {
          panel.innerHTML = `<span style="color:var(--red)">❌ ${res.error}</span>`;
          return;
      }
      
      let html = `<div style="display:flex; justify-content:space-between; margin-bottom:8px;">
                    <span><b>最新同步：</b><span style="color:var(--green)">${res.latest_time}</span></span>
                    <span><b>已同步资产：</b>${res.total_files} 只</span>
                  </div>`;
      
      html += `<div style="margin-top:4px;"><b>连续性检查 (基准标的)：</b></div>`;
      html += `<div style="display:grid; grid-template-columns: repeat(auto-fill, minmax(70px, 1fr)); gap:4px; margin-top:6px;">`;
      
      // Compute array of dates to render
      const dates = [];
      let curDef = new Date(eDate);
      curDef.setHours(0,0,0,0);
      let sDef = new Date(sDate);
      sDef.setHours(0,0,0,0);
      
      while(curDef >= sDef && dates.length < 365) {
          dates.push(new Date(curDef));
          curDef.setDate(curDef.getDate() - 1);
      }
      
      for (const d of dates) {
          const y = d.getFullYear();
          const m = String(d.getMonth() + 1).padStart(2, '0');
          const day = String(d.getDate()).padStart(2, '0');
          const dateStr = `${y}-${m}-${day}`;
          
          const count = res.daily_counts[dateStr] || 0;
          const isWeekend = d.getDay() === 0 || d.getDay() === 6;
          
          if (isWeekend && count === 0) continue;
          
          let dotColor = '#999';
          let statusLabel = '无数据';
          
          if (count >= res.ideal_count) {
              dotColor = 'var(--green)';
              statusLabel = '完整';
          } else if (count > 0) {
              dotColor = 'var(--orange)';
              statusLabel = `缺漏(${count})`;
          } else if (!isWeekend) {
              dotColor = 'var(--red)';
              statusLabel = '缺失';
          }

          html += `<div title="${dateStr}: ${count}条" style="background:rgba(0,0,0,0.2); padding:4px; border-radius:4px; text-align:center; opacity:${isWeekend?0.5:1}">
                     <div style="font-size:10px">${dateStr.slice(5)}</div>
                     <div style="display:flex; align-items:center; justify-content:center; gap:3px; margin-top:2px;">
                        <span style="width:6px; height:6px; border-radius:50%; background:${dotColor}"></span>
                        <span style="font-size:9px">${statusLabel}</span>
                     </div>
                   </div>`;
      }
      html += `</div>`;
      panel.innerHTML = html;
  } catch (e) {
      if (e.name === 'AbortError') {
          console.log('用户中止了本地状态查询。');
      } else {
          panel.innerHTML = `<span style="color:var(--red)">查询异常: ${e}</span>`;
      }
  } finally {
      if (qmtStatusAbortController) {
          qmtStatusAbortController = null;
          btn.innerHTML = '🔍 查询本地状态';
          btn.style.borderColor = 'var(--purple)';
          btn.style.color = 'var(--purple)';
      }
  }
}

async function startQMTSync() {
  const freq = document.getElementById('qmt-sync-freq').value;
  const mode = document.getElementById('qmt-sync-mode').value;
  const payload = { freq: freq };
  
  if (mode === 'days') {
      payload.days = parseInt(document.getElementById('qmt-sync-days').value) || 30;
  } else {
      const start = document.getElementById('qmt-start').value.replace(/-/g, '');
      const end = document.getElementById('qmt-end').value.replace(/-/g, '');
      if (!start) {
          addLog('error', '请先选择指定区间的开始日期');
          return;
      }
      payload.start_date = start;
      if (end) payload.end_date = end;
  }
  const prog = document.getElementById('qmt-dl-progress');
  const fill = document.getElementById('qmt-dl-fill');
  const msg = document.getElementById('qmt-dl-msg');
  const startBtn = document.getElementById('qmt-start-btn');
  const stopBtn = document.getElementById('qmt-stop-btn');
  
  prog.classList.add('active');
  fill.style.width = '5%';
  msg.textContent = `正在呼叫 QMT 并发指令...`;
  if (startBtn) startBtn.style.display = 'none';
  if (stopBtn) stopBtn.style.display = 'inline-flex';
  
  addLog('info', `⏳ 启动 QMT 分时同步任务: [${freq}] ${payload.start_date ? '指定区间: ' + payload.start_date : '追溯 ' + payload.days + ' 天'}...`);
  
  try {
    const res = await fetch('/api/data/sync_qmt_intra', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(r => r.json());
    
    if (res.status === 'started') {
      addLog('ok', '✅ QMT 分时同步任务已在后台启动，点击"⏹ 停止同步"可中断。');
    } else {
      addLog('error', `❌ 启动失败: ${res.message || '后端拒绝'}`);
      prog.classList.remove('active');
    }
  } catch (e) {
    addLog('error', `🚫 同步接口通讯异常: ${e.message}`);
    prog.classList.remove('active');
  }
}

async function stopQMTSync() {
  const stopBtn = document.getElementById('qmt-stop-btn');
  const startBtn = document.getElementById('qmt-start-btn');
  if (stopBtn) { stopBtn.disabled = true; stopBtn.textContent = '⏳ 发送中...'; }
  
  try {
    const res = await fetch('/api/data/stop_qmt_intra', { method: 'POST' }).then(r => r.json());
    addLog('warning', `🛑 ${res.message}`);
  } catch (e) {
    addLog('error', `停止指令发送失败: ${e.message}`);
  } finally {
    setTimeout(() => {
      if (stopBtn) { stopBtn.disabled = false; stopBtn.style.display = 'none'; stopBtn.textContent = '⏹ 停止同步'; }
      if (startBtn) startBtn.style.display = 'inline-flex';
      document.getElementById('qmt-dl-progress').classList.remove('active');
    }, 2000);
  }
}

// ─── Fundamentals ─────────────────────────────────────────────
async function loadFundamentalsPreview() {
  const q = document.getElementById('fund-search')?.value || '';
  const roe = document.getElementById('fund-filter-roe')?.value || '';
  const pe = document.getElementById('fund-filter-pe')?.value || '';
  let url = `/api/data/fundamentals_preview?q=${encodeURIComponent(q)}`;
  if(roe) url += `&roe=${roe}`;
  if(pe) url += `&pe=${pe}`;
  
  try {
    const res = await fetch(url).then(r=>r.json());
    const tbody = document.getElementById('fundamentals-tbody');
    if(!tbody) return;
    if(res.status === 'ok' && res.data.length > 0) {
      tbody.innerHTML = res.data.map(r => `
        <tr>
          <td><a href="#" onclick="showAiReport('${r.code}', '${r.name}')">${r.code}</a></td>
          <td>${r.name}</td>
          <td>${r.total_mv ? (+r.total_mv/10000).toFixed(1) : '-'}</td>
          <td>${r.pe_ttm||'-'}</td>
          <td>${r.pb||'-'}</td>
          <td style="color:${r.roe>15?'var(--red)':'inherit'}">${r.roe||'-'}</td>
          <td>${r.debt_to_assets||'-'}</td>
          <td>${r.gross_margin||'-'}</td>
          <td style="font-size:10px; color:var(--text3)">${r.updated_at ? r.updated_at.split(' ')[0] : '-'}</td>
        </tr>
      `).join('');
    } else {
      tbody.innerHTML = '<tr><td colspan="9" style="text-align:center">无数据</td></tr>';
    }
  } catch(e) { console.error('Async error:', e); }
}

async function loadStocks() {
  const search = document.getElementById('stock-search')?.value.toLowerCase() || '';
  try {
    const res = await fetch('/api/meta/stocks/search?query=' + encodeURIComponent(search)).then(r=>r.json());
    const tbody = document.getElementById('stock-tbody');
    if(!tbody) return;
    if(res.status === 'ok') {
      tbody.innerHTML = res.data.map(s => `
        <tr>
          <td>${s.code}</td>
          <td>${s.name}</td>
          <td>-</td>
          <td>${s.sector}</td>
          <td><span class="tag tag-buy">正常</span></td>
        </tr>
      `).join('');
    }
  } catch(e) { console.error('Async error:', e); }
}

// ─── Settings ─────────────────────────────────────────────────
async function loadSettings() {
  try {
    const data = await fetch('/api/settings').then(r=>r.json());
    const t = data.trading || {};
    const r = data.risk || {};
    const g = data.gateway || {};
    const a = data.auto_trade || {};
    const c = data.cron || {};
    const dd = data.data || {};

    setVal('set-auto-max', t.max_buy_amount || a.max_amount_per_stock);
    setVal('set-lot-size', t.order_lot_size);
    setVal('set-trail-act', r.trailing_stop_activate_pct);
    setVal('set-trail-dd', r.trailing_stop_drawdown_pct);
    setVal('set-stop', r.hard_stop_loss_pct);
    setVal('set-days', r.time_exit_days);
    setVal('set-days-min-pnl', r.time_exit_min_profit_pct);
    // TP tiers — 动态渲染
    renderRiskTiers(r.take_profit_tiers || [{profit_pct: 0.03, sell_ratio: 0.30}]);
    setVal('set-break-act', r.breakeven_threshold_pct);
    setVal('set-break-stop', r.breakeven_stop_pnl_pct);
    setVal('set-force-days', r.time_exit_force_days || 10);
    // 风控参数
    setVal('set-streak-halve', r.loss_streak_halve ?? 3);
    setVal('set-streak-pause', r.loss_streak_pause ?? 5);
    setVal('set-pause-days', r.pause_days ?? 3);
    setVal('set-cooldown', r.same_stock_cooldown ?? 20);
    // 首日弱势离场
    var fdProfit = r.first_day_exit_min_profit;
    var fdCk = document.getElementById('set-fd-enable');
    var fdProfitEl = document.getElementById('set-fd-profit');
    var fdDaysEl = document.getElementById('set-fd-days');
    if (fdProfit !== undefined && fdProfit > 0) {
      if (fdCk) fdCk.checked = true;
      if (fdProfitEl) { fdProfitEl.value = (fdProfit * 100).toFixed(1); fdProfitEl.disabled = false; }
    } else {
      if (fdCk) fdCk.checked = false;
      if (fdProfitEl) { fdProfitEl.value = '3.0'; fdProfitEl.disabled = true; }
    }
    if (fdDaysEl && r.first_day_exit_days !== undefined) fdDaysEl.value = r.first_day_exit_days;
    if (fdCk && !fdCk.checked && fdDaysEl) fdDaysEl.disabled = true;
    // ATR 动态止损
    const atrStop = r.use_atr_stop === true;
    setVal('set-atr-stop', atrStop, 'checked');
    setVal('set-atr-mul', r.atr_stop_multiplier || 2.5);
    if (!atrStop) { const el = document.getElementById('set-atr-mul'); if (el) el.disabled = true; }
    setVal('set-auto-enable', String(a.enabled === true));
    setVal('set-auto-delay', a.delay_seconds);
    setVal('set-qmt-path', g.qmt_path);
    setVal('set-qmt-mode', g.qmt_market_mode);
    setVal('set-auto-sync', dd.auto_sync || 'daily');
    setVal('set-cron-enable', String(c.enabled !== false));

    // 加载定时时段复选框
    const syncTimes = c.sync_times || [];
    document.querySelectorAll('.cron-check').forEach(cb => {
      cb.checked = syncTimes.includes(cb.value);
    });

    renderStages(r.staged_take_profit || []);

    // 加载搜索空间
    const opt = data.optimizer || {};
    renderSearchSpace(opt.search_space || {});

  } catch(e) { console.error('Async error:', e); }

  // 加载 API Key（脱敏显示）
  try {
    const ek = await fetch('/api/settings/env-keys').then(r=>r.json());
    const mk = ek.masked || {};
    const tk = document.getElementById('set-tushare-key');
    const dk = document.getElementById('set-deepseek-key');
    if (tk && mk.tushare_key) tk.placeholder = mk.tushare_key;
    if (dk && mk.deepseek_key) dk.placeholder = mk.deepseek_key;
  } catch(e) { console.error('Async error:', e); }
}

function saveEnvKeys() {
  const tk = document.getElementById('set-tushare-key').value.trim();
  const dk = document.getElementById('set-deepseek-key').value.trim();
  const msg = document.getElementById('env-keys-msg');
  if (!tk && !dk) { msg.textContent = '输入要更新的密钥'; return; }
  msg.textContent = '保存中...';
  fetch('/api/settings/env-keys', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tushare_key: tk, deepseek_key: dk})
  }).then(r=>r.json()).then(res => {
    if (res.status === 'ok') {
      msg.textContent = '✓ 密钥已保存，重启后完全生效';
      msg.style.color = 'var(--green)';
    } else {
      msg.textContent = '✗ ' + (res.message || '失败');
      msg.style.color = 'var(--red)';
    }
  }).catch(e => {
    msg.textContent = '✗ 保存失败';
    msg.style.color = 'var(--red)';
  });
}

function setVal(id, v) { const el = document.getElementById(id); if(el) el.value = v; }

async function loadBacktestCapitalDefaults() {
  try {
    const data = await fetch('/api/settings').then(r=>r.json());
    const bt = data.backtest || {};
    ['bt','ai'].forEach(prefix => {
      setVal(prefix + '-capital', bt.initial_capital || 1000000);
      setVal(prefix + '-position-size', bt.position_size || 50000);
      const up = document.getElementById(prefix + '-use-portfolio');
      if (up) up.checked = bt.use_portfolio !== false;
    });
  } catch(e) { console.error('Async error:', e); }
}

function showSaveMsg(el, msg, ok) {
  el.textContent = msg;
  el.style.color = ok ? 'var(--green)' : 'var(--red)';
  if (ok) setTimeout(() => el.textContent = '', 3000);
}

// ── 交易设置卡独立保存 ──


let _simpleBtChart = null;
let _simpleBtDailyTrades = {};
let _lastSimpleBtResult = null;
var _btAllTrades = [];
var _btTradePage = 0;
var _btPageSize = 100;

async function loadBtSimpleConfig() {
  try {
    // 加载策略列表
    const stratResp = await fetch('/api/sim-trader/config');
    const stratData = await stratResp.json();
    if (stratData.status === 'ok') {
      const sel = document.getElementById('sbt-strategy');
      if (sel) {
        sel.innerHTML = '';
        // Python 策略分组
        const pyGroup = document.createElement('optgroup');
        pyGroup.label = 'Python 策略';
        (stratData.strategies || []).forEach(s => {
          const opt = document.createElement('option');
          opt.value = s.name; opt.textContent = s.label || (s.file ? s.file + '.py' : s.name);
          opt.dataset.strategyType = 'python';
          pyGroup.appendChild(opt);
        });
        sel.appendChild(pyGroup);
        // TDX 策略分组
        const tdxGroup = document.createElement('optgroup');
        tdxGroup.label = 'TDX 策略';
        const tdxOpt = document.createElement('option');
        tdxOpt.value = 'QUANTQQ'; tdxOpt.textContent = 'QUANTQQ';
        tdxOpt.dataset.strategyType = 'tdx';
        tdxGroup.appendChild(tdxOpt);
        sel.appendChild(tdxGroup);
        if (stratData.current_strategy) sel.value = stratData.current_strategy;
      }
    }
  } catch(e) {}

  try {
    const resp = await fetch('/api/backtest/simple-config');
    const data = await resp.json();
    if (data.status !== 'ok') return;
    const cfg = data.config;
    // 填充普通字段
    const map = {
      'sbt-capital': 'initial_capital', 'sbt-pos-size': 'position_size',
      'sbt-min-buy': 'min_buy_amt', 'sbt-cooldown': 'same_stock_cooldown',
      'sbt-hs': 'hard_stop', 'sbt-ta': 'trail_activate', 'sbt-td': 'trail_dd',
      'sbt-ted': 'time_exit_days', 'sbt-tep': 'time_exit_profit',
      'sbt-tfd': 'time_force_days', 'sbt-lsh': 'loss_streak_halve',
      'sbt-lsp': 'loss_streak_pause', 'sbt-pd': 'pause_days',
      'sbt-fd-profit': 'first_day_exit_min_profit', 'sbt-fd-days': 'first_day_exit_days',
    };
    for (const [elId, key] of Object.entries(map)) {
      if (cfg[key] !== undefined) {
        const el = document.getElementById(elId);
        if (el) {
          if (typeof cfg[key] === 'number' && cfg[key] < 1 && cfg[key] > -1) {
            el.value = (cfg[key] * 100).toFixed(1);
          } else {
            el.value = cfg[key];
          }
        }
      }
    }
    // ATR 配置
    const atrCb = document.getElementById('sbt-atr');
    if (atrCb && cfg.use_atr_trail !== undefined) atrCb.checked = cfg.use_atr_trail;
    const atrMul = document.getElementById('sbt-atr-mul');
    if (atrMul && cfg.atr_trail_multiplier !== undefined) atrMul.value = cfg.atr_trail_multiplier;
    // 填充多档止盈
    var tiers = cfg.take_profit_tiers || [];
    var container = document.getElementById('sbt-tiers-container');
    if (container && tiers.length > 0) {
      container.innerHTML = '';
      var colors = ['#d29922','#3fb950','#58a6ff','#a371f7','#f59e0b'];
      tiers.forEach(function(t, i) {
        var c = colors[i % colors.length];
        var div = document.createElement('div');
        div.className = 'bt-cfg-grid';
        div.style.marginBottom = '4px';
        div.innerHTML =
          '<div class="bt-cfg-item"><label><span class="reason-tag" style="color:'+c+'">TP'+(i+1)+'</span> 盈利%</label><input type="number" class="sbt-tier-pct" step="0.5" value="'+(t.profit_pct*100).toFixed(1)+'"></div>'+
          '<div class="bt-cfg-item"><label><span class="reason-tag" style="color:'+c+'">TP'+(i+1)+'</span> 卖出%</label><input type="number" class="sbt-tier-ratio" step="5" value="'+(t.sell_ratio*100)+'"></div>';
        container.appendChild(div);
      });
    }
    if (cfg.start_date) document.getElementById('sbt-start').value = cfg.start_date;
    document.getElementById('sbt-end').value = new Date().toISOString().slice(0, 10);
    const stratSel = document.getElementById('sbt-strategy');
    if (stratSel && cfg.strategy_name) stratSel.value = cfg.strategy_name;
    const precSel = document.getElementById('sbt-precision');
    if (precSel && cfg.intraday_freq) precSel.value = cfg.intraday_freq;
    const sp = cfg.signal_params || {};
    document.getElementById('sbt-qs').checked = !sp.disable_quality_sort;
    addLog('ok', '已加载回测配置');
  } catch (e) {
    addLog('error', '加载回测配置失败: ' + e.message);
  }
}

function _collectBtConfig() {
  const map = {
    'sbt-capital': 'initial_capital', 'sbt-pos-size': 'position_size',
    'sbt-min-buy': 'min_buy_amt', 'sbt-cooldown': 'same_stock_cooldown',
    'sbt-hs': 'hard_stop',
    'sbt-ta': 'trail_activate', 'sbt-td': 'trail_dd',
    'sbt-ted': 'time_exit_days', 'sbt-tep': 'time_exit_profit',
    'sbt-tfd': 'time_force_days', 'sbt-lsh': 'loss_streak_halve',
    'sbt-lsp': 'loss_streak_pause', 'sbt-pd': 'pause_days',
    'sbt-fd-profit': 'first_day_exit_min_profit', 'sbt-fd-days': 'first_day_exit_days',
  };
  const cfg = {};
  for (const [elId, key] of Object.entries(map)) {
    const el = document.getElementById(elId);
    if (!el) continue;
    let val = parseFloat(el.value);
    if (isNaN(val)) continue;
    if (key === 'hard_stop' || key === 'trail_activate' || key === 'trail_dd' ||
        key === 'time_exit_profit' || key === 'first_day_exit_min_profit') {
      val = val / 100;
    }
    cfg[key] = val;
  }
  // 收集多档止盈
  cfg.take_profit_tiers = [];
  document.querySelectorAll('#sbt-tiers-container .bt-cfg-grid').forEach(row => {
    const pctEl = row.querySelector('.sbt-tier-pct');
    const ratioEl = row.querySelector('.sbt-tier-ratio');
    if (pctEl && ratioEl) {
      const pct = parseFloat(pctEl.value) / 100;
      const ratio = parseFloat(ratioEl.value) / 100;
      if (!isNaN(pct) && !isNaN(ratio) && pct > 0 && ratio > 0) {
        cfg.take_profit_tiers.push({ profit_pct: pct, sell_ratio: ratio });
      }
    }
  });
  cfg.use_atr_trail = document.getElementById('sbt-atr')?.checked || false;
  cfg.atr_trail_multiplier = parseFloat(document.getElementById('sbt-atr-mul')?.value) || 1.0;
  cfg.start_date = document.getElementById('sbt-start').value || '2023-01-01';
  cfg.end_date = document.getElementById('sbt-end').value || new Date().toISOString().slice(0, 10);
  cfg.strategy_name = document.getElementById('sbt-strategy')?.value || '盘整突破';
  var stratOpt = document.getElementById('sbt-strategy')?.selectedOptions?.[0];
  cfg.strategy_type = (stratOpt && stratOpt.dataset.strategyType) || 'python';
  cfg.intraday_freq = document.getElementById('sbt-precision')?.value || '5m';
  // signal_params 由后端从策略文件 PARAMS 自动读取，前端只传策略名
  cfg.signal_params = {};
  return cfg;
}

function addBtTier() {
  var container = document.getElementById('sbt-tiers-container');
  var idx = container.querySelectorAll('.bt-cfg-grid').length;
  var colors = ['#d29922','#3fb950','#58a6ff','#a371f7','#f59e0b'];
  var c = colors[idx % colors.length];
  var div = document.createElement('div');
  div.className = 'bt-cfg-grid';
  div.style.marginBottom = '4px';
  div.innerHTML =
    '<div class="bt-cfg-item"><label><span class="reason-tag" style="color:'+c+'">TP'+(idx+1)+'</span> 盈利%</label><input type="number" class="sbt-tier-pct" step="0.5" value="'+(4+idx*3)+'.0"></div>'+
    '<div class="bt-cfg-item"><label><span class="reason-tag" style="color:'+c+'">TP'+(idx+1)+'</span> 卖出%</label><input type="number" class="sbt-tier-ratio" step="5" value="'+(Math.min(15+idx*10,50))+'"></div>';
  container.appendChild(div);
}

function delBtTier() {
  var container = document.getElementById('sbt-tiers-container');
  var rows = container.querySelectorAll('.bt-cfg-grid');
  if (rows.length <= 1) return;
  rows[rows.length - 1].remove();
}

async function saveBtSimpleConfig() {
  const cfg = _collectBtConfig();
  try {
    await fetch('/api/backtest/simple-config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config: cfg }),
    });
    addLog('ok', '回测配置已保存');
  } catch (e) {
    addLog('error', '保存失败: ' + e.message);
  }
}

async function applyBtToSystem() {
  if (!confirm('将当前回测参数写入实盘系统配置？这会覆盖模拟盘的止盈止损等参数。')) return;
  try {
    const cfg = _collectBtConfig();
    await fetch('/api/backtest/simple-config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config: cfg }),
    });
    const resp = await fetch('/api/backtest/apply-to-system', { method: 'POST' });
    const data = await resp.json();
    if (data.status === 'ok') { addLog('ok', '已填入系统配置'); }
    else { addLog('error', data.message || '失败'); }
  } catch (e) { addLog('error', '填入系统失败: ' + e.message); }
}

async function resetBtSimpleConfig() {
  try {
    const resp = await fetch('/api/backtest/simple-config/reset', { method: 'POST' });
    const data = await resp.json();
    if (data.status === 'ok') {
      await loadBtSimpleConfig();
      addLog('ok', '已重置为系统配置');
    }
  } catch (e) {
    addLog('error', '重置失败: ' + e.message);
  }
}

async function runSimpleBacktest() {
  const cfg = _collectBtConfig();
  // 先保存
  await saveBtSimpleConfig();

  document.getElementById('simple-bt-result').style.display = 'none';
  showProgress('simple-bt', '正在启动回测...');
  document.getElementById('btn-simple-bt-run').disabled = true;

  try {
    await fetch('/api/backtest/run-simple', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ params: cfg }),
    });
  } catch (e) {
    addLog('error', '启动回测失败: ' + e.message);
    hideProgress('simple-bt');
    document.getElementById('btn-simple-bt-run').disabled = false;
  }
}

function showProgress(ctx, msg) {
  const wrap = document.getElementById(ctx === 'simple-bt' ? 'simple-bt-progress' : ctx + '-progress');
  if (!wrap) return;
  wrap.style.display = 'block';
  const msgEl = document.getElementById(ctx === 'simple-bt' ? 'simple-bt-msg' : ctx + '-msg');
  if (msgEl) msgEl.textContent = msg;
}

function updateProgressFill(ctx, step, total) {
  const fill = document.getElementById(ctx === 'simple-bt' ? 'simple-bt-fill' : ctx + '-fill');
  if (fill && total > 0) fill.style.width = (step / total * 100) + '%';
}

function renderSimpleBtResults(summary, equity, trades, indices) {
  // 显示结果区
  document.getElementById('simple-bt-result').style.display = 'block';

  // 汇总卡片
  const cards = document.getElementById('simple-bt-cards');
  cards.innerHTML = `
    <div class="stat-card"><div class="label">总收益</div><div class="value" style="color:${summary.total_return>=0 ? 'var(--up)' : 'var(--down)'}">${summary.total_return >= 0 ? '+' : ''}${summary.total_return}%</div></div>
    <div class="stat-card"><div class="label">最大回撤 / 胜率</div><div class="value">${summary.max_drawdown}% / ${summary.win_rate}%</div></div>
    <div class="stat-card"><div class="label">交易笔数 / 期末净值</div><div class="value">${summary.trades} / ${(summary.final_equity/10000).toFixed(0)}万</div></div>
    <div class="stat-card"><div class="label">均盈 / 均亏</div><div class="value">+${summary.avg_win}% / ${summary.avg_loss}%</div></div>
    <div class="stat-card"><div class="label">盈亏比 / 盈利月</div><div class="value">${summary.profit_factor} / ${summary.positive_months}</div></div>
    <div class="stat-card"><div class="label">交易日 / 信号</div><div class="value">${summary.trading_days} / ${summary.signals}</div></div>
  `;

  // 退出原因
  if (summary.exit_reasons) {
    const reasons = Object.entries(summary.exit_reasons).map(([k,v]) => `${k}:${v}`).join(' ');
    document.getElementById('simple-bt-exit-dist').textContent = '退出: ' + reasons;
  }

  // 交易记录
  document.getElementById('simple-bt-trade-count').textContent = '(' + trades.length + '笔)';
  const tbody = document.getElementById('simple-bt-tbody');
  tbody.innerHTML = trades.slice(-1000).reverse().map(t =>
    '<tr>'+
    '<td>'+t.code+'</td><td>'+(t.name||'')+'</td><td>'+(t.shares||0)+'</td>'+
    '<td>'+t.entry_date+' '+(t.entry_time||'')+'</td><td>'+t.entry_px+'</td><td>'+(t.entry_total||0).toLocaleString()+'</td>'+
    '<td>'+t.exit_date+' '+(t.exit_time||'')+'</td><td>'+t.exit_px+'</td><td>'+(t.exit_total||0).toLocaleString()+'</td>'+
    '<td style="color:'+(t.profit>=0 ? 'var(--up)' : 'var(--down)')+'">'+(t.profit>=0?'+':'')+Math.abs(t.profit).toFixed(0)+'</td>'+
    '<td style="color:'+(t.ret_pct>=0 ? 'var(--up)' : 'var(--down)')+'">'+(t.ret_pct>=0?'+':'')+t.ret_pct+'%</td>'+
    '<td>已平仓</td>'+
    '<td style="font-size:10px;color:var(--text2)">'+t.reason+'</td>'+
    '<td>'+t.hold_days+'</td>'+
    '</tr>'
  ).join('');

  // 图表
  renderSimpleBtChart(equity, indices, summary.total_return);
  renderBtVizCharts(trades);
}

function renderSimpleBtChart(equity, indices, totalReturn) {
  const dom = document.getElementById('simple-bt-chart');
  if (!dom) return;

  if (_simpleBtChart) _simpleBtChart.dispose();
  _simpleBtChart = echarts.init(dom);

  const colors = ['#f59e0b', '#ef4444', '#8b5cf6', '#22c55e', '#3b82f6'];
  const series = [];

  // 策略净值线
  const eqDates = equity.map(e => e.date);
  const eqValues = equity.map(e => e.norm);
  const eqDD = equity.map(e => e.dd);

  series.push({
    name: '策略 (' + (totalReturn >= 0 ? '+' : '') + totalReturn.toFixed(1) + '%)',
    type: 'line', data: eqValues, smooth: true,
    lineStyle: { color: colors[0], width: 2.5 },
    itemStyle: { color: colors[0] },
    symbol: 'none',
  });

  // 指数线
  let idxIdx = 1;
  if (indices) {
    for (const [name, data] of Object.entries(indices)) {
      if (!data || data.length === 0) continue;
      // 只用 strategy 的日期范围内的数据
      const idxMap = {};
      data.forEach(d => { idxMap[d.date] = d.norm; });
      const aligned = eqDates.map(d => idxMap[d] || null);
      series.push({
        name: name, type: 'line', data: aligned, smooth: true,
        lineStyle: { color: colors[idxIdx % colors.length], width: 1.5, type: 'dashed' },
        itemStyle: { color: colors[idxIdx % colors.length] },
        symbol: 'none',
      });
      idxIdx++;
    }
  }

  const option = {
    tooltip: {
      trigger: 'axis',
      formatter: function(params) {
        let html = '<b>' + params[0].axisValue + '</b><br/>';
        params.forEach(p => {
          if (p.value !== null && p.value !== undefined) {
            const ret = ((p.value - 1) * 100).toFixed(1);
            const color = ret >= 0 ? 'var(--up)' : 'var(--down)';
            html += `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${p.color};margin-right:6px"></span>`;
            html += `${p.seriesName}: <b style="color:${color}">${ret>=0?'+':''}${ret}%</b><br/>`;
          }
        });
        return html;
      }
    },
    legend: { top: 5, textStyle: { color: '#999', fontSize: 11 } },
    grid: { top: 40, right: 60, bottom: 50, left: 60 },
    xAxis: { type: 'category', data: eqDates, axisLabel: { color: '#666', fontSize: 10 },
      splitLine: { show: false } },
    yAxis: {
      type: 'value', axisLabel: {
        color: '#666', fontSize: 10,
        formatter: v => ((v - 1) * 100).toFixed(0) + '%'
      },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } }
    },
    series: series,
  };

  _simpleBtChart.setOption(option);
  window.addEventListener('resize', () => _simpleBtChart && _simpleBtChart.resize());
}

// 加载 tab 时自动加载配置
const _origSwitchTab = switchTab;
switchTab = function(name) {
  _origSwitchTab(name);
  if (name === 'backtest') {
    setTimeout(() => {
      if (document.getElementById('sbt-capital') && !document.getElementById('sbt-capital').value) {
        loadBtSimpleConfig();
      }
      // 切到回测tab后强制重绘图表
      if (_simpleBtChart) _simpleBtChart.resize();
    }, 300);
  }
};

// ══════════════════════════════════════════════════
// 重载版本（覆盖上面的简化回测渲染函数）
// ══════════════════════════════════════════════════

renderSimpleBtResults = function(summary, equity, trades, indices) {
  document.getElementById('simple-bt-result').style.display = 'block';
  const c2 = (v, unit) => (v !== undefined && v !== null) ? (typeof v === 'number' ? v.toFixed(v < 10 && v > -1 ? 2 : 0) : v) + (unit || '') : '--';
  const retColor = summary.total_return >= 0 ? 'var(--up)' : 'var(--down)';

  const cards = document.getElementById('simple-bt-cards');
  cards.innerHTML = `
    <div class="stat-card"><div class="label">回测区间</div><div class="value" style="font-size:13px">${summary.start_date} ~ ${summary.end_date}</div></div>
    <div class="stat-card"><div class="label">总收益率</div><div class="value" style="color:${retColor}">${summary.total_return>=0?'+':''}${c2(summary.total_return)}%</div></div>
    <div class="stat-card"><div class="label">最大回撤 / 胜率</div><div class="value">${c2(summary.max_drawdown)}% / ${c2(summary.win_rate)}%</div></div>
    <div class="stat-card"><div class="label">初始资金 / 最终资金</div><div class="value">${(summary.initial_capital/10000).toFixed(0)}万 / ${(summary.final_equity/10000).toFixed(0)}万</div></div>
    <div class="stat-card"><div class="label">交易天数 / 时长</div><div class="value">${summary.trading_days}天 / ${summary.total_calendar_days||'--'}天</div></div>
    <div class="stat-card"><div class="label">信号 / 买入 / 卖出 / 交易</div><div class="value">${summary.signals} / ${summary.buy_signals||'--'} / ${summary.sell_signals||'--'} / ${summary.trades}</div></div>
  `;

  // 风险指标
  const riskNode = document.getElementById('simple-bt-risk');
  if (riskNode) riskNode.remove();
  const riskDiv = document.createElement('div');
  riskDiv.id = 'simple-bt-risk';
  riskDiv.className = 'grid-3';
  riskDiv.style.marginBottom = 'var(--gap)';
  riskDiv.innerHTML = `
    <div class="stat-card"><div class="label">夏普比率 / 卡玛比率</div><div class="value">${c2(summary.sharpe)} / ${c2(summary.calmar)}</div></div>
    <div class="stat-card"><div class="label">索提诺比率 / 盈亏比率</div><div class="value">${c2(summary.sortino)} / ${c2(summary.profit_ratio)}</div></div>
    <div class="stat-card"><div class="label">利润因子 / 年化收益</div><div class="value">${c2(summary.profit_factor)} / ${c2(summary.ann_return)}%</div></div>
  `;
  cards.parentNode.insertBefore(riskDiv, cards.nextSibling);

  // 收益分析
  const anaNode = document.getElementById('simple-bt-analysis');
  if (anaNode) anaNode.remove();
  const analysisDiv = document.createElement('div');
  analysisDiv.id = 'simple-bt-analysis';
  analysisDiv.className = 'grid-3';
  analysisDiv.style.marginBottom = 'var(--gap)';
  analysisDiv.innerHTML = `
    <div class="stat-card"><div class="label">最佳交易 / 最差交易</div><div class="value"><span style="color:var(--up)">+${c2(summary.best_trade)}%</span> / <span style="color:var(--down)">${c2(summary.worst_trade)}%</span></div></div>
    <div class="stat-card"><div class="label">均盈 / 均亏</div><div class="value"><span style="color:var(--up)">+${c2(summary.avg_win)}%</span> / <span style="color:var(--down)">${c2(summary.avg_loss)}%</span></div></div>
    <div class="stat-card"><div class="label">均盈持仓 / 均亏持仓</div><div class="value">${c2(summary.avg_hold_win)}天 / ${c2(summary.avg_hold_loss)}天</div></div>
  `;
  riskDiv.parentNode.insertBefore(analysisDiv, riskDiv.nextSibling);

  if (summary.exit_reasons) {
    const reasons = Object.entries(summary.exit_reasons).map(([k,v]) => k+':'+v).join('  ');
    document.getElementById('simple-bt-exit-dist').textContent = '退出: ' + reasons;
  }

  // 存储所有交易记录用于分页
  _btAllTrades = trades.slice().reverse();
  _btTradePage = 0;
  _renderBtTradePage(0);

  renderSimpleBtChart(equity, indices, summary.total_return);
  renderBtVizCharts(trades);
};

renderSimpleBtChart = function(equity, indices, totalReturn) {
  var dom = document.getElementById('simple-bt-chart');
  if (!dom) return;
  if (_simpleBtChart) _simpleBtChart.dispose();
  _simpleBtChart = echarts.init(dom);

  var idxColors = ['#ef4444','#f97316','#eab308','#22c55e','#3b82f6','#8b5cf6','#ec4899','#06b6d4'];
  var series = [];
  var eqDates = equity.map(function(e) { return e.date; });
  var eqValues = equity.map(function(e) { return e.norm; });

  series.push({
    name: '策略 (' + (totalReturn >= 0 ? '+' : '') + totalReturn.toFixed(1) + '%)',
    type: 'line', data: eqValues, smooth: true,
    lineStyle: { color: '#f59e0b', width: 3 }, symbol: 'none',
  });

  if (indices) {
    var i = 0;
    for (var name in indices) {
      var data = indices[name];
      if (!data || data.length === 0) continue;
      var idxMap = {};
      data.forEach(function(d) { idxMap[d.date] = d.norm; });
          var alignedRaw = eqDates.map(function(d) { return idxMap[d] || null; });
          // 重新归一化：回测期间第一个有效值 = 1
          var firstValid = null;
          for (var k = 0; k < alignedRaw.length; k++) {
            if (alignedRaw[k] !== null) { firstValid = alignedRaw[k]; break; }
          }
          var aligned = alignedRaw.map(function(v) { return v !== null && firstValid ? v / firstValid : null; });
          var idxFinal = aligned.filter(function(v) { return v !== null; });
          var idxRet = idxFinal.length > 0 ? ((idxFinal[idxFinal.length-1] - 1) * 100).toFixed(1) : "N/A";
      series.push({
        name: name + ' (' + (idxRet>=0?'+':'') + idxRet + '%)',
        type: 'line', data: aligned, smooth: true,
        lineStyle: { color: idxColors[i % idxColors.length], width: 1.2, type: 'dashed' },
        symbol: 'none',
      });
      i++;
    }
  }

  _simpleBtChart.setOption({
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(20,22,28,0.95)',
      borderColor: '#333',
      textStyle: { color: '#ddd', fontSize: 12 },
      formatter: function(params) {
        var html = '<b style="font-size:13px">' + params[0].axisValue + '</b><br/>';
        params.forEach(function(p) {
          if (p.value !== null && p.value !== undefined) {
            var ret = ((p.value - 1) * 100).toFixed(2);
            var color = ret >= 0 ? 'var(--up)' : 'var(--down)';
            html += '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + p.color + ';margin-right:5px"></span>';
            html += p.seriesName.replace(/\(.*\)$/, '') + ': <b style="color:' + color + '">' + (ret>=0?'+':'') + ret + '%</b><br/>';
          }
        });
        var dateStr = params[0].axisValue;
        if (typeof _simpleBtDailyTrades !== 'undefined' && _simpleBtDailyTrades[dateStr]) {
          var day = _simpleBtDailyTrades[dateStr];
          html += '<hr style="margin:3px 0;border-color:#333"/>';
          html += '<div style="max-height:260px;overflow-y:auto;font-size:10px;line-height:1.55">';
          if (day.bought && day.bought.length > 0) {
            html += '<div style="color:var(--up);font-weight:600;margin-bottom:2px">买入 ' + day.bought.length + ' 笔</div>';
            day.bought.forEach(function(b) {
              html += '<div style="padding-left:2px;color:#aaa">' + b.code + (b.name ? ' <span style="color:#ccc">' + b.name + '</span>' : '') + ' <span style="color:#ddd">@' + b.price + '</span></div>';
            });
          }
          if (day.sold && day.sold.length > 0) {
            html += '<div style="color:var(--down);font-weight:600;margin-top:4px;margin-bottom:2px">卖出 ' + day.sold.length + ' 笔</div>';
            day.sold.forEach(function(s) {
              var sc = s.ret >= 0 ? 'var(--up)' : 'var(--down)';
              html += '<div style="padding-left:2px">';
              html += '<span style="color:#aaa">' + s.code + '</span>';
              if (s.name) html += ' <span style="color:#ccc">' + s.name + '</span>';
              html += ' <span style="color:#ddd">@' + s.price + '</span>';
              html += ' <span style="color:' + sc + ';font-weight:600">' + (s.ret >= 0 ? '+' : '') + s.ret + '%</span>';
              html += ' <span style="color:#888;font-size:9px">' + s.reason + '</span>';
              html += '</div>';
            });
          }
          html += '</div>';
        }
        return html;
      }
    },
    legend: { top: 5, textStyle: { color: '#999', fontSize: 10 }, type: 'scroll' },
    grid: { top: 55, right: 70, bottom: 55, left: 65 },
    xAxis: { type: 'category', data: eqDates, axisLabel: { color: '#666', fontSize: 9, rotate: 30, interval: Math.floor(eqDates.length / 8) }, splitLine: { show: false } },
    yAxis: { type: 'value', axisLabel: { color: '#666', fontSize: 10, formatter: function(v) { return ((v-1)*100).toFixed(0)+'%'; } }, splitLine: { lineStyle: { color: 'rgba(255,255,255,0.05)' } } },
    series: series,
  });
  window.addEventListener('resize', function() { if (_simpleBtChart) _simpleBtChart.resize(); });
}

// ─── 4合1可视化图表 ─────────────────────────

function renderBtVizCharts(trades) {
  if (!trades || !trades.length) return;
  var panel = document.getElementById('bt-viz-panel');
  panel.style.display = 'grid';
  renderBtMonthlyReturn(trades);
  renderBtPnlDist(trades);
  renderBtExitReason(trades);
  renderBtHoldDays(trades);
  // 强制 resize，确保隐藏状态转为可见后尺寸正确
  setTimeout(function() {
    if (window._btChartMR) window._btChartMR.resize();
    if (window._btChartPD) window._btChartPD.resize();
    if (window._btChartER) window._btChartER.resize();
    if (window._btChartHD) window._btChartHD.resize();
  }, 100);
}

// 1. 月度收益柱状图
function renderBtMonthlyReturn(trades) {
  var dom = document.getElementById('bt-chart-monthly-return');
  if (!dom || !trades.length) return;
  if (window._btChartMR) window._btChartMR.dispose();
  window._btChartMR = echarts.init(dom);

  // 月度组合收益率 = 当月总利润 / 初始资金 × 100
  var capital = (window._lastSimpleBtResult && window._lastSimpleBtResult.summary && window._lastSimpleBtResult.summary.initial_capital) || 1000000;
  var monthly = {};
  trades.forEach(function(t) {
    if (!t.exit_date) return;
    var m = t.exit_date.substring(0, 7);
    if (!monthly[m]) monthly[m] = { profit: 0, count: 0 };
    monthly[m].profit += (t.profit || 0);
    monthly[m].count++;
  });
  var months = Object.keys(monthly).sort();
  var values = months.map(function(m) { return parseFloat((monthly[m].profit / capital * 100).toFixed(2)); });
  var ymax = Math.max.apply(null, values.concat([3]));

  window._btChartMR.setOption({
    tooltip: { trigger: 'axis', formatter: function(p) { return '<b>' + p[0].name + '</b><br/>收益: ' + (p[0].value >= 0 ? '+' : '') + p[0].value.toFixed(2) + '%<br/>' + monthly[months[p[0].dataIndex]].count + '笔交易'; } },
    grid: { left: 56, right: 16, top: 16, bottom: 40 },
    xAxis: { type: 'category', data: months, axisLabel: { fontSize: 10, color: '#888' }, axisLine: { lineStyle: { color: '#333' } }, axisTick: { show: false } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10, color: '#888', formatter: '{value}%' }, splitLine: { show: false }, axisLine: { show: false }, axisTick: { show: false } },
    series: [{ type: 'bar', data: values.map(function(v) { return { value: v, itemStyle: { color: v >= 0 ? '#f85149' : '#3fb950' } }; }), barWidth: '50%' }]
  });
  window.addEventListener('resize', function() { if (window._btChartMR) window._btChartMR.resize(); });
}

// 2. 盈亏分布柱状图
function renderBtPnlDist(trades) {
  var dom = document.getElementById('bt-chart-pnl-dist');
  if (!dom || !trades.length) return;
  if (window._btChartPD) window._btChartPD.dispose();
  window._btChartPD = echarts.init(dom);

  var bins = ['<-10%', '-10%~-5%', '-5%~0%', '0%~5%', '5%~10%', '10%~20%', '>20%'];
  var counts = [0, 0, 0, 0, 0, 0, 0];
  trades.forEach(function(t) {
    var p = t.ret_pct || t.pnl_pct || 0;
    if (p < -10) counts[0]++;
    else if (p < -5) counts[1]++;
    else if (p < 0) counts[2]++;
    else if (p < 5) counts[3]++;
    else if (p < 10) counts[4]++;
    else if (p < 20) counts[5]++;
    else counts[6]++;
  });

  window._btChartPD.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 50, right: 16, top: 16, bottom: 50 },
    xAxis: { type: 'category', data: bins, axisLabel: { fontSize: 9, color: '#888', rotate: 30 }, axisLine: { lineStyle: { color: '#333' } }, axisTick: { show: false } },
    yAxis: { type: 'value', interval: 50, axisLabel: { fontSize: 10, color: '#888' }, splitLine: { show: false }, axisLine: { show: false }, axisTick: { show: false } },
    series: [{ type: 'bar', data: counts.map(function(c, i) { return { value: c, itemStyle: { color: i < 3 ? '#3fb950' : '#f85149' } }; }), barWidth: '60%' }]
  });
  window.addEventListener('resize', function() { if (window._btChartPD) window._btChartPD.resize(); });
}

// 3. 卖出原因环形饼图
function renderBtExitReason(trades) {
  var dom = document.getElementById('bt-chart-exit-reason');
  if (!dom || !trades.length) return;
  if (window._btChartER) window._btChartER.dispose();
  window._btChartER = echarts.init(dom);

  var reasonMap = {};
  var reasonLabels = {
    'TP1': '阶梯止盈', 'TP2': '阶梯止盈(2档)',
    'TR': '移动止盈', 'TC': '时间止盈',
    'HS': '硬止损', 'TF': '强制清仓', 'FE': '期末清仓'
  };
  trades.forEach(function(t) {
    var r = t.reason || t.exit_reason || '其他';
    var label = reasonLabels[r] || r;
    reasonMap[label] = (reasonMap[label] || 0) + 1;
  });
  var total = trades.length;
  var data = Object.entries(reasonMap).map(function(e) { return { name: e[0], value: e[1] }; });
  var colors = { '阶梯止盈': '#ef4444', '阶梯止盈(2档)': '#dc2626', '移动止盈': '#f97316', '时间止盈': '#22c55e', '硬止损': '#eab308', '强制清仓': '#8b5cf6', '期末清仓': '#3b82f6' };

  window._btChartER.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c}笔 ({d}%)' },
    legend: { bottom: 0, textStyle: { fontSize: 10, color: '#aaa' }, data: data.map(function(d) { return d.name; }) },
    series: [{
      type: 'pie', radius: ['45%', '72%'], center: ['50%', '45%'], avoidLabelOverlap: false,
      label: { show: true, formatter: '{d}%', fontSize: 9, color: '#ccc' },
      itemStyle: { borderColor: '#1a1a2e', borderWidth: 2 },
      data: data.map(function(d) { return { name: d.name, value: d.value, itemStyle: { color: colors[d.name] || '#888' } }; })
    }]
  });
  window.addEventListener('resize', function() { if (window._btChartER) window._btChartER.resize(); });
}

// 4. 持仓天数柱状图
function renderBtHoldDays(trades) {
  var dom = document.getElementById('bt-chart-hold-days');
  if (!dom || !trades.length) return;
  if (window._btChartHD) window._btChartHD.dispose();
  window._btChartHD = echarts.init(dom);

  var bins = ['1天', '2-3天', '4-7天', '8-14天', '15-20天', '20天+'];
  var counts = [0, 0, 0, 0, 0, 0];
  trades.forEach(function(t) {
    var d = t.hold_days || 0;
    if (d <= 1) counts[0]++;
    else if (d <= 3) counts[1]++;
    else if (d <= 7) counts[2]++;
    else if (d <= 14) counts[3]++;
    else if (d <= 20) counts[4]++;
    else counts[5]++;
  });

  window._btChartHD.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 50, right: 16, top: 16, bottom: 50 },
    xAxis: { type: 'category', data: bins, axisLabel: { fontSize: 10, color: '#888', rotate: 30 }, axisLine: { lineStyle: { color: '#333' } }, axisTick: { show: false } },
    yAxis: { type: 'value', interval: 30, axisLabel: { fontSize: 10, color: '#888' }, splitLine: { show: false }, axisLine: { show: false }, axisTick: { show: false } },
    series: [{ type: 'bar', data: counts, itemStyle: { color: '#3b82f6' }, barWidth: '55%' }]
  });
  window.addEventListener('resize', function() { if (window._btChartHD) window._btChartHD.resize(); });
}

// ─── 交易记录分页 + 下载 ──────────────────
var _btAllTrades = [];
var _btTradePage = 0;
var _btPageSize = 100;

function _renderBtTradePage(page) {
  var total = _btAllTrades.length;
  var totalPages = Math.ceil(total / _btPageSize);
  if (page < 0) page = 0;
  if (page >= totalPages && totalPages > 0) page = totalPages - 1;
  _btTradePage = page;

  document.getElementById('simple-bt-trade-count').textContent =
    '(' + total + '笔, ' + (page + 1) + '/' + (totalPages || 1) + '页)';

  var start = page * _btPageSize;
  var tbody = document.getElementById('simple-bt-tbody');
  tbody.innerHTML = _btAllTrades.slice(start, start + _btPageSize).map(function(t) {
    return '<tr>'+
      '<td>'+t.code+'</td><td>'+(t.name||'')+'</td><td>'+(t.shares||0)+'</td>'+
      '<td>'+t.entry_date+' '+(t.entry_time||'')+'</td><td>'+t.entry_px+'</td><td>'+(t.entry_total||0).toLocaleString()+'</td>'+
      '<td>'+t.exit_date+' '+(t.exit_time||'')+'</td><td>'+t.exit_px+'</td><td>'+(t.exit_total||0).toLocaleString()+'</td>'+
      '<td style="color:'+(t.profit>=0 ? 'var(--up)' : 'var(--down)')+'">'+(t.profit>=0?'+':'')+Math.abs(t.profit).toFixed(0)+'</td>'+
      '<td style="color:'+(t.ret_pct>=0 ? 'var(--up)' : 'var(--down)')+'">'+(t.ret_pct>=0?'+':'')+t.ret_pct+'%</td>'+
      '<td>已平仓</td>'+
      '<td style="font-size:10px;color:var(--text2)">'+t.reason+'</td>'+
      '<td>'+t.hold_days+'</td>'+
      '</tr>';
  }).join('');

  // 更新分页控件
  var pag = document.getElementById('simple-bt-pagination');
  if (pag) {
    pag.innerHTML = (totalPages > 1)
      ? '<button class="btn btn-sm" onclick="_renderBtTradePage(0)" '+(page===0?'disabled':'')+'>&#171; 首页</button> '+
        '<button class="btn btn-sm" onclick="_renderBtTradePage('+(page-1)+')" '+(page===0?'disabled':'')+'>&#8249; 上页</button> '+
        '<span style="font-size:11px;color:var(--text2);margin:0 8px">'+(page+1)+'/'+totalPages+'</span> '+
        '<button class="btn btn-sm" onclick="_renderBtTradePage('+(page+1)+')" '+(page>=totalPages-1?'disabled':'')+'>下页 &#8250;</button> '+
        '<button class="btn btn-sm" onclick="_renderBtTradePage('+(totalPages-1)+')" '+(page>=totalPages-1?'disabled':'')+'>末页 &#187;</button>'
      : '';
  }
}

function downloadBtTradesCSV() {
  if (_btAllTrades.length === 0) return;
  var header = '代码,名称,数量,买入日,买入时间,买入价,买入总额,卖出日,卖出时间,卖出价,卖出总额,盈亏额,收益率,卖出逻辑,持仓天';
  var rows = _btAllTrades.map(function(t) {
    return [
      t.code, t.name||'', t.shares||0,
      t.entry_date, t.entry_time||'', t.entry_px, t.entry_total||0,
      t.exit_date, t.exit_time||'', t.exit_px, t.exit_total||0,
      t.profit, t.ret_pct, t.reason, t.hold_days
    ].join(',');
  });
  var csv = '﻿' + header + '\n' + rows.join('\n');
  var blob = new Blob([csv], {type:'text/csv;charset=utf-8'});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url; a.download = 'backtest_trades.csv';
  a.click();
  URL.revokeObjectURL(url);
}

// ============================================
// 回测历史记录
// ============================================


async function loadSimpleBtHistory() {
  try {
    const resp = await fetch('/api/backtest/simple/history');
    const data = await resp.json();
    if (data.status !== 'ok') return;
    const items = data.data || [];
    const list = document.getElementById('simple-bt-history-list');
    if (!list) return;
    if (!items || items.length === 0) {
      list.innerHTML = '<div style="color:var(--text2);font-size:12px;padding:8px">暂无回测记录</div>';
      return;
    }
    list.innerHTML = items.map(h => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 8px;border-bottom:1px solid var(--border);font-size:12px">
        <span style="color:var(--text)">${h.start_date || h.id}</span>
        <span style="color:var(--text2)">${h.start_date} ~ ${h.end_date}</span>
        <span style="color:${h.total_return>=0 ? 'var(--up)' : 'var(--down)'}">${h.total_return>=0?'+':''}${h.total_return}%</span>
        <span style="color:var(--text2)">DD ${h.max_drawdown}% | ${h.trades}笔</span>
        <div style="display:flex;gap:4px">
          <button class="btn btn-ghost btn-sm" onclick="loadSimpleBtHistoryDetail('${h.id}')" style="font-size:11px">加载</button>
          <button class="btn btn-ghost btn-sm" onclick="deleteSimpleBtHistory('${h.id}')" style="font-size:11px;color:var(--red)">删除</button>
        </div>
      </div>
    `).join('');
  } catch (e) {
    console.error('Load history error:', e);
  }
}

async function loadSimpleBtHistoryDetail(id) {
  try {
    const resp = await fetch('/api/backtest/simple/history/' + id);
    const data = await resp.json();
    if (data.status !== 'ok') { addLog('error', '加载失败'); return; }
    var r = data.data;
    _lastSimpleBtResult = { summary: r.summary, equity: r.equity, trades: r.trades, indices: r.indices, dailyTrades: r.daily_trades, resultId: id };
    _simpleBtDailyTrades = r.daily_trades || {};
    renderSimpleBtResults(r.summary, r.equity, r.trades, r.indices);
    addLog('ok', '已加载: ' + id);
  } catch (e) {
    addLog('error', '加载失败: ' + e.message);
  }
}

async function deleteSimpleBtHistory(id) {
  if (!confirm('确认删除该回测记录？')) return;
  try {
    const resp = await fetch('/api/backtest/simple/history/' + id, { method: 'DELETE' });
    const data = await resp.json();
    if (data.status === 'ok') {
      addLog('ok', '已删除');
      loadSimpleBtHistory();
    }
  } catch (e) {
    addLog('error', '删除失败: ' + e.message);
  }
}

// ═══════════════ 通达信选股 (TQSDK) ═══════════════

let _tqsdkCurrentResults = [];
let _tqsdkCurrentResultId = null;

function initTqsdkTab() {
  const today = new Date();
  const y = today.getFullYear();
  const m = String(today.getMonth() + 1).padStart(2, '0');
  const d = String(today.getDate()).padStart(2, '0');
  const todayStr = y + '-' + m + '-' + d;
  const endEl = document.getElementById('tqsdk-end');
  if (endEl && !endEl.value) endEl.value = todayStr;
  const startEl = document.getElementById('tqsdk-start');
  if (startEl && !startEl.value) {
    const prev = new Date(today);
    prev.setDate(prev.getDate() - 30);
    startEl.value = prev.getFullYear() + '-' +
      String(prev.getMonth() + 1).padStart(2, '0') + '-' +
      String(prev.getDate()).padStart(2, '0');
  }
  loadTqsdkHistory();
}

function toggleTqsdkButtons(running) {
  const runBtn = document.getElementById('tqsdk-run-btn');
  const stopBtn = document.getElementById('tqsdk-stop-btn');
  if (runBtn) runBtn.style.display = running ? 'none' : '';
  if (stopBtn) stopBtn.style.display = running ? '' : 'none';
}

async function runTqsdkScreen() {
  const endEl = document.getElementById('tqsdk-end');
  const startEl = document.getElementById('tqsdk-start');
  if (!endEl || !endEl.value) { alert('请选择结束日期'); return; }

  toggleTqsdkButtons(true);
  document.getElementById('tqsdk-results-body').innerHTML =
    '<tr><td colspan="4" style="color:var(--accent);text-align:center;">正在选股中...</td></tr>';

  try {
    const resp = await fetch('/api/tqsdk/screen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_date: (startEl && startEl.value) ? startEl.value.replace(/-/g, '') : '',
        end_date: endEl.value.replace(/-/g, ''),
      }),
    });
    const data = await resp.json();
    if (data.status !== 'started') {
      addLog('error', '选股启动失败: ' + (data.message || ''));
      toggleTqsdkButtons(false);
    }
  } catch (e) {
    addLog('error', '请求失败: ' + e.message);
    toggleTqsdkButtons(false);
  }
}

async function stopTqsdkScreen() {
  try {
    await fetch('/api/tqsdk/screen/stop', { method: 'POST' });
  } catch (e) {}
}

function renderTqsdkResults(results) {
  const tbody = document.getElementById('tqsdk-results-body');
  const countEl = document.getElementById('tqsdk-result-count');
  const btAllBtn = document.getElementById('tqsdk-bt-all');

  if (!results || results.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" style="color:var(--text2);text-align:center;">无符合条件的股票</td></tr>';
    if (countEl) countEl.textContent = '(0)';
    if (btAllBtn) btAllBtn.style.display = 'none';
    return;
  }
  if (countEl) countEl.textContent = '(' + results.length + '只)';
  if (btAllBtn) btAllBtn.style.display = '';

  let html = '';
  results.forEach(function(r) {
    html += '<tr>' +
      '<td>' + (r.code || '') + '</td>' +
      '<td>' + (r.name || '') + '</td>' +
      '<td>' + (r.sector || '') + '</td>' +
      '<td><button class="btn btn-success btn-sm" onclick="oneClickBacktestSingle(\'' + (r.code || '') + '\')">一键回测</button></td>' +
      '</tr>';
  });
  tbody.innerHTML = html;
}

async function loadTqsdkHistory() {
  const tbody = document.getElementById('tqsdk-history-body');
  try {
    const resp = await fetch('/api/tqsdk/screen/history?limit=30');
    const data = await resp.json();
    if (data.status !== 'ok' || !data.data || data.data.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" style="color:var(--text2);text-align:center;">暂无记录</td></tr>';
      return;
    }
    let html = '';
    data.data.forEach(function(r) {
      var ts = r.executed_at || '';
      if (ts && ts.length > 16) ts = ts.substring(0, 16);
      html += '<tr>' +
        '<td>' + ts + '</td>' +
        '<td>' + (r.end_date || r.start_date || '') + '</td>' +
        '<td>' + r.stock_count + '只</td>' +
        '<td>' +
          '<button class="btn btn-ghost btn-sm" onclick="viewTqsdkHistory(' + r.id + ')">查看</button> ' +
          '<button class="btn btn-success btn-sm" onclick="oneClickBacktestHistory(' + r.id + ')">回测</button> ' +
          '<button class="btn btn-danger btn-sm" onclick="deleteTqsdkHistory(' + r.id + ')">删除</button>' +
        '</td>' +
        '</tr>';
    });
    tbody.innerHTML = html;
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="4" style="color:var(--red);">加载失败</td></tr>';
  }
}

async function viewTqsdkHistory(id) {
  try {
    const resp = await fetch('/api/tqsdk/screen/history/' + id);
    const data = await resp.json();
    if (data.status === 'ok' && data.data) {
      _tqsdkCurrentResults = data.data.stock_details || [];
      _tqsdkCurrentResultId = id;
      renderTqsdkResults(_tqsdkCurrentResults);
    }
  } catch (e) {
    addLog('error', '加载详情失败: ' + e.message);
  }
}

async function deleteTqsdkHistory(id) {
  if (!confirm('确认删除该选股记录？')) return;
  try {
    const resp = await fetch('/api/tqsdk/screen/history/' + id, { method: 'DELETE' });
    const data = await resp.json();
    if (data.status === 'ok') {
      addLog('ok', '已删除');
      loadTqsdkHistory();
    }
  } catch (e) {
    addLog('error', '删除失败: ' + e.message);
  }
}

function oneClickBacktestSingle(code) {
  _oneClickBacktestWithCodes([code]);
}

function oneClickBacktestAll() {
  if (_tqsdkCurrentResults.length === 0) { alert('没有可回测的股票'); return; }
  var codes = _tqsdkCurrentResults.map(function(r) { return r.code; });
  _oneClickBacktestWithCodes(codes);
}

function oneClickBacktestHistory(id) {
  fetch('/api/tqsdk/screen/history/' + id)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'ok' && data.data) {
        var codes = data.data.stock_codes || [];
        if (codes.length === 0) { alert('该记录无股票，无法回测'); return; }
        _oneClickBacktestWithCodes(codes);
      }
    })
    .catch(function(e) { addLog('error', '加载详情失败: ' + e.message); });
}

function _oneClickBacktestWithCodes(codes) {
  if (!codes || codes.length === 0) { alert('股票列表为空'); return; }

  // 切换到回测 tab，自动选中 QUANTQQ
  switchTab('backtest');

  setTimeout(function() {
    var stratSel = document.getElementById('sbt-strategy');
    if (stratSel) stratSel.value = 'QUANTQQ';

    var startEl = document.getElementById('tqsdk-start');
    var endEl = document.getElementById('tqsdk-end');
    if (startEl) document.getElementById('sbt-start').value = startEl.value || '2023-01-01';
    if (endEl) document.getElementById('sbt-end').value = endEl.value || '';

    addLog('ok', '已切换到回测 tab，策略=QUANTQQ (' + codes.length + '只候选)');
  }, 300);
}