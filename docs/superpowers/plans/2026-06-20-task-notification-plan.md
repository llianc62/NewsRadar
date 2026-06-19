# 任务通知系统改造实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 crawl/sync 任务完成状态接入通知系统，用 SSE 替代前端轮询，并用 `asyncio.Queue` 替代 `asyncio.Event` 作为 daemon-web 通信 channel。

**Architecture:** 自底向上四层：crawler 返回值 → main.py Queue channel → app.py SSE + 闭包回调 → base.html EventSource。每层改动独立可测。

**Tech Stack:** Python 3.12+, asyncio, FastAPI + Starlette SSE, vanilla JavaScript EventSource, pytest

## Global Constraints

- `fetch_all` 和 `sync_from_cloud` 从 `-> None` 改为 `-> dict`，无调用者依赖返回值
- `create_app(signals=...)` 改为 `create_app(queues=...)`，仅 `main.py` 调用
- `_add_notification` 新增 `category` 和 `summary` 参数，默认值保持向后兼容
- 通知为内存态，重启后清空 — 和当前行为一致
- daemon 不应直接导入调用 `_add_notification`

---

### Task 1: crawler 返回值改造

**Files:**
- Modify: `news/crawler.py:250-298` (fetch_all)
- Modify: `news/crawler.py:648-766` (sync_from_cloud)

**Interfaces:**
- Produces: `fetch_all() -> dict` — `{"total": int, "hotlist": int, "rss": int}`
- Produces: `sync_from_cloud() -> dict` — `{"upserted": int, "skipped": int, "days": int}`

- [ ] **Step 1: 修改 `fetch_all` 返回值**

```python
# news/crawler.py:296-297 — 将 print 后隐式返回 None 改为 return dict
        print(f"=== Fetch complete: {len(all_items)} items ===")
        return {
            "total": len(all_items),
            "hotlist": len([i for i in all_items if i.get("source_type") == "hotlist"]),
            "rss": len([i for i in all_items if i.get("source_type") == "rss"]),
        }
```

- [ ] **Step 2: 修改 `sync_from_cloud` 返回值**

在 `news/crawler.py` 的 `sync_from_cloud` 方法末尾（约 763 行），将 `print` 后改为 `return dict`：

```python
# news/crawler.py:763-766 — 替换 print 为 return
        print(
            f"\n[Sync] Complete: {total_new} upserted, {total_skipped} skipped "
            f"({len(db_keys)} day(s) processed)"
        )
        return {
            "upserted": total_new,
            "skipped": total_skipped,
            "days": len(db_keys),
        }
```

同时将方法签名从 `def sync_from_cloud(self) -> None:` 改为 `def sync_from_cloud(self) -> dict:`。

- [ ] **Step 3: 运行现有测试验证向后兼容**

Run: `pytest tests/ -v`
Expected: 所有现有测试 PASS（无调用者依赖返回值）

- [ ] **Step 4: Commit**

```bash
git add news/crawler.py
git commit -m "feat: fetch_all 和 sync_from_cloud 返回 dict 结果"
```

---

### Task 2: main.py — Event → Queue channel 迁移

**Files:**
- Modify: `main.py:60-62` (__init__ — signals → queues)
- Modify: `main.py:82-88` (_timer)
- Modify: `main.py:92-122` (_worker, _wait_signal → _wait_queue, _try_run_job)
- Modify: `main.py:126-141` (_crawl_job, _sync_job)
- Modify: `main.py:144-213` (run — signals dict → queues dict)

**Interfaces:**
- Consumes: `fetch_all() -> dict`, `sync_from_cloud() -> dict` (from Task 1)
- Produces: `_wait_queue(queue) -> Callable | None`, `_try_run_job(name, job, callback=None)`, `queues: dict[str, asyncio.Queue]`

- [ ] **Step 1: __init__ — 替换 signal 为 queue**

```python
# main.py:60-62 — 替换
        # ── Channels (asyncio.Queue — Go-style signal + data carrier) ──
        self._crawl_queue: asyncio.Queue = asyncio.Queue()
        self._sync_queue: asyncio.Queue = asyncio.Queue()
```

删除原有的 `self._crawl_signal = asyncio.Event()` 和 `self._sync_signal = asyncio.Event()`。

- [ ] **Step 2: 改造 `_timer` — set() → put(None)**

```python
# main.py:82-88 — 替换
    async def _timer(self, queue: asyncio.Queue, interval_min: int, name: str) -> None:
        """Put None into *queue* every *interval_min* minutes to wake the Worker."""
        print(f"[Timer/{name}] every {interval_min} min")
        while not self._shutdown_event.is_set():
            await self._sleep_or_shutdown(interval_min * 60)
            if not self._shutdown_event.is_set():
                await queue.put(None)   # None = timer-triggered, no notification
```

- [ ] **Step 3: 改造 `_worker` — 用 queue.get() 替代 signal.wait()**

```python
# main.py:92-100 — 替换
    async def _worker(self, name: str, queue: asyncio.Queue, job) -> None:
        """Wait for an item from *queue*, then execute *job*.
        
        queue item ``None`` → timer-triggered, skip notification.
        queue item ``Callable`` → manual trigger, call it on completion.
        """
        print(f"[Worker/{name}] ready")
        while not self._shutdown_event.is_set():
            callback = await self._wait_queue(queue)
            if callback is None and self._shutdown_event.is_set():
                break
            await self._try_run_job(name, job, callback)
```

- [ ] **Step 4: 新增 `_wait_queue` — 替代 `_wait_signal`**

```python
# main.py:102-111 — 删除 _wait_signal，新增 _wait_queue
    async def _wait_queue(self, queue: asyncio.Queue):
        """Block until queue has data or shutdown is requested.
        
        Returns the queue item (None or callable), or None on shutdown.
        """
        get_task = asyncio.create_task(queue.get(), name="q_get")
        shut_task = asyncio.create_task(self._shutdown_event.wait(), name="q_shut")
        done, pending = await asyncio.wait(
            [get_task, shut_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if shut_task in done:
            return None   # shutdown
        return done.pop().result()
```

- [ ] **Step 5: 改造 `_try_run_job` — 接收 callback 参数**

```python
# main.py:113-122 — 替换
    async def _try_run_job(self, name: str, job, callback=None) -> None:
        """Execute *job*. If *callback* is not None, call it with the result.
        
        Args:
            name: Human-readable job name for logging.
            job: Async callable returning a dict.
            callback: ``(bool, str) -> None`` or ``None``.
                      ``None`` means timer-triggered — skip notification.
        """
        try:
            print(f"\n[{name}] Starting...")
            result = await job()
            print(f"[{name}] Complete.")
            if callback is not None:
                if isinstance(result, dict) and "success" in result:
                    callback(result["success"], result.get("summary", f"{name} 完成"))
                elif result:
                    callback(True, str(result)[:500])
                else:
                    callback(True, f"{name} 完成")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{name}] Failed (non-fatal): {e}")
            if callback is not None:
                callback(False, str(e)[:500])
```

- [ ] **Step 6: 改造 `_crawl_job` 和 `_sync_job` — 返回 dict**

```python
# main.py:126-131 — _crawl_job 替换
    async def _crawl_job(self) -> dict:
        """Fetch news (with content) → save to PostgreSQL."""
        crawler = Crawler(self.config, pg_db=self.db)
        result = await self._run_in_thread(
            crawler.fetch_all, OutputStyle.POSTGRESQL, True, True
        )
        total = result.get("total", 0) if result else 0
        return {
            "success": True,
            "summary": f"抓取完成，共 {total} 条新闻" if total > 0 else "抓取完成，无新新闻",
            "count": total,
        }
```

```python
# main.py:133-140 — _sync_job 替换
    async def _sync_job(self) -> dict:
        """Sync cloud SQLite data into PostgreSQL."""
        cloud_config = self.config["storage"]["cloud"]
        if not (cloud_config.get("bucket_name") and cloud_config.get("endpoint_url")):
            return {"success": True, "summary": "云端未配置 — 已跳过同步", "count": 0}
        crawler = Crawler(self.config, pg_db=self.db)
        result = await self._run_in_thread(crawler.sync_from_cloud)
        total = result.get("upserted", 0) if result else 0
        return {
            "success": True,
            "summary": f"同步完成，新增 {total} 条" if total > 0 else "同步完成，无新数据",
            "count": total,
        }
```

- [ ] **Step 7: 改造 `run()` — signals dict → queues dict，Timer/Worker 传 queue**

```python
# main.py:158-165 — 替换 signals 为 queues
        # 3. Start web server first — non-blocking
        queues = {
            "crawl": self._crawl_queue,
            "sync": self._sync_queue,
        }
        s3_config = self.config.get("storage", {}).get("resource", {})
        web_crawler = Crawler(self.config, pg_db=self.db)
        app = create_app(self.db, s3_config, queues=queues, crawler=web_crawler)
```

```python
# main.py:168-186 — Worker 和 Timer 启动改为传 queue
        # 4. Launch Workers
        for coro in [
            lambda: self._worker("Crawl", self._crawl_queue, self._crawl_job),
            lambda: self._worker("Sync", self._sync_queue, self._sync_job),
        ]:
            t = asyncio.create_task(coro(), name=coro.__name__)
            self._bg_tasks.append(t)

        # 5. Launch Timers
        crawl_interval = self.config.get("crawler", {}).get("daemon_interval_minutes", 60)
        sync_interval = self.config.get("crawler", {}).get("sync_interval_minutes", 60)

        for queue, interval, name in [
            (self._crawl_queue, crawl_interval, "Crawl"),
            (self._sync_queue, sync_interval, "Sync"),
        ]:
            t = asyncio.create_task(self._timer(queue, interval, name), name=f"timer_{name}")
            self._bg_tasks.append(t)
```

- [ ] **Step 8: 运行现有测试验证兼容性**

Run: `pytest tests/ -v`
Expected: 所有现有测试 PASS

- [ ] **Step 9: Commit**

```bash
git add main.py
git commit -m "refactor: asyncio.Event → asyncio.Queue channel for daemon workers"
```

---

### Task 3: web/app.py — SSE + 通知扩展 + trigger 改造

**Files:**
- Modify: `web/app.py:75-94` (_add_notification)
- Modify: `web/app.py:158-165` (create_app 签名)
- Modify: `web/app.py:435-454` (trigger 端点)
- Modify: `web/app.py:557-590` (notification API 区)
- Create: 无新建文件，新增模块级变量和函数

**Interfaces:**
- Consumes: `queues: dict[str, asyncio.Queue]` (from Task 2)
- Produces: `_push_sse_event(data: dict) -> None`, `GET /api/notifications/stream`, `POST /api/notifications/mark-all-read`
- Modifies: `_add_notification` 新增 `category`, `summary` 参数

- [ ] **Step 1: 新增模块级 SSE 状态变量**

在 `web/app.py` 顶部 (靠近 `_notifications`, `_notification_lock` 等模块变量处，约 67 行后)，插入：

```python
# ── SSE state ──
_sse_clients: set["asyncio.Queue"] = set()
_sse_event_loop: "asyncio.AbstractEventLoop | None" = None
```

同时在文件顶部的 import 中新增：

```python
import json
import asyncio
```

（`asyncio` 和 `json` 可能已在其他地方 import，检查后按需添加）

- [ ] **Step 2: 新增 `_push_sse_event` 函数**

在 `_add_notification` 之后（约 94 行后）插入：

```python
def _push_sse_event(data: dict) -> None:
    """Push an SSE event to all connected clients. Thread-safe."""
    loop = _sse_event_loop
    if loop is None or not _sse_clients:
        return

    def _put():
        for q in list(_sse_clients):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass

    try:
        running = asyncio.get_running_loop()
        if running is loop:
            _put()
        else:
            loop.call_soon_threadsafe(_put)
    except RuntimeError:
        # No running loop — called from thread pool thread
        loop.call_soon_threadsafe(_put)
```

- [ ] **Step 3: 修改 `_add_notification` 签名**

```python
# web/app.py:75-94 — 替换
def _add_notification(
    article_id: int,
    title: str,
    status: str = "pending",
    error_message: str = "",
    category: str = "fetch",
    summary: str = "",
) -> dict:
    """Create a notification, append to list, return the dict."""
    global _notification_counter
    with _notification_lock:
        _notification_counter += 1
        notif = {
            "id": _notification_counter,
            "category": category,
            "article_id": article_id,
            "title": title,
            "summary": summary,
            "status": status,
            "error_message": error_message,
            "is_read": False,
            "created_at": _now(),
        }
        _notifications.insert(0, notif)
        # Cap at 50
        if len(_notifications) > 50:
            _notifications.pop()
        # Push SSE event for new notification
        _push_sse_event({"type": "new", "notification": dict(notif)})
        return notif
```

- [ ] **Step 4: 修改 `create_app` 签名 — signals → queues**

```python
# web/app.py:158 — 修改函数签名
def create_app(db, s3_config: dict, queues: dict = None, crawler=None):
    """Create and configure the FastAPI application.
    
    Args:
        ...
        queues: Optional dict of ``asyncio.Queue`` for manual trigger +
                notification callback. Keys: ``"crawl"``, ``"sync"``.
    """
```

```python
# web/app.py:192 — 替换 app.state.signals
    app.state.queues = queues or {}
```

- [ ] **Step 5: 改造 trigger 端点 — 闭包回调 + queue.put**

```python
# web/app.py:437-453 — 替换 trigger_crawl 和 trigger_sync
    @app.post("/api/trigger/crawl")
    async def trigger_crawl():
        """Manually trigger a crawl job with notification."""
        queue = app.state.queues.get("crawl")
        if queue is None:
            return JSONResponse({"ok": False, "error": "not available"}, status_code=404)

        notif = _add_notification(0, "新闻抓取", "pending", category="crawl")

        def on_complete(success: bool, summary: str):
            with _notification_lock:
                notif["status"] = "completed" if success else "failed"
                notif["summary"] = summary
            _push_sse_event({"type": "update", "notification": dict(notif)})

        await queue.put(on_complete)
        return {"ok": True, "task": "crawl", "notif_id": notif["id"]}

    @app.post("/api/trigger/sync")
    async def trigger_sync():
        """Manually trigger a cloud sync job with notification."""
        queue = app.state.queues.get("sync")
        if queue is None:
            return JSONResponse({"ok": False, "error": "not available"}, status_code=404)

        notif = _add_notification(0, "云端同步", "pending", category="sync")

        def on_complete(success: bool, summary: str):
            with _notification_lock:
                notif["status"] = "completed" if success else "failed"
                notif["summary"] = summary
            _push_sse_event({"type": "update", "notification": dict(notif)})

        await queue.put(on_complete)
        return {"ok": True, "task": "sync", "notif_id": notif["id"]}
```

- [ ] **Step 6: 新增 SSE endpoint**

在 trigger API 区之后（约 454 行之后）插入：

```python
    # ── SSE stream ──────────────────────────────────────────────

    @app.get("/api/notifications/stream")
    async def notification_stream(request: Request):
        """SSE endpoint — pushes new/updated notifications to the client."""
        from starlette.responses import StreamingResponse

        global _sse_event_loop
        if _sse_event_loop is None:
            _sse_event_loop = asyncio.get_running_loop()

        queue: asyncio.Queue = asyncio.Queue()
        _sse_clients.add(queue)

        async def event_generator():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                _sse_clients.discard(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
```

- [ ] **Step 7: 新增 `mark-all-read` 端点**

在 `mark_notification_read` 之后（约 590 行）插入：

```python
    @app.post("/api/notifications/mark-all-read")
    async def mark_all_read():
        """Mark all notifications as read."""
        with _notification_lock:
            for n in _notifications:
                n["is_read"] = True
        return {"ok": True}
```

- [ ] **Step 8: 在 fetch/refetch 的 `_run_fetch_url` / `_run_refetch` 中添加 SSE 推送**

在 `_run_fetch_url`（约 97-109 行）状态变更后添加 `_push_sse_event`：

```python
def _run_fetch_url(url: str, crawler, notif: dict, db) -> None:
    """Execute URL fetch in background — thin wrapper around crawler.fetch()."""
    try:
        notif["status"] = "running"
        _push_sse_event({"type": "update", "notification": dict(notif)})
        crawler.fetch(url, OutputStyle.POSTGRESQL, True, True)
        notif["status"] = "completed"
        # 回填 article_id
        article = db.get_article_by_url(url)
        if article:
            notif["article_id"] = article["id"]
    except Exception as e:
        notif["status"] = "failed"
        notif["error_message"] = str(e)[:500]
    finally:
        _push_sse_event({"type": "update", "notification": dict(notif)})
```

对 `_run_refetch`（约 112 行起）做同样的修改 — 在状态变更后添加 `_push_sse_event`。

- [ ] **Step 9: 运行现有测试验证兼容性**

Run: `pytest tests/ -v`
Expected: 所有现有测试 PASS（尤其是 `test_refetch.py`, `test_notification_frontend.py`）

- [ ] **Step 10: Commit**

```bash
git add web/app.py
git commit -m "feat: SSE 推送 + mark-all-read + trigger 闭包回调 + 通知结构扩展"
```

---

### Task 4: base.html — SSE 替代轮询 + 前端改造

**Files:**
- Modify: `web/templates/base.html:22-267` (整个内联 `<script>`)

**Interfaces:**
- Consumes: `GET /api/notifications/stream` (SSE), `POST /api/notifications/mark-all-read` (from Task 3)
- Removes: `setInterval(fetchUnreadCount, POLL_INTERVAL)`, `fetchUnreadCount()`

- [ ] **Step 1: 替换整个 `<script>` 块**

将 `base.html:22-267` 的 `<script>` 内容替换为：

```javascript
<script>
(function() {
  'use strict';

  var TOAST_DURATION = 5000;
  var shownIds = {};   // track which notifs already toasted

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

  function showToast(notif) {
    var isTask = notif.category === 'crawl' || notif.category === 'sync';
    var canNavigate = !isTask && notif.status === 'completed' && notif.article_id > 0;
    buildToast({
      title: notif.title,
      sub: isTask
        ? (notif.summary || (notif.status === 'completed' ? '任务完成' : '任务失败'))
        : (notif.status === 'completed' ? '抓取完成' : '抓取失败'),
      kind: notif.status === 'completed' ? 'done' : 'fail',
      onClick: canNavigate ? function() { window.location.href = '/news/' + notif.article_id; } : null,
    });
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

  window.markAndGo = function(notifId, articleId, status, category) {
    fetch('/api/notifications/' + notifId + '/read', { method: 'POST' });
    if (status === 'completed' && articleId > 0 && category !== 'crawl' && category !== 'sync') {
      window.location.href = '/news/' + articleId;
    }
    closeDrawer();
  };

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
  function updateBadge(count) {
    var badge = document.getElementById('bell-badge');
    if (!badge) return;
    badge.setAttribute('data-count', count);
    badge.textContent = count;
    badge.style.animation = 'none';
    badge.offsetHeight;
    badge.style.animation = '';
  }

  // ── Drawer ──
  window.toggleDrawer = function(event) {
    event && event.stopPropagation();
    var drawer = document.getElementById('notify-drawer');
    var overlay = document.getElementById('notify-overlay');
    if (!drawer || !overlay) return;
    if (drawer.classList.contains('is-open')) {
      closeDrawer();
    } else {
      drawer.classList.add('is-open');
      overlay.classList.add('is-open');
      document.body.style.overflow = 'hidden';
      fetchNotifications(true);
      // 打开抽屉 = 全部已读
      fetch('/api/notifications/mark-all-read', { method: 'POST' })
        .then(function() { updateBadge(0); });
    }
  };

  window.closeDrawer = function() {
    var drawer = document.getElementById('notify-drawer');
    var overlay = document.getElementById('notify-overlay');
    if (!drawer || !overlay) return;
    drawer.classList.remove('is-open');
    overlay.classList.remove('is-open');
    document.body.style.overflow = '';
  };

  // ── Fetch ──
  function fetchNotifications(forDrawer) {
    fetch('/api/notifications')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (!Array.isArray(data)) return;
        if (forDrawer) {
          renderDrawerList(data);
          return;
        }
        data.forEach(function(n) {
          if (!shownIds[n.id] && (n.status === 'completed' || n.status === 'failed')) {
            shownIds[n.id] = true;
            var path = window.location.pathname;
            if (path.indexOf('/hot-news') === 0 || path.indexOf('/news/') === 0) {
              showToast(n);
            }
          }
        });
      })
      .catch(function() { /* ignore network errors */ });
  }

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

  function renderDrawerList(data) {
    var list = document.getElementById('notify-list');
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

      html +=
        '<div class="notify-item' + readClass + '" data-id="' + escapeAttr(n.id)
          + '" onclick="markAndGo('
          + escapeAttr(n.id) + ', '
          + escapeAttr(n.article_id) + ', '
          + escapeAttr(n.status) + ', '
          + escapeAttr(n.category)
          + ')">'
          + '<span class="notify-item-dot ' + dotClass + '"></span>'
          + '<div class="notify-item-body">'
            + '<div class="notify-item-title">' + escapeHtml(n.title) + '</div>'
            + (n.summary && isTask
                ? '<div class="notify-item-summary">' + escapeHtml(n.summary) + '</div>'
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
  }

  // ── Initialization ──
  var path = window.location.pathname;
  if (path.indexOf('/hot-news') === 0 || path.indexOf('/news/') === 0) {
    // Create toast container if not present
    if (!document.getElementById('toast-container')) {
      var tc = document.createElement('div');
      tc.className = 'toast-container';
      tc.id = 'toast-container';
      document.body.appendChild(tc);
    }

    // Seed shownIds with existing notifications so reconnect doesn't re-toast
    fetch('/api/notifications')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (Array.isArray(data)) {
          data.forEach(function(n) { shownIds[n.id] = true; });
        }
      })
      .catch(function() {});

    // ── SSE connection (replaces polling) ──
    var es = new EventSource('/api/notifications/stream');

    es.onopen = function() {
      console.log('[SSE] connected');
      // 初始获取未读计数（SSE 重连后同步 badge）
      fetch('/api/notifications/unread-count')
        .then(function(r) { return r.json(); })
        .then(function(d) { updateBadge(d.count || 0); });
    };

    es.onerror = function() {
      console.log('[SSE] connection lost, will retry...');
    };

    es.addEventListener('new', function(e) {
      var payload = JSON.parse(e.data);
      var notif = payload.notification;
      if (!notif) return;

      // 更新铃铛红点
      fetch('/api/notifications/unread-count')
        .then(function(r) { return r.json(); })
        .then(function(d) { updateBadge(d.count || 0); });

      // 如果不在 hot-news 或 news detail 页面，不弹 toast
      if (path.indexOf('/hot-news') !== 0 && path.indexOf('/news/') !== 0) return;

      // 根据 category 显示不同 toast
      if (notif.category === 'crawl' || notif.category === 'sync') {
        showAppToast(
          notif.title,
          notif.summary || '任务已触发，正在执行…',
          'info'
        );
      } else {
        if (!shownIds[notif.id]) {
          shownIds[notif.id] = true;
          showToast(notif);
        }
      }
    });

    es.addEventListener('update', function(e) {
      var payload = JSON.parse(e.data);
      var notif = payload.notification;
      if (!notif) return;

      // 更新铃铛红点
      fetch('/api/notifications/unread-count')
        .then(function(r) { return r.json(); })
        .then(function(d) { updateBadge(d.count || 0); });

      if (path.indexOf('/hot-news') !== 0 && path.indexOf('/news/') !== 0) return;

      // 任务完成/失败 — toast
      var kind = notif.status === 'completed' ? 'done' : 'fail';
      var sub = notif.status === 'completed'
        ? (notif.summary || '任务完成')
        : (notif.error_message || '任务失败');

      // 抽屉打开则刷新列表
      var drawer = document.getElementById('notify-drawer');
      if (drawer && drawer.classList.contains('is-open')) {
        fetchNotifications(true);
      }

      showAppToast(notif.title, sub, kind);
    });
  }
})();
</script>
```

- [ ] **Step 2: 验证前端语法**

Run: 在浏览器中打开 `/hot-news` 页面，检查浏览器控制台无 JS 错误
Expected: `[SSE] connected` 日志出现

- [ ] **Step 3: Commit**

```bash
git add web/templates/base.html
git commit -m "feat: SSE 替代轮询 + 通知前端改造 (category/toggleDrawer/markAndGo)"
```

---

### Task 5: 集成测试

**Files:**
- Create: `tests/test_task_notification.py`

**Interfaces:**
- Consumes: `create_app(queues=...)` (from Task 3), `_add_notification(category=..., summary=...)` (from Task 3)
- Produces: 测试覆盖 SSE stream, trigger 端点, mark-all-read, 通知结构扩展

- [ ] **Step 1: 编写测试文件**

```python
# tests/test_task_notification.py
"""Integration tests for task notification system."""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from web.app import create_app, _add_notification, _notifications, _notification_counter
from web.app import _sse_clients, _push_sse_event, _notification_lock


@pytest.fixture(autouse=True)
def _clear_notifications():
    """Reset module-level notification state before each test."""
    global _notifications, _notification_counter
    with _notification_lock:
        _notifications.clear()
        _notification_counter = 0
    _sse_clients.clear()


@pytest.fixture
def db_mock():
    db = Mock()
    db.is_connected = True
    db.get_article_by_url = Mock(return_value=None)
    db.get_news_by_id = Mock(return_value={"id": 1, "title": "test", "url": "http://example.com"})
    return db


@pytest.fixture
def s3_config():
    return {
        "endpoint_url": "http://localhost:9000",
        "bucket_name": "test-bucket",
        "access_key_id": "minioadmin",
        "secret_access_key": "minioadmin",
        "region": "",
    }


@pytest.fixture
def app_with_queues(db_mock, s3_config):
    """Create app with mock queues for testing."""
    import asyncio
    queues = {
        "crawl": asyncio.Queue(),
        "sync": asyncio.Queue(),
    }
    app = create_app(db_mock, s3_config, queues=queues, crawler=None)
    return app, queues


class TestAddNotification:
    """Tests for _add_notification structure extension."""

    def test_default_category_is_fetch(self):
        notif = _add_notification(1, "test title", "pending")
        assert notif["category"] == "fetch"
        assert notif["summary"] == ""

    def test_explicit_category_crawl(self):
        notif = _add_notification(0, "新闻抓取", "pending", category="crawl", summary="任务已触发")
        assert notif["category"] == "crawl"
        assert notif["summary"] == "任务已触发"
        assert notif["article_id"] == 0

    def test_explicit_category_sync(self):
        notif = _add_notification(0, "云端同步", "pending", category="sync")
        assert notif["category"] == "sync"
        assert notif["article_id"] == 0

    def test_new_fields_compatible_with_old_callers(self):
        """Old callers passing only 4 positional args still work."""
        notif = _add_notification(123, "article title", "pending", "some error")
        assert notif["category"] == "fetch"
        assert notif["summary"] == ""
        assert notif["article_id"] == 123
        assert notif["error_message"] == "some error"

    def test_notification_capped_at_50(self):
        for i in range(60):
            _add_notification(i, f"title {i}", category="fetch")
        with _notification_lock:
            assert len(_notifications) == 50
            assert _notifications[0]["title"] == "title 59"


class TestMarkAllRead:
    """Tests for POST /api/notifications/mark-all-read."""

    def test_mark_all_read(self, app_with_queues):
        app, _ = app_with_queues
        client = _make_test_client(app)

        # Create some unread notifications
        _add_notification(1, "test1", "completed")
        _add_notification(2, "test2", "completed")

        resp = client.post("/api/notifications/mark-all-read")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        with _notification_lock:
            for n in _notifications:
                assert n["is_read"] is True


class TestTriggerEndpoints:
    """Tests for POST /api/trigger/{crawl,sync} with queue channels."""

    def test_trigger_crawl_puts_callback_on_queue(self, app_with_queues):
        app, queues = app_with_queues
        client = _make_test_client(app)

        resp = client.post("/api/trigger/crawl")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["task"] == "crawl"
        assert "notif_id" in body

        # Queue should have exactly 1 item (the callback closure)
        assert not queues["crawl"].empty()

        # Consume the callback and verify it's callable
        cb = queues["crawl"].get_nowait()
        assert callable(cb)

        # Call it with success and verify notification updates
        cb(True, "抓取完成，共 23 条新闻")
        with _notification_lock:
            notif = _notifications[0]
            assert notif["status"] == "completed"
            assert notif["summary"] == "抓取完成，共 23 条新闻"

    def test_trigger_sync_puts_callback_on_queue(self, app_with_queues):
        app, queues = app_with_queues
        client = _make_test_client(app)

        resp = client.post("/api/trigger/sync")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["task"] == "sync"

        assert not queues["sync"].empty()
        cb = queues["sync"].get_nowait()
        assert callable(cb)

        cb(False, "Connection refused")
        with _notification_lock:
            notif = _notifications[0]
            assert notif["status"] == "failed"
            assert notif["summary"] == "Connection refused"

    def test_trigger_without_queue_returns_404(self, db_mock, s3_config):
        app = create_app(db_mock, s3_config, queues=None, crawler=None)
        client = _make_test_client(app)

        resp = client.post("/api/trigger/crawl")
        assert resp.status_code == 404

    def test_trigger_crawl_creates_notification_with_correct_structure(self, app_with_queues):
        app, _ = app_with_queues
        client = _make_test_client(app)

        resp = client.post("/api/trigger/crawl")
        body = resp.json()
        notif_id = body["notif_id"]

        with _notification_lock:
            notif = [n for n in _notifications if n["id"] == notif_id][0]
        assert notif["category"] == "crawl"
        assert notif["title"] == "新闻抓取"
        assert notif["status"] == "pending"
        assert notif["article_id"] == 0


class TestSSE:
    """Tests for SSE stream endpoint."""

    def test_sse_endpoint_returns_200(self, app_with_queues):
        app, _ = app_with_queues
        client = _make_test_client(app)

        # Use stream=True for SSE testing
        with client.stream("GET", "/api/notifications/stream") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache, no-transform"

    def test_push_sse_event_delivers_to_client(self, app_with_queues):
        app, _ = app_with_queues
        client = _make_test_client(app)

        with client.stream("GET", "/api/notifications/stream") as response:
            # Push an event
            _push_sse_event({"type": "test", "message": "hello"})

            # Read the SSE line (with timeout via chunk iteration)
            import itertools
            chunks = []
            for chunk in itertools.islice(response.iter_bytes(), 3):
                chunks.append(chunk.decode("utf-8"))

            body = "".join(chunks)
            assert "data:" in body
            assert "hello" in body


# ── helper ──

def _make_test_client(app):
    """Create a synchronous Starlette test client for the given FastAPI app."""
    from starlette.testclient import TestClient
    return TestClient(app)
```

- [ ] **Step 2: 运行测试验证**

Run: `pytest tests/test_task_notification.py -v`
Expected: 所有新测试 PASS

- [ ] **Step 3: 运行全部测试套件**

Run: `pytest tests/ -v`
Expected: 全部测试 PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_task_notification.py
git commit -m "test: task notification system — SSE + Queue + mark-all-read"
```

---

### Task 6: 端到端验证

- [ ] **Step 1: 启动 daemon 并验证 SSE 连接**

```bash
python main.py &
sleep 3
curl -N -s http://localhost:8000/api/notifications/stream &
sleep 1
```

Expected: 看到 `: keepalive\n\n` 心跳

- [ ] **Step 2: 触发 crawl 并验证通知**

```bash
curl -X POST http://localhost:8000/api/trigger/crawl
sleep 5
curl http://localhost:8000/api/notifications | python -m json.tool
```

Expected: 通知存在，`category` 为 `"crawl"`，`status` 为 `"completed"` 或 `"failed"`

- [ ] **Step 3: 测试 mark-all-read**

```bash
curl -X POST http://localhost:8000/api/notifications/mark-all-read
curl http://localhost:8000/api/notifications/unread-count
```

Expected: `{"count": 0}`

- [ ] **Step 4: 清理**

```bash
kill %1  # stop daemon
```

