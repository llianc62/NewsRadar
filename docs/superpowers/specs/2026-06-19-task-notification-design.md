# 任务通知系统改造设计

> 将 crawl/sync 任务完成状态接入通知系统，同时用 SSE 替代前端轮询，并对通知结构做扩展以支持非文章类通知。

## 1. 当前状态分析

### 1.1 架构现状

```
┌─ main.py (NewsRadarDaemon) ──────────────────────────────────┐
│                                                                │
│  Timer ──set()──▶ asyncio.Event ◀──await── Worker ──job──▶    │
│                                                                │
│  _crawl_job:  crawler.fetch_all() → PostgreSQL    (无返回值)   │
│  _sync_job:   crawler.sync_from_cloud() → PG      (无返回值)   │
│                                                                │
│  signals = { "crawl": Event, "sync": Event }                   │
│                                                                │
├─ web/app.py (FastAPI) ────────────────────────────────────────┤
│                                                                │
│  POST /api/trigger/crawl  → signal.set()  → {ok:true}         │
│  POST /api/trigger/sync   → signal.set()  → {ok:true}         │
│                                                                │
│  _add_notification() — 仅用于 fetch/refetch（文章级通知）      │
│  _run_fetch_url()    — 线程池执行，更新 notif dict             │
│  _run_refetch()      — 线程池执行，更新 notif dict             │
│                                                                │
│  前端轮询 GET /api/notifications (每 5s)                       │
│  前端轮询 GET /api/notifications/unread-count (每 5s)         │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

### 1.2 问题清单

| # | 问题 | 影响 |
|---|------|------|
| 1 | `_crawl_job` / `_sync_job` 完成/失败后无通知 | 用户不知道任务结果 |
| 2 | daemon worker 与通知系统无任何通信机制 | 架构断层 |
| 3 | 前端用 `setInterval` 每 5s 轮询两次 API | 无效请求，延迟高 |
| 4 | 通知结构面向文章（`title`=文章标题，`status`="抓取成功/失败"） | 无法表达任务类通知 |
| 5 | `markAndGo` 硬编码跳转文章详情 | crawl/sync 通知不应跳转 |
| 6 | `asyncio.Event` 只能传信号，不能带数据 — 信号和回调分离在两个变量中 | 不内聚 |

### 1.3 关键约束

- daemon 和 web 跑在**同一进程**中，共享模块级变量
- `_add_notification` 是 `web/app.py` 的内部函数 — daemon **不应直接导入调用**
- `_crawl_job` / `_sync_job` 的工作在 daemon 的 `ThreadPoolExecutor` 中执行（`_run_in_thread`）
- `fetch_all()` 和 `sync_from_cloud()` 当前返回 `None`
- 前端通知相关代码在 `base.html` 的内联 `<script>` 中

---

## 2. 设计原则

1. **Channel 通信替代分离的信号+回调** — 用 `asyncio.Queue` 同时承载"唤醒"和"回调"，模仿 Go channel 的语义。Timer 放入 `None`（只唤醒，不通知），Trigger 放入闭包（唤醒 + 通知）。Worker 从队列取到闭包则执行回调，取到 `None` 则只跑 job
2. **闭包封装通知逻辑** — 通知内容（标题、类别、状态文案）完全在 `app.py` 业务层通过闭包写好，daemon 完全不知道通知结构
3. **SSE 推送替代轮询** — 服务端主动推送，零延迟、零无效请求
4. **通知结构向后兼容** — 新增字段，不影响现有 fetch/refetch 通知
5. **最小侵入** — Worker/Timer 的骨架不变，只换通信原语和少量代码

---

## 3. 详细设计

### 3.1 Channel 通信机制（asyncio.Queue）

**核心思路：借鉴 Go channel —— 一个 `asyncio.Queue` 同时承载信号和数据。**

- **Timer** `put(None)` → Worker 取到 `None`，知道是定时触发，跳过通知
- **Trigger** `put(闭包)` → Worker 取到闭包，job 完成后调用它
- 不再需要分离的 `callbacks` dict，回调生命周期随 queue 自然管理

```
                   ┌──── asyncio.Queue ────┐
                   │                        │
 Timer ──put(None)─▶│  ◀── .get() ── Worker │── job（无通知）
                   │                        │
Trigger ──put(fn)──▶│  ◀── .get() ── Worker │── job ── callback(success, summary)
                   │                        │
                   └────────────────────────┘
```

#### 3.1.1 数据结构

`main.py` 中用 `asyncio.Queue` 替换 `asyncio.Event`：

```python
# __init__ 中（替换 self._crawl_signal / self._sync_signal）
self._crawl_queue: asyncio.Queue = asyncio.Queue()
self._sync_queue:  asyncio.Queue = asyncio.Queue()

# run() 中（替换 signals dict）
queues = {
    "crawl": self._crawl_queue,
    "sync":  self._sync_queue,
}
app = create_app(self.db, s3_config, queues=queues, crawler=web_crawler)

# Worker 启动（传入 queue 而非 signal）
for coro in [
    lambda: self._worker("Crawl", self._crawl_queue, self._crawl_job),
    lambda: self._worker("Sync",  self._sync_queue,  self._sync_job),
]: ...
```

#### 3.1.2 Timer 改造

`None` 表示"定时触发，无需通知"。Worker 拿到 `None` 就知道跳过回调。

```python
async def _timer(self, queue: asyncio.Queue, interval_min: int, name: str) -> None:
    print(f"[Timer/{name}] every {interval_min} min")
    while not self._shutdown_event.is_set():
        await self._sleep_or_shutdown(interval_min * 60)
        if not self._shutdown_event.is_set():
            await queue.put(None)   # ← 只唤醒，不传递回调
```

#### 3.1.3 Worker 改造

Worker 从 queue 取 item（`None` 或 callable），直接传给 `_try_run_job`。

```python
async def _worker(self, name: str, queue: asyncio.Queue, job) -> None:
    print(f"[Worker/{name}] ready")
    while not self._shutdown_event.is_set():
        callback = await self._wait_queue(queue)
        if callback is None and self._shutdown_event.is_set():
            break
        await self._try_run_job(name, job, callback)

async def _wait_queue(self, queue: asyncio.Queue):
    """阻塞等待 queue 中有数据，或 shutdown 事件触发。
    返回 queue item（None 或 callable），shutdown 时返回 None。
    """
    get_task = asyncio.create_task(queue.get())
    shut_task = asyncio.create_task(self._shutdown_event.wait())
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

#### 3.1.4 Trigger 端点（app.py）

闭包在 app.py 中创建，作为 queue item 放入 channel。通知标题、类别、SSE 推送全部在闭包中封装，daemon 完全无知。

```
用户点击虫子按钮
        │
        ▼
POST /api/trigger/crawl
        │
        ├─► notif = _add_notification(0, "新闻抓取", "pending", category="crawl")
        │       │ 通知内容在 app.py 定义：
        │       │   - title: "新闻抓取", category: "crawl", status: "pending"
        │       └─► _push_sse_event({"type":"new", "notification": notif})
        │
        ├─► 创建闭包（通知完成/失败逻辑在此封装）:
        │       def on_complete(success: bool, summary: str):
        │           """捕获 notif、_push_sse_event"""
        │           with _notification_lock:
        │               notif["status"] = "completed" if success else "failed"
        │               notif["summary"] = summary
        │           _push_sse_event({"type": "update", "notification": dict(notif)})
        │
        ├─► await queues["crawl"].put(on_complete)  # 闭包即消息
        │       │
        │       │  Worker 被唤醒 → 拿到 on_complete
        │       │  → 执行 job → 拿到 result dict
        │       │  → _try_run_job 调用 callback(True, result["summary"])
        │       │  → 闭包更新 notif → SSE 推送 → 前端 toast
        │       │  → 闭包执行后自然丢弃，无需手动清除
        │
        └─► 返回 {ok: true, notif_id: N}


定时器流程:
  Timer → await queue.put(None)
        → Worker 取到 callback=None
        → _try_run_job(name, job, None)
        → job 完成后 if callback is None: pass
        → 不创建通知，行为不变
```

#### 3.1.5 接口契约

```python
# === Queue item 类型（channel 中传递的数据） ===
QueueItem = Callable[[bool, str], None] | None
# Callable = app.py 创建的闭包，捕获了 notif + _push_sse_event
# None      = 定时器触发，Worker 跳过通知

# === 回调签名 — daemon → web 的唯一通信协议 ===
# callback(success: bool, summary: str) -> None

# === Job 返回值 ===
# 所有 job 函数返回 dict:
#   {"success": True, "summary": "抓取完成，共 23 条新闻", "count": 23}
#   {"success": True, "summary": "Cloud not configured — skipping.", "count": 0}
#   失败则抛异常 → _try_run_job 捕获 → callback(False, str(e)[:500])

# daemon 完全不知道：_add_notification、通知 dict 结构、SSE 推送
# daemon 只知道：从 queue 取 item，job 完成后如果是 callable 就调用之
```

### 3.2 Job 返回值改造

#### 3.2.1 `_crawl_job`

```python
async def _crawl_job(self) -> dict:
    crawler = Crawler(self.config, pg_db=self.db)
    result = await self._run_in_thread(
        crawler.fetch_all, OutputStyle.POSTGRESQL, True, True
    )
    # result: {"total": 23, "hotlist": 15, "rss": 8}
    total = result.get("total", 0) if result else 0
    return {
        "success": True,
        "summary": f"抓取完成，共 {total} 条新闻" if total > 0 else "抓取完成，无新新闻",
        "count": total,
    }
```

#### 3.2.2 `_sync_job`

```python
async def _sync_job(self) -> dict:
    cloud_config = self.config["storage"]["cloud"]
    if not (cloud_config.get("bucket_name") and cloud_config.get("endpoint_url")):
        return {"success": True, "summary": "云端未配置 — 已跳过同步", "count": 0}
    crawler = Crawler(self.config, pg_db=self.db)
    result = await self._run_in_thread(crawler.sync_from_cloud)
    # result: {"upserted": 5, "skipped": 0, "days": 3}
    total = result.get("upserted", 0) if result else 0
    return {
        "success": True,
        "summary": f"同步完成，新增 {total} 条" if total > 0 else "同步完成，无新数据",
        "count": total,
    }
```

#### 3.2.3 `fetch_all` 返回值

当前签名 `-> None`，改为 `-> dict`（向后兼容，无调用者依赖返回值）：

```python
def fetch_all(self, ...) -> dict:
    ...
    print(f"=== Fetch complete: {len(all_items)} items ===")
    return {
        "total": len(all_items),
        "hotlist": len([i for i in all_items if i.get("source_type") == "hotlist"]),
        "rss": len([i for i in all_items if i.get("source_type") == "rss"]),
    }
```

#### 3.2.4 `sync_from_cloud` 返回值

当前签名 `-> None`，改为 `-> dict`：

```python
def sync_from_cloud(self) -> dict:
    ...
    print(f"\n[Sync] Complete: {total_new} upserted, {total_skipped} skipped...")
    return {
        "upserted": total_new,
        "skipped": total_skipped,
        "days": len(db_keys),
    }
```

#### 3.2.5 `_try_run_job` 改造

Worker 从 queue 拿到的 callback 直接传入，不再查 dict。callback 为 `None` 时跳过通知。

```python
async def _try_run_job(self, name: str, job, callback=None) -> None:
    """Execute *job*. If *callback* is not None, call it with result.

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

### 3.3 SSE 实时推送

#### 3.3.1 后端端点

新增 `GET /api/notifications/stream`：

```python
import json
import asyncio

# Module-level SSE state (in web/app.py)
_sse_clients: set[asyncio.Queue] = set()
_sse_event_loop: asyncio.AbstractEventLoop | None = None


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


@app.get("/api/notifications/stream")
async def notification_stream(request: Request):
    """SSE endpoint — pushes new/updated notifications to the client."""
    from starlette.responses import StreamingResponse

    # Store event loop reference for thread-safe access
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
            "X-Accel-Buffering": "no",    # 兼容 nginx 反向代理
        },
    )
```

#### 3.3.2 推送时机

| 事件 | SSE type | 触发位置 |
|------|----------|---------|
| 创建新通知（pending） | `"new"` | `_add_notification` 调用后 |
| 通知状态变更（completed/failed） | `"update"` | `on_complete` 回调 / `_run_fetch_url` / `_run_refetch` 中 |
| 通知标记已读 | 暂不推送（前端本地处理） | — |

#### 3.3.3 线程安全分析

```
调用路径 1（主线程 / async context）:
  POST /api/trigger/crawl → _add_notification() → _push_sse_event()
  → asyncio.get_running_loop() 返回 event loop
  → 直接调用 _put() ✓

调用路径 2（线程池线程）:
  _refetch_executor.submit(_run_fetch_url) → notif["status"] = "completed"
  → _push_sse_event()
  → asyncio.get_running_loop() 抛出 RuntimeError
  → loop.call_soon_threadsafe(_put) ✓

调用路径 3（daemon 线程池）:
  _crawl_job → result → callback → _push_sse_event()
  → 同路径 2，call_soon_threadsafe ✓
```

### 3.4 通知结构扩展

#### 3.4.1 新字段

在现有通知 dict 中新增两个字段：

```python
notif = {
    "id": _notification_counter,       # int — 自增 ID（不变）
    "category": "fetch",               # str — "fetch" | "crawl" | "sync"（新增）
    "article_id": article_id,          # int — 文章 ID，crawl/sync 为 0（不变）
    "title": title,                    # str — 通知标题（语义不变，值变化）
    "summary": "",                     # str — 摘要信息（新增，如 "新增 23 条新闻"）
    "status": "pending",              # str — "pending"|"running"|"completed"|"failed"（不变）
    "error_message": error_message,    # str — 失败时的错误信息（不变）
    "is_read": False,                 # bool — 已读标记（不变）
    "created_at": _now(),             # float — 时间戳（不变）
}
```

#### 3.4.2 各类通知示例

```python
# Fetch 通知（文章抓取）
{
    "category": "fetch",
    "article_id": 123,
    "title": "央行宣布降准0.5个百分点",
    "summary": "",
    "status": "completed",
    ...
}

# Crawl 通知（手动抓取）
{
    "category": "crawl",
    "article_id": 0,
    "title": "新闻抓取",          # 显示名
    "summary": "抓取完成，共 23 条新闻",
    "status": "completed",
    ...
}

# Sync 通知（云端同步）
{
    "category": "sync",
    "article_id": 0,
    "title": "云端同步",
    "summary": "同步完成，新增 5 条",
    "status": "completed",
    ...
}
```

#### 3.4.3 `_add_notification` 签名

```python
def _add_notification(
    article_id: int,
    title: str,
    status: str = "pending",
    error_message: str = "",
    category: str = "fetch",    # 新增参数，默认 "fetch" 保持向后兼容
    summary: str = "",           # 新增参数
) -> dict:
```

### 3.5 前端改造

#### 3.5.1 轮询 → SSE

**删除：** `setInterval(fetchUnreadCount, POLL_INTERVAL)` 整个轮询机制
**删除：** `fetchUnreadCount()` 函数
**新增：** `EventSource` 长连接

```javascript
// ── SSE connection (replaces polling) ──
var es = new EventSource('/api/notifications/stream');

es.onopen = function() {
  console.log('[SSE] connected');
  // 初始抓取一次未读计数和通知列表
  fetch('/api/notifications/unread-count')
    .then(function(r) { return r.json(); })
    .then(function(d) { updateBadge(d.count || 0); });
};

es.onerror = function() {
  // EventSource 会自动重连，4s 后重试
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
  var path = window.location.pathname;
  if (path.indexOf('/hot-news') !== 0 && path.indexOf('/news/') !== 0) return;

  // 根据 category 显示不同 toast
  if (notif.category === 'crawl' || notif.category === 'sync') {
    // 任务触发 — 显示 pending toast
    showAppToast(
      notif.title,
      notif.summary || '任务已触发，正在执行…',
      'info'
    );
  } else {
    // Fetch — 按原逻辑处理
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

  var path = window.location.pathname;
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
```

#### 3.5.2 删除轮询后的按需 fetch

删除 `setInterval(fetchUnreadCount, POLL_INTERVAL)` 整个轮询机制后，仍有两次**按需** HTTP 请求（非轮询）：

1. **SSE `onopen` 时获取初始未读计数** — 用于 SSE 重连后同步红点状态（服务重启后内存通知清空，badge 需要重新同步）
2. **`fetchNotifications(forDrawer)`** — 打开抽屉时拉取完整通知列表来渲染，按需调用，不是定时轮询

新增的 `mark-all-read`（打开抽屉时调用）进一步减少了单条标记的需求。

#### 3.5.3 `renderDrawerList` 改造

区分 `category` 渲染不同内容和状态文案：

```javascript
function renderDrawerList(data) {
  var list = document.getElementById('notify-list');
  if (!list) return;
  if (data.length === 0) {
    list.innerHTML = '...';  // 空状态不变
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

function getStatusText(category, status) {
  if (category === 'crawl' || category === 'sync') {
    if (status === 'pending')  return '排队中';
    if (status === 'running')  return '执行中';
    if (status === 'completed') return '已完成';
    if (status === 'failed')   return '执行失败';
  }
  // 原有文章抓取状态文案
  if (status === 'pending')  return '等待抓取';
  if (status === 'running')  return '抓取中';
  if (status === 'completed') return '抓取成功';
  if (status === 'failed')   return '抓取失败';
  return status;
}
```

#### 3.5.4 `markAndGo` 改造

```javascript
window.markAndGo = function(notifId, articleId, status, category) {
  fetch('/api/notifications/' + notifId + '/read', { method: 'POST' });
  // 只有文章抓取类的完成通知才跳转
  if (status === 'completed' && articleId > 0 && category !== 'crawl' && category !== 'sync') {
    window.location.href = '/news/' + articleId;
  }
  // crawl/sync 通知：关闭抽屉即可
  closeDrawer();
};
```

#### 3.5.5 `showToast` 改造

```javascript
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
```

#### 3.5.6 已读/未读管理

**问题：** SSE 推送 toast 后，用户已看到通知内容，但通知仍为"未读"状态，铃铛红点不消除。用户必须打开抽屉逐条点击才能消除，对 crawl/sync 这类无可跳转文章的任务通知尤为繁琐。

**方案：打开抽屉即全部已读。** 用户拉开抽屉查看通知列表，就表示已经消费了所有通知。

```javascript
// toggleDrawer 中 — 打开抽屉时标记全部已读
function toggleDrawer(event) {
  event && event.stopPropagation();
  var drawer = document.getElementById('notify-drawer');
  var isOpen = drawer.classList.toggle('is-open');

  if (isOpen) {
    fetchNotifications(true);
    // 打开抽屉 = 全部已读
    fetch('/api/notifications/mark-all-read', { method: 'POST' })
      .then(function() { updateBadge(0); });
  }
}
```

**保留 `markAndGo` 中的单条已读标记** — 兼容用户不打开抽屉、直接点 toast 跳转的场景（文章类通知）：

```javascript
window.markAndGo = function(notifId, articleId, status, category) {
  fetch('/api/notifications/' + notifId + '/read', { method: 'POST' });
  // 只有文章抓取完成才跳转；crawl/sync 不跳转
  if (status === 'completed' && articleId > 0 && category !== 'crawl' && category !== 'sync') {
    window.location.href = '/news/' + articleId;
  }
  closeDrawer();
};
```

**后端新增端点：**

```python
@app.post("/api/notifications/mark-all-read")
def mark_all_read():
    """Mark all notifications as read."""
    with _notification_lock:
        for n in _notifications:
            n["is_read"] = True
    return {"ok": True}
```

---

## 4. 涉及文件

| 文件 | 变更性质 | 行数估算 |
|------|---------|---------|
| `main.py` | `asyncio.Event` → `asyncio.Queue`；改造 `_timer`、`_worker`、`_wait_queue`、`_try_run_job`、`_crawl_job`、`_sync_job`、`run` | +35 / -15 |
| `web/app.py` | 新增 SSE endpoint、`_push_sse_event`、`mark-all-read` 端点；trigger API 改为 `queue.put(on_complete)`；修改 `_add_notification` 签名 | +95 / -10 |
| `news/crawler.py` | 修改 `fetch_all`、`sync_from_cloud` 返回值 | +15 / -2 |
| `web/templates/base.html` | 替换轮询为 SSE、改造 `renderDrawerList`、`markAndGo`、`showToast`、`toggleDrawer`（打开即全部已读） | +105 / -40 |
| `tests/` | 新增 SSE 测试、Queue callback 测试 | 新建文件 |

---

## 5. 数据流总览

```
┌──────────────────────────────────────────────────────────────────────────┐
│  用户点击虫子 🐛                                                          │
│       │                                                                    │
│       ▼                                                                    │
│  POST /api/trigger/crawl                                                  │
│       │                                                                    │
│       ├──► notif = _add_notification(0, "新闻抓取", "pending",            │
│       │                               category="crawl")                   │
│       │        │                                                           │
│       │        └──► _push_sse_event({"type":"new", "notification":notif}) │
│       │                 → 浏览器 SSE → 铃铛红点 +1                         │
│       │                                                                    │
│       ├──► def on_complete(success, summary):  # 闭包捕获 notif           │
│       │        notif["status"] = "completed" if success else "failed"     │
│       │        notif["summary"] = summary                                 │
│       │        _push_sse_event({"type":"update", ...})                    │
│       │                                                                    │
│       └──► await queues["crawl"].put(on_complete)  # 闭包放入 channel     │
│                                                                           │
│  Worker: callback = await queue.get()    # 阻塞等待，拿到 on_complete     │
│       │                                                                    │
│       ▼                                                                    │
│  _crawl_job() → crawler.fetch_all() → {"success": True,                   │
│       "summary": "抓取完成，共 23 条新闻", "count": 23}                    │
│       │                                                                    │
│       ▼                                                                    │
│  _try_run_job(name, job, callback)                                         │
│       │                                                                    │
│       └──► callback(True, "抓取完成，共 23 条新闻")                        │
│                 │                                                          │
│                 ├──► notif["status"] = "completed"                         │
│                 ├──► notif["summary"] = "抓取完成，共 23 条新闻"           │
│                 └──► _push_sse_event({"type":"update", ...})              │
│                          → 浏览器 SSE → toast "新闻抓取 完成"             │
│                          → 抽屉内列表自动刷新（如已打开）                   │
│                                                                           │
│  ── 定时器触发 ──                                                         │
│  Timer → await queue.put(None)                                            │
│  Worker → callback = None                                                 │
│  _try_run_job(name, job, None) → 跳过通知 → 行为不变                      │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 6. 错误处理

| 场景 | 处理方式 |
|------|---------|
| Queue item 为 None（定时器触发） | `_try_run_job` 跳过通知，只 print 日志 |
| Job 抛出异常 | `_try_run_job` 捕获 → `callback(False, str(e)[:500])` → 前端显示失败 toast |
| SSE 客户端断连 | `EventSource` 自动重连（浏览器默认 4s 间隔） |
| SSE 队列满 | `put_nowait` 捕获 `QueueFull`，静默丢弃（客户端消费太慢） |
| SSE 循环引用丢失 | `_sse_event_loop` 初始化为 `None`，首次连接时赋值；重启后重置 |
| 用户快速双击按钮 | 两次 `queue.put(closure)` — queue 中积压两个闭包，各引用各自的 notif；第二个 job 拿到第二个闭包，更新第二个 notif；第一个闭包对应的 notif 将永远停留在 pending 状态 |
| Queue 中积压未消费的闭包 | 服务重启后 queue 重建，积压的旧闭包随进程消亡 — 通知停留在 pending 是预期行为（重启=中断） |
| 服务重启 | 通知为内存态，重启后清空 — 和当前行为一致 |

---

## 7. 兼容性

- **现有 fetch/refetch 通知不受影响** — `category` 默认 `"fetch"`，`summary` 默认为空
- **`/api/notifications` 响应新增字段** — 前端需处理未知字段（JS 中访问不存在的属性返回 `undefined`，无需任何处理）
- **`/api/notifications/unread-count` 不变**
- **`POST /api/notifications/{id}/read` 不变** — 保留单条已读标记，兼容 toast 点击跳转场景
- **新增 `POST /api/notifications/mark-all-read`** — 打开抽屉时批量标记已读
- **`POST /api/trigger/{crawl,sync}` 响应新增 `notif_id` 字段** — 前端可选使用
- **`fetch_all` / `sync_from_cloud` 从 `-> None` 改为 `-> dict`** — 无调用者依赖返回值
- **`create_app(signals=...)` → `create_app(queues=...)`** — 仅 daemon 侧 `main.py` 调用，不影响外部
- **`asyncio.Event` → `asyncio.Queue`** — Timer/Worker 接口变化，但行为语义不变：定时触发=无通知，手动触发=有通知
