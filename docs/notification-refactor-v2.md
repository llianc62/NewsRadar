# Notification + BackgroundTask Refactor (v2)

## 动机

当前通知系统和 refetch 状态分散在三个模块的模块级全局变量中：

| 模块 | 全局变量 | 问题 |
|------|---------|------|
| `web/notification.py` | `_notifications`, `_notification_counter`, `_notification_lock`, `_sse_clients`, `_sse_clients_lock`, `_sse_event_loop` | 模块级全局，测试难以 mock，生命周期模糊 |
| `web/news.py` | `_refetch_tasks`, `_refetch_executor` | 同上 |
| `web/app.py` | `_news_mod._refetch_executor = ...` | 跨模块直接修改私有变量，隐式耦合 |

此外，`notification._notification_lock` 被 `news.py` 拿来保护 refetch 数据（`_refetch_tasks`），属于**耦合泄露**——两个不同的关注点共用了同一个锁。

## 方案

封装成两个独立的 state 类，由 `app.py` 创建实例挂到 `app.state`，所有路由通过 `request.app.state` 访问——跟 `db`、`crawler` 完全一致的模式。

### 1. `NotificationState` 类

```python
# web/notification.py

class NotificationState:
    """Notification list + SSE broadcast state.

    Single responsibility.  No business dependencies.
    """

    def __init__(self) -> None:
        self._notifications: list[dict] = []
        self._counter: int = 0
        self._lock = threading.Lock()
        self._sse_clients: set[asyncio.Queue] = set()
        self._sse_clients_lock = threading.Lock()
        self._sse_event_loop: asyncio.AbstractEventLoop | None = None

    # ── 公开 API ──

    def add_notification(
        self, scope: str, article_id: int, title: str,
        status: str = "pending", error_message: str = "",
        category: str = "fetch", summary: str = "",
    ) -> dict: ...

    def push_sse_event(self, data: dict) -> None: ...
    def register_client(self, queue: asyncio.Queue) -> None: ...
    def unregister_client(self, queue: asyncio.Queue) -> None: ...
    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None: ...

    # 替代外部直接访问 _notifications + _lock 的查询方法
    def get_notifications(self, scope=None, unread_only=False) -> list[dict]: ...
    def get_unread_count(self, scope=None) -> int: ...
    def mark_read(self, notif_id: int) -> bool: ...
    def mark_all_read(self) -> None: ...
```

**关键设计点：**
- 所有锁都封装在类内部，外部不再 import `_notification_lock`
- `_notifications` 不再通过 `with notification._notification_lock: notification._notifications` 直接访问
- `_sse_event_loop` 通过 `set_event_loop()` 设置，而非直接赋值

### 2. `BackgroundTaskRunner` —— 通用后台任务执行器

```python
# web/background.py

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

class BackgroundTaskRunner:
    """Generic background task executor.

    Accepts any callable + arguments, runs in a ThreadPoolExecutor.
    Provides dedup (by task_id), status tracking, and lifecycle management.

    Usage::

        runner = BackgroundTaskRunner(max_workers=10)
        runner.submit("refetch-42", _run_refetch, 42, crawler, db, notif)
        status = runner.get_status("refetch-42")  # {"status": "running", ...}
        runner.shutdown()
    """

    def __init__(self, max_workers: int = 10) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._tasks: dict[str, dict] = {}  # task_id -> {status, started_at, ...}
        self._lock = threading.Lock()

    # ── 公开 API ──

    def submit(self, task_id: str, func: Callable, *args: Any, **kwargs: Any) -> dict:
        """Submit a background task.

        *task_id* is used for dedup — if a task with the same ID is already
        pending or running, it won't be submitted again.

        Returns a status dict ``{status, task_id, error?}``.
        """
        with self._lock:
            existing = self._tasks.get(task_id)
            if existing and existing["status"] in ("pending", "running"):
                return {"status": "duplicate", "task_id": task_id,
                        "error": "任务已在执行中"}

            entry = {
                "task_id": task_id,
                "status": "pending",
                "started_at": None,
                "finished_at": None,
                "error": None,
            }
            self._tasks[task_id] = entry

        self._executor.submit(self._run, task_id, func, args, kwargs)
        return {"status": "submitted", "task_id": task_id}

    def get_status(self, task_id: str) -> dict | None:
        """Return task status dict, or None if task_id unknown."""
        with self._lock:
            entry = self._tasks.get(task_id)
            return dict(entry) if entry else None

    def remove(self, task_id: str) -> None:
        """Remove a task from tracking (completed, cancelled, or cleanup)."""
        with self._lock:
            self._tasks.pop(task_id, None)

    def shutdown(self, wait: bool = False) -> None:
        """Shutdown the executor and clear all task state."""
        self._executor.shutdown(wait=wait)
        self._tasks.clear()

    # ── 内部 ──

    def _run(self, task_id: str, func: Callable, args: tuple, kwargs: dict) -> None:
        """Execute func in executor thread and update task status."""
        with self._lock:
            entry = self._tasks.get(task_id)
            if entry is None:
                return  # already removed
            entry["status"] = "running"
            entry["started_at"] = time.time()

        try:
            func(*args, **kwargs)
        except Exception as e:
            with self._lock:
                entry = self._tasks.get(task_id)
                if entry:
                    entry["status"] = "failed"
                    entry["error"] = str(e)[:500]
                    entry["finished_at"] = time.time()
            return

        with self._lock:
            entry = self._tasks.get(task_id)
            if entry:
                entry["status"] = "completed"
                entry["finished_at"] = time.time()
```

**设计要点：**

- **纯通用** — 不依赖任何业务模块（crawler、db、notification）。后续任何耗时操作都可以复用。
- **dedup** — `task_id` 做去重，同名 pending/running 任务不会重复提交。
- **状态跟踪** — 通过 `get_status(task_id)` 查询，路由 handler 不再需要自己维护 task dict。
- **无业务逻辑** — 不包含 fetch/refetch 的具体实现。

### 3. fetch/refetch 逻辑仍留在 `news.py`

```python
# web/news.py — 普通函数，不在类里

def _run_fetch_url(url: str, crawler, notif: dict, db, ns: NotificationState) -> None:
    """Run in executor thread.  Updates notif via ns.push_sse_event."""
    try:
        notif["status"] = "running"
        ns.push_sse_event({"type": "update", "notification": dict(notif)})
        crawler.fetch(url, 1, True, True)
        notif["status"] = "completed"
        article = db.get_article_by_url(url)
        if article:
            notif["article_id"] = article["id"]
    except Exception as e:
        notif["status"] = "failed"
        notif["error_message"] = str(e)[:500]
    finally:
        ns.push_sse_event({"type": "update", "notification": dict(notif)})


def _run_refetch(article_id: int, crawler, notif: dict, db, ns: NotificationState) -> None:
    """Run in executor thread."""
    try:
        notif["status"] = "running"
        ns.push_sse_event({"type": "update", "notification": dict(notif)})
        article = db.get_news_by_id(article_id)
        if article is None:
            raise ValueError("文章不存在")
        article["content"] = ""
        crawler.enrich_content(article, with_image=True)
        db.update_article_full(article_id, ...)
        notif["status"] = "completed"
    except Exception as e:
        notif["status"] = "failed"
        notif["error_message"] = str(e)[:500]
    finally:
        ns.push_sse_event({"type": "update", "notification": dict(notif)})
```

### 4. 路由 handler 的使用

```python
@router.post("/api/news/fetch")
async def fetch_news_by_url(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "URL 不能为空"}, 400)

    db = request.app.state.db
    crawler = request.app.state.crawler
    runner = request.app.state.background_runner
    ns = request.app.state.notification_state

    if runner is None:
        return JSONResponse({"ok": False, "error": "后台任务未就绪"}, 503)

    existing = db.get_article_by_url(url)
    if existing:
        article_id = existing["id"]
        # 用 runner 的 dedup 代替之前的 _refetch_tasks 手动检查
        status = runner.get_status(f"refetch-{article_id}")
        if status and status["status"] in ("pending", "running"):
            return {"ok": False, "error": "该文章正在抓取中"}

        title = existing.get("title") or url
        notif = ns.add_notification(...)
        runner.submit(f"refetch-{article_id}", _run_refetch,
                      article_id, crawler, notif, db, ns)
        return {"ok": True, "refetch": True, "article_id": article_id}

    notif = ns.add_notification(scope="news", article_id=0, title=url, status="pending")
    runner.submit(f"fetch-{int(time.time())}", _run_fetch_url,
                  url, crawler, notif, db, ns)
    return {"ok": True, "message": "已提交抓取任务"}


@router.post("/api/news/{article_id}/refetch")
async def refetch_article(request: Request, article_id: int):
    runner = request.app.state.background_runner
    ns = request.app.state.notification_state
    db = request.app.state.db

    # ... validate article exists ...

    status = runner.get_status(f"refetch-{article_id}")
    if status and status["status"] in ("pending", "running"):
        return {"ok": False, "error": "该文章正在抓取中"}

    notif = ns.add_notification(...)
    runner.submit(f"refetch-{article_id}", _run_refetch,
                  article_id, crawler, notif, db, ns)
    return {"ok": True, "task": notif}


@router.delete("/api/news/{article_id}")
async def delete_article(request: Request, article_id: int):
    # ...
    runner = request.app.state.background_runner
    if runner:
        runner.remove(f"refetch-{article_id}")
    return {"ok": True}
```

### 5. `app.py` 启动

```python
from web.notification import NotificationState
from web.background import BackgroundTaskRunner

def create_app(...):
    app = FastAPI(...)
    app.state.notification_state = NotificationState()
    app.state.background_runner = BackgroundTaskRunner(max_workers=10) if crawler else None
    ...
```

不再有 `import web.news as _news_mod; _news_mod._refetch_executor = ...`。

## 涉及修改点

| 文件 | 操作 | 说明 |
|------|------|------|
| `web/notification.py` | **重写** | 所有状态 + 函数 → `NotificationState` 类。导出类 + 所有公开方法。不再导出模块级全局变量。 |
| `web/background.py` | **新建** | `BackgroundTaskRunner` 通用后台任务执行器。纯通用，无业务依赖。 |
| `web/news.py` | **修改** | 移除 `_refetch_tasks` / `_refetch_executor` / `_run_fetch_url` / `_run_refetch` 仍在 `news.py` 作为普通函数。路由 handler 走 `request.app.state.background_runner` / `notification_state`。通知相关接口走 `request.app.state.notification_state`。 |
| `web/app.py` | **修改** | 创建 `NotificationState()` + `BackgroundTaskRunner()` 挂到 `app.state`。移除 `import web.news as _news_mod` hack。 |
| `tests/test_task_notification.py` | **修改** | 改用 `NotificationState()` 实例 |
| `tests/test_notification_frontend.py` | **修改** | 同上 |
| `tests/test_delete.py` | **修改** | 适配 `BackgroundTaskRunner.get_status()` / `remove()` |
| `tests/test_refetch.py` | **修改** | 适配 `BackgroundTaskRunner.submit()` / `get_status()` |

## 不改的部分

- SSE 端点 `notification_stream()` 逻辑不变（只是访问路径变 `app.state.notification_state`）
- 前端 `notification.js` / `notification.html` / `base.html` 完全不变
- Notification 的 `scope` 设计、toast/drawer/badge 逻辑不变
- `_run_fetch_url` / `_run_refetch` 函数体不变，只从模块级移入 `news.py` 作为普通函数

## 风险 & 边界情况

1. **后台线程访问 `notification_state`** — `BackgroundTaskRunner` 不持有 `NotificationState`，由业务函数 `_run_refetch` 通过参数接收实例，直接调用 `.push_sse_event()`（线程安全）
2. **`_sse_event_loop` 设置时机** — 第一个 SSE 连接时设，仍在 `notification_stream()` 中通过 `request.app.state.notification_state.set_event_loop(loop)` 设置
3. **测试隔离** — 每个测试创建一个新的 `NotificationState()` / `BackgroundTaskRunner()` 实例，不再有模块级状态残留
4. **`task_id` 命名约定** — 用 `"refetch-{article_id}"` / `"fetch-{timestamp}"` 格式，确保唯一性且能通过 task_id 做 dedup
