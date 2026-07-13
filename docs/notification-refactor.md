# 通知系统重构设计

> **版本**: v2  
> **设计日期**: 2026-07-13  
> **状态**: 设计中  
> **目标**: 将通知系统拆为独立模块，前端后端统一重构，消除 monkey-patch 和路径白名单

---

## 1. 现状问题

### 1.1 后端

- `web/state.py` 三职责合一：通知列表 + SSE 广播 + 后台任务执行（`_run_fetch_url`、`_run_refetch`）
- `web/agent.py` 模块加载时 monkey-patch `web.state._push_sse_event`，让 SSE 事件也推给 WebSocket 客户端，属于运行时副作用
- 后台任务逻辑在 state 模块中，不属其职责

### 1.2 前端

- SSE 订阅在 `base.html` 中，但有**路径白名单**（仅 `/hot-news`、`/news/`、`/agent` 生效）
- Agent 页面从两条通道收通知：SSE + WebSocket `notification` 消息，重复
- 通知抽屉（`notify-drawer`）在 `hot_news.html` 中，其他页面没有抽屉
- `base.html` 内联 ~250 行通知 JS，随页面扩展将更难维护

---

## 2. 核心概念：模块作用域

### 2.1 问题

当前通知的 `category` 字段描述的是**操作类型**（`fetch`、`refetch`、`crawl`、`sync`），没有区分**所属模块**。这导致：

- `crawl` / `sync` → daemon 系统级操作
- `fetch` / `refetch` → hot-news 模块操作

当用户在 hot-news 页面打开通知抽屉，应该只看到 hot-news 相关的通知，而不是系统任务通知。

### 2.2 两级视图

```
                    Toast (弹窗)         通知抽屉 / 角标
                    ──────────           ────────────────
  所有页面             ✅ 全局可见            按模块作用域过滤
  hot-news 页面       ✅ 可见               只显示 scope=news
  agent 页面          ✅ 可见               只显示 scope=agent
```

- **Toast** — 全局弹窗，任何 SSE 事件都触发，不区分作用域
- **通知列表/角标** — 按模块作用域过滤，每个页面只看自己的通知

### 2.3 作用域定义

| scope | 说明 | 包含的操作 |
|-------|------|-----------|
| `news` | hot-news 模块 | crawl, sync, fetch, refetch |
| `agent` | AI 对话模块 | (预留，当前无通知) |

`add_notification()` 新增 `scope` 参数。SSE 事件的 payload 携带 `scope` 字段供前端过滤。

---

## 3. 改造方案

### 3.1 后端模块拆分

```
当前:                    改造后:
web/state.py             web/notification.py   (新建)
├── 通知列表                    ├── 通知列表 CRUD（含 scope 过滤）
├── SSE 广播                   ├── SSE 客户端管理
├── _run_fetch_url             ├── _push_sse_event()
├── _run_refetch               └── add_notification(scope=...)
├── _refetch_executor
└── _push_sse_event()         web/news.py
                              ├── _run_fetch_url()
                              ├── _run_refetch()
                              └── _refetch_executor
                             
                             web/agent.py
                              └── 删除 _patch_push_sse_event()
```

#### `web/notification.py`

新建文件，职责单一：

- 通知列表管理（增、查、改，上限 50 条，线程安全）
- 通知带 `scope` 字段（`news` / `agent`）
- SSE 客户端注册/注销（`asyncio.Queue` 集合）
- SSE 事件推送（线程安全，支持跨线程调用）
- **不依赖任何业务模块**（news、agent、crawler）

对外接口：

| 函数/变量 | 用途 |
|-----------|------|
| `add_notification(scope, ...)` | 创建通知、加入列表、触发 SSE |
| `push_sse_event(data)` | 推送给所有 SSE 客户端 |
| `register_client(queue)` | SSE 端点注册客户端 |
| `unregister_client(queue)` | SSE 端点注销客户端 |
| `_notifications` | 通知列表（外部只读） |
| `_notification_lock` | 线程锁 |

SSE 事件 payload 格式：

```json
{
  "type": "new" | "update",
  "notification": {
    "id": 1,
    "scope": "news",
    "category": "fetch",
    "article_id": 123,
    "title": "...",
    "status": "completed",
    ...
  }
}
```

#### `web/news.py`

新增职责：

- 承接 `_run_fetch_url()` — 后台 URL 抓取
- 承接 `_run_refetch()` — 后台文章正文重新下载
- 管理 `_refetch_executor`（`ThreadPoolExecutor`）和 `_refetch_tasks`（去重字典）

创建通知时传入对应 scope：

```python
# URL 提交 → scope="news"
notif = add_notification(scope="news", article_id=0, url, ...)

# 后台任务状态更新
push_sse_event({"type": "update", "notification": {..., "scope": "news"}})
```

#### `web/app.py`

trigger_crawl / trigger_sync 回调中创建通知传入 `scope="news"`。

#### `web/agent.py`

删除 `_patch_push_sse_event()` 及其相关的 monkey-patch 代码。Agent 页面不再从 WebSocket 接收通知。

### 3.2 API 变更

`GET /api/notifications` 新增 `scope` 查询参数：

| 参数 | 类型 | 说明 |
|------|------|------|
| `scope` | str (可选) | 按模块作用域过滤：`news` / `agent` |
| `unread_only` | bool (可选) | 仅未读 |

未传 `scope` 时返回全部（兼容现有调用）。

`GET /api/notifications/unread-count` 同步新增 `scope` 参数。

### 3.3 前端组件化

```
web/templates/
├── base.html                  # 引入 notification.js（仅 Toast + SSE）
├── components/
│   ├── notification.html      # 新增：通知 UI 骨架（参数化 scope）
│   ├── sidebar.html
│   └── ...
└── pages/
    ├── hot_news.html          # 用 notification.html 取代内联 drawer
    ├── agent_chat.html        # 移除 WebSocket notification handler
    └── ...

web/static/
├── css/
│   └── app.css               # 通知相关样式（已有，不变）
└── js/
    └── notification.js        # 新增：通知 JS 逻辑
```

#### 3.3.1 `components/notification.html` — 参数化组件

通过 Jinja2 变量 `scope` 控制作用域：

```html
{% with scope=scope|default('news') %}
<!-- Toast container (always present, shared) -->
<div id="toast-container"></div>

<!-- Bell button + badge (scoped) -->
<div class="bell-btn-wrap" data-scope="{{ scope }}">
  <button class="bell-btn" onclick="toggleDrawer('{{ scope }}')" title="消息通知">
    {{ icon_svg("bell") | safe }}
  </button>
  <span class="bell-badge" id="bell-badge-{{ scope }}" data-count="0"></span>
</div>

<!-- Notification drawer (scoped) -->
<div class="notify-overlay" id="notify-overlay-{{ scope }}" onclick="closeDrawer('{{ scope }}')"></div>
<div class="notify-drawer" id="notify-drawer-{{ scope }}">
  <div class="notify-drawer-list" id="notify-list-{{ scope }}">
    <div class="notify-empty">
      {{ icon_svg("bell") | safe }}
      <span>暂无消息</span>
    </div>
  </div>
</div>
{% endwith %}
```

| 元素 | scope 依赖 | 说明 |
|------|-----------|------|
| `#toast-container` | 全局共享 | 所有 SSE 事件触发 toast |
| `#bell-badge-{scope}` | 按 scope | 只显示该作用域的未读数 |
| `#notify-drawer-{scope}` | 按 scope | 只列出该作用域的通知 |
| `#notify-overlay-{scope}` | 按 scope | 遮罩层 |

#### 3.3.2 `static/js/notification.js`

**SSE 连接（全局一份）：**

- `EventSource('/api/notifications/stream')`，**无条件启动，无路径白名单**
- `new` 事件 → 所有 scope 的 toast + 按 scope 更新对应角标
- `update` 事件 → 所有 scope 的 toast + 按 scope 更新对应角标 + 刷新对应抽屉

**对外 API（挂载到 `window`）：**

```javascript
// Toast — 全局
window.showAppToast(title, sub, kind, onClick)

// Drawer — 按 scope 操作
window.toggleDrawer(scope)     // 打开/关闭指定 scope 的抽屉
window.closeDrawer(scope)      // 关闭指定 scope 的抽屉

// 通知列表 — 按 scope 拉取
window.fetchNotifications(scope, forDrawer)
window.renderDrawerList(data, scope)

// 已读标记
window.markAndGo(notifId, articleId, status, category)

// 角标更新
window.updateBadge(scope, count)
```

#### 3.3.3 页面使用方式

**`base.html`** — 加载 JS + 共享 toast 容器：

```html
<!-- Toast container 在 base.html 全局存在 -->
<div id="toast-container"></div>

{% block content %}{% endblock %}

<script src="/static/js/notification.js"></script>
```

**`hot_news.html`** — 引入 scope="news" 的 bell + drawer：

```html
{% block content %}
<div class="app-main">
  <div class="section-header">
    ...
    {% include "components/notification.html" with scope="news" %}
  </div>
  ...
</div>
{% endblock %}
```

**`agent_chat.html`** — 如果也需要通知列表，引入 scope="agent"：

```html
{% include "components/notification.html" with scope="agent" %}
```

**市场概览页 `/`** — 如果不需要通知抽屉，就不引入。

### 3.4 页面修改清单

#### `base.html`

- 移除内联通知 JS（~250 行）
- 移除路径白名单
- 保留 `#toast-container`（全局 toast 容器）
- 添加 `<script src="/static/js/notification.js">`

#### `hot_news.html`

- 移除内联 `notify-drawer` 和 `notify-overlay`
- 移除内联铃铛按钮
- 替换为 `{% include "components/notification.html" with scope="news" %}`
- 移除 `triggerCrawl`/`triggerSync` 的回退 toast

#### `agent_chat.html`

- 移除 WebSocket 的 `notification` 消息处理
- 移除 `showNotification` 函数
- 通知走全局 toast

---

## 4. 数据流

```
后端                                       前端
─────────                                  ─────────
trigger_crawl() ──→ add_notification()     notification.js (全局 SSE)
  scope="news"       │                      │
                     ├──→ SSE 'new' 事件 ───→ toast (全局弹窗)
                     │                       ├── updateBadge('news')
                     │                       └── if drawer open: fetch('news')
                     │
_run_fetch_url() ──→ push_sse_event()        │
  scope="news"       │                      │
  ├── running ─────→ SSE 'update' ───────→ toast (全局弹窗)
                     │
用户打开抽屉(scope) ─→ GET /api/notifications?scope=news ─→ renderDrawerList()
用户点击条目 ────────→ POST /api/notifications/{id}/read
```

Agent 页面的通知不再走 WebSocket 通道：

```
改前:  agent.py monkey-patch → WebSocket → agent_chat.html (双通道)
改后:  notification.py → SSE → notification.js → 所有页面 toast + 按 scope 过滤 (单通道)
```

---

## 5. 后端 API 接口

| 方法 | 路由 | 用途 | 变更 |
|------|------|------|------|
| `GET` | `/api/notifications/stream` | SSE 端点 | 不变 |
| `GET` | `/api/notifications` | 通知列表 | 新增 `?scope=news` 参数 |
| `GET` | `/api/notifications/unread-count` | 未读计数 | 新增 `?scope=news` 参数 |
| `POST` | `/api/notifications/{id}/read` | 标记已读 | 不变 |
| `POST` | `/api/notifications/mark-all-read` | 全部已读 | 不变 |

### GET /api/notifications

```python
@router.get("/api/notifications")
async def list_notifications(
    request: Request,
    scope: str | None = Query(None),       # 新增：news / agent
    unread_only: bool = Query(False),
):
    with notification._notification_lock:
        result = [dict(n) for n in notification._notifications]
    if scope:
        result = [n for n in result if n.get("scope") == scope]
    if unread_only:
        result = [n for n in result if not n.get("is_read")]
    # article_id 回填逻辑（略）
    return result
```

### GET /api/notifications/unread-count

```python
@router.get("/api/notifications/unread-count")
async def unread_notification_count(
    scope: str | None = Query(None),       # 新增
):
    with notification._notification_lock:
        count = sum(
            1 for n in notification._notifications
            if not n.get("is_read")
            and (scope is None or n.get("scope") == scope)
        )
    return {"count": count}
```

---

## 6. 实施步骤

### Phase 1: 后端拆模块

```
Step 1.1  创建 web/notification.py
           - 从 web/state.py 提取通知列表 + SSE 广播逻辑
           - 通知新增 scope 字段（news / agent）
           - add_notification(scope=...) 必填参数
           - 暴露 register_client / unregister_client / push_sse_event
           
Step 1.2  GET /api/notifications 新增 ?scope= 过滤
           
Step 1.3  GET /api/notifications/unread-count 新增 ?scope= 过滤
           
Step 1.4  迁后台任务到 web/news.py
           - _run_fetch_url() → news.py 模块级函数，scope="news"
           - _run_refetch()  → news.py 模块级函数，scope="news"
           - _refetch_executor / _refetch_tasks → news.py 模块级变量
           
Step 1.5  trigger_crawl / trigger_sync 回调 → scope="news"
           
Step 1.6  web/state.py 标记废弃（保留引用兼容，后续删除）
           
Step 1.7  删除 web/agent.py 的 _patch_push_sse_event()
           - 删除 monkey-patch 代码
           - 删除模块加载时的 _patch_push_sse_event() 调用
```

### Phase 2: 前端组件化

```
Step 2.1  创建 static/js/notification.js
           - 从 base.html 提取所有通知相关 JS
           - 所有 API 调用（/api/notifications）携带 scope 参数
           - SSE 连接无条件启动
           - 对外暴露 window.showAppToast / toggleDrawer(scope) / closeDrawer(scope) / markAndGo
           
Step 2.2  创建 components/notification.html
           - 参数化 scope（通过 Jinja2 with scope=...）
           - Toast container（全局共享）
           - 铃铛按钮 + 角标（按 scope）
           - 通知抽屉 + 遮罩层（按 scope）
           
Step 2.3  更新 base.html
           - 移除内联通知 JS
           - 移除路径白名单
           - 添加 <script src="/static/js/notification.js">
           - 添加全局 #toast-container
           
Step 2.4  更新 hot_news.html
           - 移除内联 notify-drawer / notify-overlay / bell-btn
           - 替换为 {% include "components/notification.html" with scope="news" %}
           
Step 2.5  更新 agent_chat.html
           - 移除 WebSocket 的 'notification' case
           - 移除 showNotification 函数
```

### Phase 3: 清理验证

```
Step 3.1  验证 SSE 在所有页面生效（/、/hot-news、/news/{id}、/agent）
Step 3.2  验证 toast 弹窗在所有页面触发，不受 scope 限制
Step 3.3  验证 hot-news 页面的通知抽屉只显示 scope="news" 的通知
Step 3.4  验证 hot-news 抽屉只显示 scope="news" 的通知
Step 3.5  验证 Agent 页面不再从 WS 收重复通知
Step 3.6  验证 fetch/refetch/crawl/sync 任务通知流正常
Step 3.7  删除 web/state.py（确认无引用后）
```

---

## 7. 依赖关系图

```
后端:
web/notification.py     ← 无业务依赖，仅通知列表 + SSE
    ↑
web/news.py             ← 依赖 notification.add_notification(scope="news")
    ↑
web/app.py              ← include_router(news_router)

web/agent.py            ← 不再依赖 notification
    ↑
web/app.py              ← include_router(agent_router)

前端:
base.html
├── <div id="toast-container">     (全局 toast)
├── <script src="notification.js"> (全局 SSE)
└── {% block content %}
    ├── hot_news.html
    │   └── notification.html (scope="news")
    ├── agent_chat.html       (无通知组件)
    └── news_detail.html      (无通知组件)
```

---

## 8. 回滚方案

每个 Phase 可独立回滚：

| Phase | 回滚方式 |
|-------|---------|
| Phase 1 | `web/notification.py` 保留，`web/news.py` 的任务函数迁回 `web/state.py`；scope 参数不影响不传参的调用 |
| Phase 2 | `base.html` 的 `<script>` 注释掉即可恢复内联；`notification.html` 的 `{% include %}` 注释掉即可恢复 `hot_news.html` 内联 drawer |
| Phase 3 | `web/state.py` 保留不删，引用切回即可 |

`window.showAppToast` API 签名不变。scope 参数为可选，不传 `?scope=` 的 API 调用返回全部通知，向后兼容。
