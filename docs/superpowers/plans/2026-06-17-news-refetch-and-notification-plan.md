# 新闻重新抓取 + 后台消息通知 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在新闻详情页增加后台重新抓取功能，完成后通过 Toast + 铃铛通知用户。

**Architecture:** 后端在 web/app.py 模块级用 dict 管理任务状态和通知队列，通过 ThreadPoolExecutor 异步执行抓取。前端通过轮询 API 获取通知更新，铃铛 badge + Toast + 右侧抽屉展示。

**Tech Stack:** FastAPI + Jinja2 SSR + vanilla CSS/JS + PostgreSQL + ThreadPoolExecutor

## Global Constraints

- 通知纯内存管理，不持久化到数据库
- max_workers=10, max_notifications=50 硬编码
- 无 WebSocket/SSE，前端 5 秒轮询
- 无"全部已读"功能
- 工具栏仅刷新按钮

---

## 文件结构

| 文件 | 变更 | 职责 |
|------|------|------|
| `web/app.py` | 修改 | 模块级状态管理 + 4 个 API 端点 + bell SVG 图标 |
| `web/static/css/app.css` | 修改 | 追加 Toolbar/Toast/Bell/Drawer 样式 |
| `web/templates/pages/news_detail.html` | 修改 | 增加右侧工具栏 HTML |
| `web/templates/pages/hot_news.html` | 修改 | 增加铃铛图标 + 通知抽屉 HTML |
| `web/templates/base.html` | 修改 | 增加轮询 JS（scripts block） |
| `main.py` | 修改 | 创建 Crawler 实例，传入 create_app |
| `tests/test_refetch.py` | **新增** | API 端点集成测试 |

---

### Task 1: CSS — 新增所有组件样式

**Files:**
- Modify: `web/static/css/app.css` — 在文件末尾追加

**Interfaces:**
- Produces: CSS classes `.tool-bar`, `.tool-btn`, `.toast-container`, `.toast`, `.toast-body`, `.toast-title`, `.toast-sub`, `.bell-btn-wrap`, `.bell-btn`, `.bell-badge`, `.notify-overlay`, `.notify-drawer`, `.notify-drawer-list`, `.notify-empty`, `.notify-item`, `.notify-item-dot`, `.notify-item-body`, `.notify-item-title`, `.notify-item-meta`, `.notify-item-status`, `.notify-item-time`

- [ ] **Step 1: 追加 CSS 到 app.css 末尾**

在 `web/static/css/app.css` 末尾追加以下内容：

```css
/* ═══════════════════════════════════════════
   TOOL BAR — right-side fixed, dynamic gap
   ═══════════════════════════════════════════ */
.tool-bar {
  position: fixed;
  right: max(24px, calc((100vw - var(--sidebar-collapsed) - 700px) / 4));
  top: 50%;
  transform: translateY(-50%);
  z-index: 90;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 4px;
  background: var(--surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
}

.tool-btn {
  display: flex; align-items: center; justify-content: center;
  width: 32px; height: 32px;
  border: none; border-radius: var(--radius-sm);
  background: transparent; color: var(--text-muted);
  cursor: pointer; padding: 0;
  transition: color var(--duration-fast), background var(--duration-fast);
}

.tool-btn:hover { color: var(--accent); background: var(--accent-light); }
.tool-btn svg { width: 16px; height: 16px; }

/* ═══════════════════════════════════════════
   TOAST — top-right, orange accent
   ═══════════════════════════════════════════ */
.toast-container {
  position: fixed; top: 16px; right: 16px;
  z-index: 300;
  display: flex; flex-direction: column; gap: 8px;
  pointer-events: none;
}

.toast {
  display: flex; align-items: center;
  padding: 14px 18px;
  background: var(--surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius);
  box-shadow: var(--shadow-lg);
  font-size: 13px; color: var(--text);
  max-width: 340px;
  pointer-events: auto; cursor: pointer;
  animation: toastIn 0.4s var(--ease-out-back);
  transition: opacity var(--duration-normal), transform var(--duration-normal),
              border-color var(--duration-fast), box-shadow var(--duration-fast);
}

.toast:hover {
  border-color: var(--accent);
  box-shadow: var(--shadow-lg), 0 0 0 1px var(--accent-glow);
}

.toast.fading { opacity: 0; transform: translateX(24px); }

.toast-body { flex: 1; min-width: 0; }
.toast-title {
  font-size: 13px; font-weight: 600; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  display: flex; align-items: center; gap: 8px;
}
.toast-title .dot {
  width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
}
.toast.done .dot { background: var(--accent); }
.toast.fail .dot { background: var(--text-muted); }
.toast-sub { font-size: 11px; color: var(--text-muted); margin-top: 3px; padding-left: 14px; }

@keyframes toastIn {
  from { opacity: 0; transform: translateX(24px) scale(0.95); }
  to   { opacity: 1; transform: translateX(0) scale(1); }
}

/* ═══════════════════════════════════════════
   BELL — notification trigger
   ═══════════════════════════════════════════ */
.bell-btn-wrap { position: relative; margin-left: 8px; }

.bell-btn {
  display: flex; align-items: center; justify-content: center;
  width: 40px; height: 40px;
  border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--surface); cursor: pointer;
  color: var(--text-muted); padding: 0;
  transition: all var(--duration-fast); box-shadow: var(--shadow-xs);
}

.bell-btn:hover { border-color: var(--accent); color: var(--accent); box-shadow: var(--shadow-sm); }
.bell-btn svg { width: 18px; height: 18px; }

.bell-badge {
  position: absolute; top: -5px; right: -5px;
  min-width: 18px; height: 18px; padding: 0 5px;
  border-radius: 9px;
  background: var(--red); color: #fff;
  font-size: 10px; font-weight: 700;
  line-height: 18px; text-align: center;
  box-shadow: 0 0 0 2px var(--surface);
  animation: badgePop 0.3s var(--ease-out-back);
}

.bell-badge[data-count="0"] { display: none; }

@keyframes badgePop {
  from { transform: scale(0); }
  to   { transform: scale(1); }
}

/* ═══════════════════════════════════════════
   NOTIFICATION DRAWER — right slide panel
   ═══════════════════════════════════════════ */
.notify-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.08);
  z-index: 250; opacity: 0; pointer-events: none;
  transition: opacity var(--duration-normal);
}

.notify-overlay.is-open { opacity: 1; pointer-events: auto; }

.notify-drawer {
  position: fixed; top: 0; right: 0; bottom: 0;
  width: 340px; max-width: calc(100vw - 24px);
  background: var(--surface);
  border-left: 1px solid var(--border);
  box-shadow: var(--shadow-xl);
  z-index: 251;
  transform: translateX(100%);
  transition: transform var(--duration-slow) var(--ease-out-expo);
  display: flex; flex-direction: column;
}

.notify-drawer.is-open { transform: translateX(0); }

.notify-drawer-list {
  flex: 1; overflow-y: auto;
  padding: 16px 16px;
}

.notify-drawer-list::-webkit-scrollbar { width: 4px; }
.notify-drawer-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }

.notify-empty {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; height: 100%;
  color: var(--text-muted); font-size: 13px; gap: 12px;
}

.notify-empty svg { width: 36px; height: 36px; opacity: 0.15; }

.notify-item {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 14px;
  border-radius: var(--radius);
  cursor: pointer;
  transition: background var(--duration-fast);
  text-decoration: none; color: inherit;
  position: relative;
}

.notify-item:hover { background: var(--accent-light); }

.notify-item-dot {
  width: 8px; height: 8px; border-radius: 50%;
  flex-shrink: 0; margin-top: 5px;
  box-shadow: 0 0 0 3px transparent;
  transition: box-shadow var(--duration-fast);
}

.notify-item-dot.done  { background: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
.notify-item-dot.fail  { background: var(--text-muted); }
.notify-item-dot.running { background: var(--amber); }

.notify-item-body { flex: 1; min-width: 0; }

.notify-item-title {
  font-size: 13px; font-weight: 500; color: var(--text);
  line-height: 1.5;
  overflow: hidden; text-overflow: ellipsis;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}

.notify-item-meta {
  display: flex; align-items: center; gap: 8px;
  margin-top: 6px; font-size: 11px;
}

.notify-item-status {
  font-size: 11px; font-weight: 600;
}

.notify-item-status.done  { color: var(--accent); }
.notify-item-status.fail  { color: var(--text-muted); }
.notify-item-status.running { color: var(--amber); }

.notify-item-time {
  color: var(--text-muted);
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
}
```

- [ ] **Step 2: 验证 CSS 无语法错误**

```bash
# 启动 app 后在浏览器中确认样式加载正常
python main.py &
sleep 2
curl -s http://localhost:8000/static/css/app.css | tail -20
```

- [ ] **Step 3: Commit**

```bash
git add web/static/css/app.css
git commit -m "feat: add toolbar, toast, bell, drawer CSS styles"
```

---

### Task 2: Backend — Refetch 状态管理 + API 端点

**Files:**
- Modify: `web/app.py` — 添加模块级状态、bell SVG 图标、4 个 API 端点，修改 `create_app()` 签名
- Create: `tests/test_refetch.py` — API 集成测试

**Interfaces:**
- Consumes: `Crawler` instance (传入 create_app), `PostgreSQL` (app.state.db)
- Produces:
  - `POST /api/news/{id}/refetch` → `{ok: bool, task?: dict, error?: str}`
  - `GET /api/notifications?unread_only=true` → `list[dict]`
  - `GET /api/notifications/unread-count` → `{count: int}`
  - `POST /api/notifications/{id}/read` → `{ok: bool}`

- [ ] **Step 1: 编写失败测试**

创建 `tests/test_refetch.py`：

```python
"""Tests for news refetch API endpoints."""
import pytest
from fastapi.testclient import TestClient
from web.app import create_app

# We'll use a mock crawler and mock db for testing
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_db():
    """Mock PostgreSQL with get_news_by_id returning a test article."""
    db = MagicMock()
    db.get_news_by_id.return_value = {
        "id": 1,
        "title": "测试新闻标题",
        "url": "https://example.com/news/1",
        "source_name": "测试来源",
        "content": "",
    }
    return db


@pytest.fixture
def mock_crawler():
    """Mock Crawler."""
    return MagicMock()


@pytest.fixture
def client(mock_db, mock_crawler):
    """Create test client with mock db and crawler."""
    s3_config = {
        "endpoint_url": "http://localhost:9000",
        "bucket_name": "test",
        "access_key_id": "test",
        "secret_access_key": "test",
        "region": "us-east-1",
    }
    app = create_app(mock_db, s3_config, crawler=mock_crawler)
    return TestClient(app)


class TestRefetchEndpoint:
    """Tests for POST /api/news/{id}/refetch."""

    def test_refetch_returns_ok_for_valid_article(self, client, mock_db):
        """Should accept refetch request for an article with a URL."""
        resp = client.post("/api/news/1/refetch")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "task" in data

    def test_refetch_rejects_duplicate(self, client):
        """Second refetch for same article should be rejected while running."""
        client.post("/api/news/1/refetch")
        resp = client.post("/api/news/1/refetch")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "正在抓取中" in data.get("error", "")

    def test_refetch_rejects_article_without_url(self, client, mock_db):
        """Article without URL should be rejected."""
        mock_db.get_news_by_id.return_value = {
            "id": 2,
            "title": "无链接新闻",
            "url": "",
            "source_name": "测试",
            "content": "",
        }
        resp = client.post("/api/news/2/refetch")
        data = resp.json()
        assert data["ok"] is False

    def test_refetch_returns_404_for_missing_article(self, client, mock_db):
        """Missing article should return 404."""
        mock_db.get_news_by_id.return_value = None
        resp = client.post("/api/news/999/refetch")
        assert resp.status_code == 404


class TestNotificationsEndpoint:
    """Tests for GET /api/notifications."""

    def test_notifications_returns_list(self, client):
        """Should return a list (empty initially)."""
        resp = client.get("/api/notifications")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_unread_count_returns_zero_initially(self, client):
        """Should return count 0 with no notifications."""
        resp = client.get("/api/notifications/unread-count")
        assert resp.status_code == 200
        assert resp.json() == {"count": 0}
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
python -m pytest tests/test_refetch.py -v
# Expected: FAIL — 404 or 500 because endpoints don't exist yet
```

- [ ] **Step 3: 添加 bell SVG 图标到 ICONS dict**

在 `web/app.py` 的 `ICONS` dict 中，`"x"` 图标后面追加：

```python
    # ── Notification ──
    "bell": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>',
    "refresh": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/></svg>',
    "check-circle": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20 6L9 17l-5-5"/></svg>',
    "x-circle": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>',
```

- [ ] **Step 4: 添加模块级状态和辅助函数**

在 `web/app.py` 的 `ICONS` dict 后面，`_md_renderer` 之前添加：

```python
# ── Refetch state (in-memory) ─────────────────────────────────────

import time
import threading
from concurrent.futures import ThreadPoolExecutor

_refetch_tasks: dict[int, dict] = {}       # key=article_id, 去重+状态跟踪
_notifications: list[dict] = []            # 通知列表，最多 50 条
_notification_counter: int = 0             # 自增 ID
_notification_lock = threading.Lock()      # 线程安全
_refetch_executor: ThreadPoolExecutor | None = None


def _now() -> float:
    return time.time()


def _add_notification(article_id: int, title: str, status: str = "pending",
                      error_message: str = "") -> dict:
    """Create a notification, append to list, return the dict."""
    global _notification_counter
    with _notification_lock:
        _notification_counter += 1
        notif = {
            "id": _notification_counter,
            "article_id": article_id,
            "title": title,
            "status": status,
            "error_message": error_message,
            "is_read": False,
            "created_at": _now(),
        }
        _notifications.insert(0, notif)
        # Cap at 50
        if len(_notifications) > 50:
            _notifications.pop()
        return notif


def _run_refetch(article_id: int, url: str, title: str, crawler,
                 db, notif: dict) -> None:
    """Execute refetch in background thread, update status on completion."""
    try:
        notif["status"] = "running"
        from news.crawler import OutputStyle
        crawler.fetch(
            url,
            output_style=OutputStyle.POSTGRESQL,
            with_content=True,
            with_image=True,
        )
        notif["status"] = "completed"
    except Exception as e:
        notif["status"] = "failed"
        notif["error_message"] = str(e)
    finally:
        # Remove from dedup dict so re-fetch is allowed again
        _refetch_tasks.pop(article_id, None)
```

- [ ] **Step 5: 修改 create_app 签名**

将 `web/app.py` 中 `create_app` 函数签名从：

```python
def create_app(db, s3_config: dict, signals: dict = None):
```

改为：

```python
def create_app(db, s3_config: dict, signals: dict = None, crawler=None):
```

并在 lifespan 中初始化 executor。在 `app.state.db = db` 之后添加：

```python
    app.state.signals = signals or {}

    # S3 storage — required for /media/ proxy
    app.state.s3_storage = S3Storage(s3_config)

    # Refetch state
    global _refetch_executor
    if _refetch_executor is None or crawler is not None:
        if _refetch_executor is not None:
            _refetch_executor.shutdown(wait=False)
        _refetch_executor = ThreadPoolExecutor(max_workers=10)
    app.state.crawler = crawler
```

- [ ] **Step 6: 添加 4 个 API 端点**

在 `create_app` 内部的 `trigger_sync` 端点之后、`return app` 之前添加：

```python
    # ── Refetch API ────────────────────────────────────────────

    @app.post("/api/news/{article_id}/refetch")
    async def refetch_article(article_id: int):
        """Submit a background refetch job for the given article."""
        article = db.get_news_by_id(article_id)
        if article is None:
            return JSONResponse({"ok": False, "error": "文章不存在"}, status_code=404)
        url = (article.get("url") or "").strip()
        title = article.get("title") or ""
        if not url:
            return {"ok": False, "error": "该文章没有原文链接"}

        # Dedup
        existing = _refetch_tasks.get(article_id)
        if existing and existing["status"] in ("pending", "running"):
            return {"ok": False, "error": "该文章正在抓取中"}

        # Create notification + task
        notif = _add_notification(article_id, title, status="pending")
        task = {"article_id": article_id, "title": title,
                "status": "pending", "created_at": notif["created_at"]}
        _refetch_tasks[article_id] = task

        c = app.state.crawler
        if c is None:
            return {"ok": False, "error": "抓取服务未就绪"}

        _refetch_executor.submit(_run_refetch, article_id, url, title,
                                 c, db, notif)
        return {"ok": True, "task": task}

    @app.get("/api/notifications")
    async def list_notifications(unread_only: bool = Query(False)):
        """Return notification list, optionally filtered to unread only."""
        with _notification_lock:
            result = [dict(n) for n in _notifications]
        if unread_only:
            result = [n for n in result if not n.get("is_read")]
        return result

    @app.get("/api/notifications/unread-count")
    async def unread_notification_count():
        """Return the count of unread notifications."""
        with _notification_lock:
            count = sum(1 for n in _notifications if not n.get("is_read"))
        return {"count": count}

    @app.post("/api/notifications/{notif_id}/read")
    async def mark_notification_read(notif_id: int):
        """Mark a single notification as read."""
        with _notification_lock:
            for n in _notifications:
                if n["id"] == notif_id:
                    n["is_read"] = True
                    return {"ok": True}
        return JSONResponse({"ok": False, "error": "通知不存在"}, status_code=404)
```

- [ ] **Step 7: 运行测试，确认通过**

```bash
python -m pytest tests/test_refetch.py -v
# Expected: all tests PASS
```

- [ ] **Step 8: Commit**

```bash
git add web/app.py tests/test_refetch.py
git commit -m "feat: add refetch API endpoints with in-memory task management"
```

---

### Task 3: HTML — 新闻详情页工具栏

**Files:**
- Modify: `web/templates/pages/news_detail.html`

**Interfaces:**
- Consumes: `icon_svg("refresh")` from Jinja2 globals, `article.id` from template context

- [ ] **Step 1: 在 news_detail.html 中添加工具栏**

在 `news_detail.html` 的 `{% block content %}` 内，`.app-main` div 的末尾（`</div><!-- /app-main -->` 之前）添加：

```html
  <!-- Tool Bar -->
  <div class="tool-bar">
    <button class="tool-btn"
            onclick="fetch('/api/news/{{ article.id }}/refetch', {method:'POST'})"
            title="重新抓取">
      {{ icon_svg("refresh") | safe }}
    </button>
  </div>
```

- [ ] **Step 2: 验证渲染正确**

```bash
# 启动 app，检查详情页 HTML 包含 .tool-bar
curl -s http://localhost:8000/news/1 2>/dev/null | grep -q 'tool-bar' && echo "OK" || echo "MISSING"
```

- [ ] **Step 3: Commit**

```bash
git add web/templates/pages/news_detail.html
git commit -m "feat: add tool bar to news detail page"
```

---

### Task 4: HTML — 铃铛图标 + 抽屉

**Files:**
- Modify: `web/templates/pages/hot_news.html`

**Interfaces:**
- Consumes: `icon_svg("bell")` from Jinja2 globals

- [ ] **Step 1: 在 section-header 中添加铃铛图标**

在 `hot_news.html` 的 `date-filter-trigger` closing `</div>` 之后、`</div><!-- /section-header -->` 之前添加：

```html
        <!-- Notification bell -->
        <div class="bell-btn-wrap">
          <button class="bell-btn" id="bell-btn" onclick="toggleDrawer()" title="消息通知">
            {{ icon_svg("bell") | safe }}
          </button>
          <span class="bell-badge" id="bell-badge" data-count="0"></span>
        </div>
```

- [ ] **Step 2: 在 hot_news.html 末尾添加通知抽屉 HTML**

在 `{% endblock %}` 之前（即 `{% block scripts %}` 之前）添加：

```html

<!-- Notification Drawer -->
<div class="notify-overlay" id="notify-overlay" onclick="closeDrawer()"></div>
<div class="notify-drawer" id="notify-drawer">
  <div class="notify-drawer-list" id="notify-list">
    <div class="notify-empty">
      {{ icon_svg("bell") | safe }}
      <span>暂无未读消息</span>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Commit**

```bash
git add web/templates/pages/hot_news.html
git commit -m "feat: add bell icon and notification drawer to hot news page"
```

---

### Task 5: JS — 轮询 + Toast + 抽屉交互

**Files:**
- Modify: `web/templates/base.html` — 在 `{% block scripts %}{% endblock %}` 之前追加共享 JS

**Interfaces:**
- Consumes: API endpoints `/api/notifications/unread-count`, `/api/notifications?unread_only=true`, `/api/notifications/{id}/read`
- Produces: global functions `toggleDrawer()`, `closeDrawer()`, `updateBadge()`, polling interval

- [ ] **Step 1: 在 base.html 中添加共享 JS**

在 `web/templates/base.html` 的 `{% block scripts %}{% endblock %}` 之前添加：

```html
<!-- Shared: notification polling + toast + drawer -->
<script>
(function() {
  'use strict';

  var POLL_INTERVAL = 5000;
  var TOAST_DURATION = 5000;
  var shownIds = {};   // track which notifs already toasted

  // ── Toast ──
  function showToast(notif) {
    var container = document.getElementById('toast-container');
    if (!container) return;

    var toast = document.createElement('div');
    toast.className = 'toast ' + (notif.status === 'completed' ? 'done' : 'fail');

    var body = document.createElement('div');
    body.className = 'toast-body';

    var title = document.createElement('div');
    title.className = 'toast-title';
    title.innerHTML = '<span class="dot"></span>' + escapeHtml(notif.title);

    var sub = document.createElement('div');
    sub.className = 'toast-sub';
    sub.textContent = notif.status === 'completed' ? '抓取完成' : '抓取失败';

    body.appendChild(title);
    body.appendChild(sub);
    toast.appendChild(body);

    toast.addEventListener('click', function() {
      window.location.href = '/news/' + notif.article_id;
    });

    container.appendChild(toast);

    setTimeout(function() {
      toast.classList.add('fading');
      setTimeout(function() {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 300);
    }, TOAST_DURATION);
  }

  function escapeHtml(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
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
  window.toggleDrawer = function() {
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
    }
  };

  window.closeDrawer = function() {
    var drawer = document.getElementById('notify-drawer');
    var overlay = document.getElementById('notify-overlay');
    if (!drawer || !overlay) return;
    drawer.classList.remove('is-open');
    overlay.classList.remove('is-open');
    document.body.style.overflow = '';
    // Mark all shown as read
    var list = document.getElementById('notify-list');
    if (list) {
      var items = list.querySelectorAll('.notify-item');
      items.forEach(function(item) {
        var id = item.getAttribute('data-id');
        if (id) {
          fetch('/api/notifications/' + id + '/read', { method: 'POST' });
        }
      });
    }
    updateBadge(0);
  };

  // ── Fetch ──
  function fetchNotifications(forDrawer) {
    fetch('/api/notifications?unread_only=true')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (!Array.isArray(data)) return;

        if (forDrawer) {
          renderDrawerList(data);
        }

        data.forEach(function(n) {
          if (!shownIds[n.id] && (n.status === 'completed' || n.status === 'failed')) {
            shownIds[n.id] = true;
            // Only toast on hot-news or detail pages
            var path = window.location.pathname;
            if (path.indexOf('/hot-news') === 0 || path.indexOf('/news/') === 0) {
              showToast(n);
            }
          }
        });
      })
      .catch(function() { /* ignore network errors */ });
  }

  function fetchUnreadCount() {
    fetch('/api/notifications/unread-count')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        updateBadge(data.count || 0);
        if (data.count > 0) {
          fetchNotifications(false);
        }
      })
      .catch(function() {});
  }

  function renderDrawerList(data) {
    var list = document.getElementById('notify-list');
    if (!list) return;

    if (data.length === 0) {
      list.innerHTML = '<div class="notify-empty">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>' +
        '<span>暂无未读消息</span></div>';
      return;
    }

    var html = '';
    data.forEach(function(n) {
      var dotClass = n.status === 'completed' ? 'done' : 'fail';
      var statusClass = n.status === 'completed' ? 'done' : n.status === 'failed' ? 'fail' : 'running';
      var statusText = n.status === 'completed' ? '已完成' : n.status === 'failed' ? '失败' : '抓取中';

      html +=
        '<div class="notify-item" data-id="' + n.id + '" onclick="window.location.href=\'/news/' + n.article_id + '\'">' +
          '<span class="notify-item-dot ' + dotClass + '"></span>' +
          '<div class="notify-item-body">' +
            '<div class="notify-item-title">' + escapeHtml(n.title) + '</div>' +
            '<div class="notify-item-meta">' +
              '<span class="notify-item-status ' + statusClass + '">' + statusText + '</span>' +
            '</div>' +
          '</div>' +
        '</div>';
    });
    list.innerHTML = html;
  }

  // ── Polling ──
  // Only active on hot-news and news detail pages
  var path = window.location.pathname;
  if (path.indexOf('/hot-news') === 0 || path.indexOf('/news/') === 0) {
    // Create toast container if not present
    if (!document.getElementById('toast-container')) {
      var tc = document.createElement('div');
      tc.className = 'toast-container';
      tc.id = 'toast-container';
      document.body.appendChild(tc);
    }

    fetchUnreadCount();
    setInterval(fetchUnreadCount, POLL_INTERVAL);
  }
})();
</script>
```

- [ ] **Step 2: Commit**

```bash
git add web/templates/base.html
git commit -m "feat: add notification polling, toast, and drawer JS"
```

---

### Task 6: Wiring — main.py 传入 Crawler

**Files:**
- Modify: `main.py`

**Interfaces:**
- Consumes: `Crawler` class from `news.crawler`
- Produces: `create_app(db, s3_config, signals=signals, crawler=crawler)` call

- [ ] **Step 1: 修改 main.py 中 create_app 调用**

在 `main.py` 的 `run()` 方法中，找到：

```python
        app = create_app(self.db, s3_config, signals=signals)
```

替换为：

```python
        crawler = Crawler(self.config, pg_db=self.db)
        app = create_app(self.db, s3_config, signals=signals, crawler=crawler)
```

同时修改 `_crawl_job` 方法，复用同一个 crawler 实例。在 `__init__` 中添加 `self._crawler = None`，在 `run()` 中创建 crawler 后赋值 `self._crawler = crawler`，`_crawl_job` 中检查 `if self._crawler is None: self._crawler = Crawler(...)`。

但为了最小改动，`_crawl_job` 保持每次新建 Crawler（原逻辑不变），只为 web 创建一个持久的 crawler。

最终修改 `main.py` 第 163-164 行附近：

```python
        # 3. Start web server first — non-blocking
        signals = {
            "crawl": self._crawl_signal,
            "sync": self._sync_signal,
        }
        s3_config = self.config.get("storage", {}).get("resource", {})
        web_crawler = Crawler(self.config, pg_db=self.db)
        app = create_app(self.db, s3_config, signals=signals, crawler=web_crawler)
```

- [ ] **Step 2: 验证启动**

```bash
python main.py &
sleep 3
# Check that the refetch endpoint exists
curl -s -X POST http://localhost:8000/api/news/1/refetch | python -m json.tool
# Expected: {"ok": false, "error": "文章不存在"} or {"ok": true, ...}
```

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: pass shared crawler instance to web app for refetch"
```

---

### Task 7: 集成验证 + 手动测试

**Files:** 无新文件

- [ ] **Step 1: 运行完整测试套件**

```bash
python -m pytest tests/ -v
# Expected: all tests pass
```

- [ ] **Step 2: 端到端手动验证清单**

```bash
# 1. 启动应用
python main.py

# 2. 访问热点新闻页
#    http://localhost:8000/hot-news
#    ✓ 日期筛选按钮右侧可见铃铛图标
#    ✓ 铃铛初始无 badge

# 3. 访问某条新闻详情页
#    http://localhost:8000/news/1
#    ✓ 右侧可见工具栏（刷新图标）
#    ✓ 点击刷新按钮 → 浏览器 Network 面板可见 POST /api/news/1/refetch 请求

# 4. 等待抓取完成（观察终端日志）
#    ✓ 浏览器右上角弹出 Toast
#    ✓ Toast 点击可跳转到新闻详情页

# 5. 回到热点新闻页
#    ✓ 铃铛 badge 显示未读数
#    ✓ 点击铃铛 → 右侧滑出抽屉
#    ✓ 抽屉显示未读通知列表
#    ✓ 点击遮罩关闭抽屉 → badge 清零
#    ✓ 点击某条通知 → 跳转到对应新闻详情页
```

- [ ] **Step 3: Commit（如有微调）**

```bash
git add -A
git commit -m "chore: final integration tweaks for refetch feature"
```

---

## 自审

**Spec coverage:**
- ✅ POST /api/news/{id}/refetch — Task 2 Step 6
- ✅ GET /api/notifications — Task 2 Step 6
- ✅ GET /api/notifications/unread-count — Task 2 Step 6
- ✅ POST /api/notifications/{id}/read — Task 2 Step 6
- ✅ 内存状态管理 (max_workers=10, 去重, 通知上限50) — Task 2 Step 4
- ✅ 右侧工具栏 — Task 3
- ✅ Toast 通知 — Task 5 (JS) + Task 1 (CSS)
- ✅ 铃铛 + badge — Task 4 (HTML) + Task 1 (CSS)
- ✅ 通知抽屉 — Task 4 (HTML) + Task 5 (JS) + Task 1 (CSS)
- ✅ 前端轮询 5 秒 — Task 5
- ✅ main.py 传入 crawler — Task 6

**Placeholder scan:** 无 TBD/TODO。所有步骤包含完整代码。

**Type consistency:**
- `_refetch_tasks: dict[int, dict]` — key=article_id, value=task dict ✅
- `_notifications: list[dict]` — 每个 dict 包含 id, article_id, title, status, error_message, is_read, created_at ✅
- API 返回格式与前端 JS 消费字段匹配 ✅
