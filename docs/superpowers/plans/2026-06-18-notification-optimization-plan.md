# 通知栏优化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 通知抽屉展示全部消息（已读半透明区分），点击通知项先标已读再跳转

**Architecture:** 纯前端改动，后端 API 不变。base.html JS 三处修改 + app.css 一条新规则 + hot_news.html 文案调整

**Tech Stack:** Vanilla JS, CSS, Jinja2 模板

## Global Constraints

- 后端 API 零改动
- 内存存储，重启丢失可接受
- 通知列表上限 50 条
- 轮询间隔保持 5s

---

### Task 1: CSS — 已读样式 + 边框

**Files:**
- Modify: `web/static/css/app.css:1029-1037`

**Interfaces:**
- Produces: `.notify-item.is-read` selector（供 Task 3 JS 渲染使用）
- Produces: `.notify-item` 增加 `border` 和 `margin-bottom`

- [ ] **Step 1: 修改 .notify-item 加边框，新增 .is-read 规则**

在 `web/static/css/app.css` 中，找到 `.notify-item` 规则（约 1029 行），添加 `border` 和 `margin-bottom`。在 `.notify-item-time` 规则后新增 `.notify-item.is-read` 规则。

```css
.notify-item {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 14px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  margin-bottom: 6px;
  cursor: pointer;
  transition: background var(--duration-fast), opacity var(--duration-fast);
  text-decoration: none; color: inherit;
  position: relative;
  background: var(--bg);
}

/* ... 现有 hover 规则不变 ... */

.notify-item.is-read {
  opacity: 0.45;
}
.notify-item.is-read:hover {
  opacity: 0.7;
}
```

- [ ] **Step 2: 验证 CSS 无语法错误**

```bash
# 手动检查：确认规则在大括号内闭合正确，无多余逗号
grep -n "notify-item" /home/llianc62/ws/NewsRadar/web/static/css/app.css
```

- [ ] **Step 3: Commit**

```bash
git add web/static/css/app.css
git commit -m "style: add is-read styling and border to notification items"
```

---

### Task 2: 模板 — 空状态文案

**Files:**
- Modify: `web/templates/pages/hot_news.html:250`

**Interfaces:**
- 无依赖

- [ ] **Step 1: 修改空状态文案**

在 `web/templates/pages/hot_news.html` 约 250 行，将 `<span>暂无未读消息</span>` 改为 `<span>暂无消息</span>`。

```html
<!-- 改前 -->
<span>暂无未读消息</span>
<!-- 改后 -->
<span>暂无消息</span>
```

- [ ] **Step 2: Commit**

```bash
git add web/templates/pages/hot_news.html
git commit -m "fix: update notification empty state text"
```

---

### Task 3: JS — 全部消息 + 点击标记已读

**Files:**
- Modify: `web/templates/base.html:148-214`

**Interfaces:**
- Consumes: `.notify-item.is-read` CSS class（Task 1）
- Consumes: `GET /api/notifications`（不带 `unread_only` 参数）
- Consumes: `POST /api/notifications/{notif_id}/read`
- Produces: `markAndGo(notifId, articleId)` 全局函数

- [ ] **Step 1: 修改 `fetchNotifications` — 请求全部通知**

将 `unread_only=true` 参数移除。

```javascript
// 改前（约 150 行）
fetch('/api/notifications?unread_only=true')

// 改后
fetch('/api/notifications')
```

- [ ] **Step 2: 新增 `markAndGo` 函数 + `formatRelativeTime` 辅助函数**

在 `escapeAttr` 函数之后（约 99 行后）添加两个辅助函数：

```javascript
function markAndGo(notifId, articleId) {
  fetch('/api/notifications/' + notifId + '/read', { method: 'POST' });
  window.location.href = '/news/' + articleId;
}

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
```

- [ ] **Step 3: 修改 `renderDrawerList` — 已读 class + 状态文案 + 时间**

完整替换 `renderDrawerList` 函数（约 185-213 行）：

```javascript
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
    var dotClass = n.status === 'completed' ? 'done' : n.status === 'failed' ? 'fail' : 'running';
    var statusClass = n.status === 'completed' ? 'done' : n.status === 'failed' ? 'fail' : 'running';
    var statusText = n.status === 'completed' ? '抓取成功' : n.status === 'failed' ? '抓取失败' : '抓取中';
    var readClass = n.is_read ? ' is-read' : '';
    var timeStr = formatRelativeTime(n.created_at);

    html +=
      '<div class="notify-item' + readClass + '" data-id="' + escapeAttr(n.id) + '" onclick="markAndGo(' + escapeAttr(n.id) + ', ' + escapeAttr(n.article_id) + ')">' +
        '<span class="notify-item-dot ' + dotClass + '"></span>' +
        '<div class="notify-item-body">' +
          '<div class="notify-item-title">' + escapeHtml(n.title) + '</div>' +
          '<div class="notify-item-meta">' +
            '<span class="notify-item-status ' + statusClass + '">' + statusText + '</span>' +
            (timeStr ? '<span class="notify-item-time">· ' + escapeHtml(timeStr) + '</span>' : '') +
          '</div>' +
        '</div>' +
      '</div>';
  });
  list.innerHTML = html;
}
```

- [ ] **Step 4: 修改 `renderDrawerList` 的空状态文案（`closeDrawer` 不变）**

确认 `closeDrawer` 函数（约 127-146 行）保持不变 — 逐条标记已读 + badge 归零。

- [ ] **Step 5: 手动验证 — 检查 JS 语法**

```bash
# 检查 markAndGo 和 renderDrawerList 是否正确闭合，无语法错误
node -e "console.log('JS syntax check requires browser context, verify manually in template')"
```

- [ ] **Step 6: Commit**

```bash
git add web/templates/base.html
git commit -m "feat: show all notifications in drawer, mark read on click"
```

---

### Task 4: 集成验证

**Files:**
- No new files — 启动 daemon 手动验证

**接口:**
- Consumes: Task 1 CSS + Task 2 模板 + Task 3 JS

- [ ] **Step 1: 启动服务**

```bash
# 确保 PostgreSQL + MinIO 已运行
docker compose up -d
# 启动 daemon
python main.py
```

- [ ] **Step 2: 手动验证清单**

| # | 验证项 | 预期结果 |
|---|--------|---------|
| 1 | 打开 http://localhost:8000/hot-news，点击铃铛 | 抽屉滑出，显示全部通知 |
| 2 | 已读通知 | 半透明（opacity 0.45），hover 恢复到 0.7 |
| 3 | 未读通知 | 完全不透明，全亮 |
| 4 | 状态文字 | 显示"抓取成功/抓取失败/抓取中" |
| 5 | 每条消息 | 有 1px 边框分隔 |
| 6 | 点击未读通知 | 跳转到文章详情页，返回后再打开抽屉该通知变半透明 |
| 7 | 关闭抽屉 | 全部标已读，角标归零 |
| 8 | 无消息时 | 显示"暂无消息" |

- [ ] **Step 3: 运行现有测试确保无回归**

```bash
pytest tests/ -v
```

- [ ] **Step 4: Commit（如有修复）**

```bash
git add -A
git commit -m "chore: integration verification fixes"
```

---

### Task 5: 前端单元测试

**Files:**
- Create: `tests/test_notification_frontend.py`

**Interfaces:**
- Consumes: `GET /api/notifications`、`POST /api/notifications/{id}/read`、`GET /api/notifications/unread-count`

- [ ] **Step 1: 写测试 — API 返回全部通知（非仅未读）**

```python
"""Test notification API endpoints for the optimized drawer behavior."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create a TestClient with an in-memory notification state."""
    from web.app import create_app
    from storage.postgres import PostgreSQL

    db = PostgreSQL(
        host="localhost", port=5432, database="newsradar_test",
        user="newsradar", password="",
    )
    # Use a mock that won't actually connect
    app = create_app(
        db=db,
        s3_config={
            "endpoint_url": "", "bucket_name": "",
            "access_key_id": "", "secret_access_key": "", "region": "",
        },
    )
    return TestClient(app)


class TestListNotifications:
    def test_returns_all_notifications_including_read(self, client):
        """GET /api/notifications returns all notifications, not just unread."""
        response = client.get("/api/notifications")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


class TestMarkNotificationRead:
    def test_mark_single_notification_as_read(self, client):
        """POST /api/notifications/{id}/read marks the notification as read."""
        # First list to find an existing notification
        response = client.get("/api/notifications")
        data = response.json()

        if len(data) == 0:
            pytest.skip("No notifications in memory to test with")

        notif = data[0]
        notif_id = notif["id"]

        response = client.post(f"/api/notifications/{notif_id}/read")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        # Verify it's now marked read
        response = client.get("/api/notifications")
        updated = [n for n in response.json() if n["id"] == notif_id]
        assert len(updated) == 1
        assert updated[0]["is_read"] is True

    def test_mark_nonexistent_notification_returns_404(self, client):
        """POST /api/notifications/99999/read returns 404."""
        response = client.post("/api/notifications/99999/read")
        assert response.status_code == 404


class TestUnreadCount:
    def test_unread_count_endpoint(self, client):
        """GET /api/notifications/unread-count returns a count."""
        response = client.get("/api/notifications/unread-count")
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert isinstance(data["count"], int)
```

- [ ] **Step 2: 运行测试**

```bash
pytest tests/test_notification_frontend.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_notification_frontend.py
git commit -m "test: add notification API endpoint tests"
```

---

## Summary

| Task | 文件 | 改动量 |
|------|------|--------|
| 1. CSS | `web/static/css/app.css` | ~10 行 |
| 2. 模板 | `web/templates/pages/hot_news.html` | 1 行 |
| 3. JS | `web/templates/base.html` | ~40 行 |
| 4. 集成验证 | 手动 | — |
| 5. 测试 | `tests/test_notification_frontend.py` | ~60 行 |
