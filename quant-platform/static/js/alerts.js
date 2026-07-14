/**
 * 告警中心前端(v6.0 Phase 4)
 * 功能:系统状态卡片 + 告警历史列表 + 测试通知 + 过滤 chips + 15s 轮询
 */
(function () {
  if (window._alertsInit) return;
  window._alertsInit = true;

  // ── 状态 ───────────────────────────────────────────────────────────
  let _alertsTimer = null;
  let _currentLevel = '';
  let _cooldown = false;

  // ── 轮询启停 ───────────────────────────────────────────────────────
  function startPolling() {
    stopPolling();
    loadStatus();
    loadList(_currentLevel);
    _alertsTimer = setInterval(function () {
      loadStatus();
      loadList(_currentLevel);
    }, 15000);
  }

  function stopPolling() {
    if (_alertsTimer) {
      clearInterval(_alertsTimer);
      _alertsTimer = null;
    }
  }

  // ── 外部调用:switchTab('alerts') 时 ──────────────────────────────
  window.startAlertsPolling = startPolling;
  window.stopAlertsPolling = stopPolling;

  // ── 系统状态卡片 ──────────────────────────────────────────────────
  async function loadStatus() {
    try {
      var r = await fetch('/live/status');
      if (!r.ok) return;
      var d = await r.json();
      var qmtOk = !!d.qmt_connected;
      setBadge('alerts-qmt', qmtOk, qmtOk ? '已连接' : '未连接');
      setBadge('alerts-mode', true, d.mode || '—');
      var ks = d.killswitch;
      var ksOk = !ks || !ks.activated;
      setBadge('alerts-ks', ksOk, ksOk ? '正常' : '激活');
      var card = document.getElementById('alerts-status-card');
      if (card) card.classList.toggle('alerts-card--danger', !ksOk);
    } catch (e) {
      // ignore
    }
    // 自动选股状态(从 switches 端点取)
    try {
      var sw = await fetch('/live/config/switches');
      if (sw.ok) {
        var sd = await sw.json();
        var abOk = !!sd.auto_buy_enabled;
        setBadge('alerts-auto-buy', abOk, abOk ? '已开启' : '已关闭');
      }
    } catch (e) {
      // ignore
    }
    // 总通知数
    try {
      var s = await fetch('/live/notifications/summary');
      if (s.ok) {
        var sd = await s.json();
        var total = (sd.INFO || 0) + (sd.WARN || 0) + (sd.CRITICAL || 0);
        var el = document.getElementById('alerts-total');
        if (el) el.textContent = total;
      }
    } catch (e) {
      // ignore
    }
  }

  function setBadge(id, ok, text) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = text || '—';
    el.className = 'lt-badge' + (ok ? ' lt-badge--ok' : ' lt-badge--danger');
  }

  // ── 告警历史列表 ──────────────────────────────────────────────────
  async function loadList(level) {
    var url = '/live/notifications?limit=50';
    if (level) url += '&level=' + encodeURIComponent(level);
    try {
      var r = await fetch(url);
      if (r.status === 404) {
        // API 尚未上线,降级显示
        renderEmpty('通知功能加载中...');
        return;
      }
      if (!r.ok) return;
      var data = await r.json();
      renderList(data);
    } catch (e) {
      renderEmpty('加载失败');
    }
  }

  function renderList(rows) {
    var tbody = document.getElementById('alerts-list-tbody');
    if (!tbody) return;
    if (!rows || rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text2)">暂无记录</td></tr>';
      return;
    }
    var html = '';
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var lv = escHtml(r.level || 'INFO');
      var lvCls = lv === 'CRITICAL' ? 'lt-badge--danger' : lv === 'WARN' ? 'lt-badge--warn' : 'lt-badge--ok';
      var ts = r.ts ? fmtTime(r.ts) : '—';
      var src = escHtml(r.source || '—');
      var title = escHtml(r.title || '—');
      var content = escHtml((r.content || '').substring(0, 80));
      html += '<tr>' +
        '<td>' + ts + '</td>' +
        '<td><span class="lt-badge ' + lvCls + '">' + lv + '</span></td>' +
        '<td>' + title + '</td>' +
        '<td style="color:var(--text2)">' + src + '</td>' +
        '<td style="color:var(--text2);font-size:12px">' + content + '</td>' +
        '</tr>';
    }
    tbody.innerHTML = html;
  }

  function renderEmpty(msg) {
    var tbody = document.getElementById('alerts-list-tbody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text2)">' + escHtml(msg) + '</td></tr>';
  }

  // ── 测试通知 ──────────────────────────────────────────────────────
  window.sendTestNotification = async function () {
    if (_cooldown) return;
    var btn = document.getElementById('alerts-test-btn');
    if (btn) {
      btn.disabled = true;
      _cooldown = true;
    }
    try {
      var r = await fetch('/live/notifications/test', { method: 'POST' });
      var d = await r.json();
      if (d.sent) {
        // 成功后立即刷新列表
        loadList(_currentLevel);
        loadStatus();
      } else {
        alert(d.msg || '发送失败');
      }
    } catch (e) {
      alert('发送异常');
    } finally {
      if (btn) {
        setTimeout(function () {
          btn.disabled = false;
          _cooldown = false;
        }, 3000);
      }
    }
  };

  // ── 过滤 chips ────────────────────────────────────────────────────
  window.filterAlerts = function (el, level) {
    _currentLevel = level;
    // chip 样式切换
    document.querySelectorAll('.alerts-chips .chip').forEach(function (c) {
      c.classList.toggle('active', c.getAttribute('data-level') === level);
    });
    loadList(level);
  };

  // ── 工具 ─────────────────────────────────────────────────────────
  function fmtTime(iso) {
    try {
      var d = new Date(iso);
      var h = String(d.getHours()).padStart(2, '0');
      var m = String(d.getMinutes()).padStart(2, '0');
      var s = String(d.getSeconds()).padStart(2, '0');
      return h + ':' + m + ':' + s;
    } catch (e) {
      return '—';
    }
  }

  function escHtml(s) {
    if (!s) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
})();
