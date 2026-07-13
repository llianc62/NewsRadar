/**
 * notification.js — Global notification system.
 *
 * SSE connection: unconditional, no path whitelist.
 * Toast: all SSE events trigger toast regardless of scope.
 * Drawer + badge: scoped by module (news / agent).
 *
 * Exposes on window:
 *   showAppToast(title, sub, kind, onClick)
 *   toggleDrawer(scope)
 *   closeDrawer(scope)
 *   markAndGo(notifId, articleId, status, category)
 *   fetchNotifications(scope, forDrawer)
 *   renderDrawerList(data, scope)
 *   updateBadge(scope, count)
 */
(function() {
  'use strict';

  var TOAST_DURATION = 5000;
  var shownIds = {};

  // ── Toast ──
  function buildToast(opts) {
    var container = document.getElementById('toast-container');
    if (!container) return null;

    var toast = document.createElement('div');
    toast.className = 'toast ' + (opts.kind === 'fail' ? 'fail' : 'done');

    var body = document.createElement('div');
    body.className = 'toast-body';

    var title = document.createElement('div');
    title.className = 'toast-title';
    title.innerHTML = '<span class="dot"></span>' + escapeHtml(opts.title);

    var sub = document.createElement('div');
    sub.className = 'toast-sub';
    sub.textContent = opts.sub;

    body.appendChild(title);
    body.appendChild(sub);
    toast.appendChild(body);

    if (opts.onClick) {
      toast.style.cursor = 'pointer';
      toast.addEventListener('click', opts.onClick);
    } else {
      toast.style.cursor = 'default';
    }

    container.appendChild(toast);

    setTimeout(function() {
      toast.classList.add('fading');
      setTimeout(function() {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 300);
    }, opts.duration || TOAST_DURATION);
    return toast;
  }

  window.showAppToast = function(title, sub, kind, onClick) {
    buildToast({ title: title, sub: sub || '', kind: kind || 'done',
                 onClick: onClick || null });
  };

  function escapeHtml(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  function escapeAttr(str) {
    return String(str).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ── Mark & Navigate ──
  window.markAndGo = function(notifId, articleId, status, category) {
    fetch('/api/notifications/' + notifId + '/read', { method: 'POST' });
    if (status === 'completed' && articleId > 0 && category !== 'crawl' && category !== 'sync') {
      window.location.href = '/news/' + articleId;
    }
    closeDrawer();
  };

  // ── Time formatting ──
  function formatRelativeTime(ts) {
    if (!ts) return '';
    var now = Date.now() / 1000;
    var diff = now - ts;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 604800) return Math.floor(diff / 86400) + ' 天前';
    return new Date(ts * 1000).toLocaleDateString('zh-CN');
  }

  // ── Badge ──
  window.updateBadge = function(scope, count) {
    var badge = document.getElementById('bell-badge-' + scope);
    if (!badge) return;
    badge.setAttribute('data-count', count);
    badge.textContent = count;
    badge.style.animation = 'none';
    badge.offsetHeight;
    badge.style.animation = '';
  };

  // ── Drawer ──
  window.toggleDrawer = function(scope) {
    var drawer = document.getElementById('notify-drawer-' + scope);
    var overlay = document.getElementById('notify-overlay-' + scope);
    if (!drawer || !overlay) return;
    if (drawer.classList.contains('is-open')) {
      closeDrawer(scope);
    } else {
      drawer.classList.add('is-open');
      overlay.classList.add('is-open');
      document.body.style.overflow = 'hidden';
      fetchNotifications(scope, true);
      // 打开抽屉 = 全部已读
      var url = scope
        ? '/api/notifications/mark-all-read?scope=' + encodeURIComponent(scope)
        : '/api/notifications/mark-all-read';
      fetch(url, { method: 'POST' })
        .then(function() { updateBadge(scope, 0); });
    }
  };

  window.closeDrawer = function(scope) {
    var drawer = document.getElementById('notify-drawer-' + scope);
    var overlay = document.getElementById('notify-overlay-' + scope);
    if (!drawer || !overlay) return;
    drawer.classList.remove('is-open');
    overlay.classList.remove('is-open');
    document.body.style.overflow = '';
  };

  // ── Fetch ──
  window.fetchNotifications = function(scope, forDrawer) {
    var url = '/api/notifications';
    if (scope) url += '?scope=' + encodeURIComponent(scope);
    fetch(url)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (!Array.isArray(data)) return;
        if (forDrawer) {
          renderDrawerList(data, scope);
          return;
        }
        // Seed shownIds (called at init)
        data.forEach(function(n) { shownIds[n.id] = true; });
      })
      .catch(function() { /* ignore network errors */ });
  };

  function getStatusText(category, status) {
    if (category === 'crawl' || category === 'sync') {
      if (status === 'pending')  return '排队中';
      if (status === 'running')  return '执行中';
      if (status === 'completed') return '已完成';
      if (status === 'failed')   return '执行失败';
    }
    if (status === 'pending')  return '等待抓取';
    if (status === 'running')  return '抓取中';
    if (status === 'completed') return '抓取成功';
    if (status === 'failed')   return '抓取失败';
    return status;
  }

  window.renderDrawerList = function(data, scope) {
    var list = document.getElementById('notify-list-' + scope);
    if (!list) return;

    if (data.length === 0) {
      list.innerHTML = '<div class="notify-empty">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>' +
        '<span>暂无消息</span></div>';
      return;
    }

    var html = '';
    data.forEach(function(n) {
      var dotClass = n.status === 'completed' ? 'done'
        : n.status === 'failed' ? 'fail'
        : 'running';
      var statusText = getStatusText(n.category, n.status);
      var readClass = n.is_read ? ' is-read' : '';
      var timeStr = formatRelativeTime(n.created_at);
      var isTask = n.category === 'crawl' || n.category === 'sync';

      var subText = '';
      if (n.status === 'failed') {
        subText = n.error_message || n.summary || '';
      } else if (n.summary && isTask) {
        subText = n.summary;
      }

      html +=
        '<div class="notify-item' + readClass + '" data-id="' + escapeAttr(n.id)
          + '" onclick="markAndGo('
          + escapeAttr(n.id) + ', '
          + escapeAttr(n.article_id) + ', '
          + "'" + escapeAttr(n.status) + "', "
          + "'" + escapeAttr(n.category) + "'"
          + ')">'
          + '<span class="notify-item-dot ' + dotClass + '"></span>'
          + '<div class="notify-item-body">'
            + '<div class="notify-item-title">' + escapeHtml(n.title) + '</div>'
            + (subText
                ? '<div class="notify-item-summary">' + escapeHtml(subText) + '</div>'
                : '')
            + '<div class="notify-item-meta">'
              + '<span class="notify-item-status ' + dotClass + '">'
                + statusText
              + '</span>'
              + (timeStr ? '<span class="notify-item-time">· '
                + escapeHtml(timeStr) + '</span>' : '')
            + '</div>'
          + '</div>'
        + '</div>';
    });
    list.innerHTML = html;
  };

  // ── Global SSE connection ──
  var es = new EventSource('/api/notifications/stream');

  function closeSSE() { es.close(); }
  window.addEventListener('beforeunload', closeSSE);
  window.addEventListener('pagehide', closeSSE);

  es.onopen = function() {
    console.log('[SSE] connected');
    // Sync badge on reconnect — query each scope that has a badge element
    document.querySelectorAll('[id^="bell-badge-"]').forEach(function(el) {
      var scope = el.id.replace('bell-badge-', '');
      var url = scope
        ? '/api/notifications/unread-count?scope=' + encodeURIComponent(scope)
        : '/api/notifications/unread-count';
      fetch(url)
        .then(function(r) { return r.json(); })
        .then(function(d) { updateBadge(scope, d.count || 0); });
    });
  };

  es.onerror = function() {
    console.log('[SSE] connection lost, will retry...');
  };

  es.addEventListener('new', function(e) {
    var payload = JSON.parse(e.data);
    var notif = payload.notification;
    if (!notif) return;

    // Update badge for this notification's scope
    var scope = notif.scope || 'news';
    var url = scope
      ? '/api/notifications/unread-count?scope=' + encodeURIComponent(scope)
      : '/api/notifications/unread-count';
    fetch(url)
      .then(function(r) { return r.json(); })
      .then(function(d) { updateBadge(scope, d.count || 0); });

    // Toast for crawl/sync triggers
    if (notif.category === 'crawl' || notif.category === 'sync') {
      showAppToast(
        notif.title,
        notif.summary || '任务已触发，正在执行…',
        'info'
      );
    }
  });

  es.addEventListener('update', function(e) {
    var payload = JSON.parse(e.data);
    var notif = payload.notification;
    if (!notif) return;

    // Update badge for this notification's scope
    var scope = notif.scope || 'news';
    var url = scope
      ? '/api/notifications/unread-count?scope=' + encodeURIComponent(scope)
      : '/api/notifications/unread-count';
    fetch(url)
      .then(function(r) { return r.json(); })
      .then(function(d) { updateBadge(scope, d.count || 0); });

    // Toast
    var kind, sub;
    if (notif.status === 'completed') {
      kind = 'done';
      sub = notif.summary || (
        notif.category === 'crawl' || notif.category === 'sync'
          ? '任务完成' : '抓取完成');
    } else if (notif.status === 'running') {
      kind = 'info';
      sub = notif.summary || '执行中…';
    } else {
      kind = 'fail';
      sub = notif.error_message || (
        notif.category === 'crawl' || notif.category === 'sync'
          ? '任务失败' : '抓取失败');
    }

    // If drawer for this scope is open, refresh list
    var drawer = document.getElementById('notify-drawer-' + scope);
    if (drawer && drawer.classList.contains('is-open')) {
      fetchNotifications(scope, true);
    }

    var isTask = notif.category === 'crawl' || notif.category === 'sync';
    var onClick = (notif.status === 'completed' && notif.article_id > 0 && !isTask)
      ? function() { window.location.href = '/news/' + notif.article_id; }
      : null;
    showAppToast(notif.title, sub, kind, onClick);
  });

  // ── Seed shownIds on load ──
  fetchNotifications(null, false);
})();
