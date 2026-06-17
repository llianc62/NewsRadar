# 新闻重新抓取 + 后台消息通知 — 设计文档

**日期**: 2026-06-17
**状态**: 已确认

---

## 概述

在新闻详情页增加重新抓取功能，支持后台异步执行，完成后通过 Toast + 铃铛通知用户。

---

## 一、后端架构

### 1.1 状态管理

不引入新类，在 `web/app.py` 模块级别用 dict 管理所有状态，配置硬编码。

```python
# 模块级状态
_refetch_tasks: dict[int, dict] = {}      # key=article_id, 去重+状态跟踪
_notifications: list[dict] = []           # 通知列表，最多 50 条
_notification_counter: int = 0            # 自增 ID
_refetch_executor: ThreadPoolExecutor     # max_workers=10
```

`create_app()` 接受 `crawler` 参数，在 lifespan 中初始化 executor。

**任务信息结构（纯 dict）：**
```python
{
    "id": 1,                        # 通知 ID（自增整数）
    "article_id": 123,
    "title": "某某新闻标题",
    "status": "completed",          # 'pending' | 'running' | 'completed' | 'failed'
    "error_message": "",            # 失败原因，成功时为空
    "is_read": False,
    "created_at": 1718572800.0,     # time.time()
}
```

### 1.2 执行流程

```
POST /api/news/{id}/refetch
  │
  ├─ 查数据库获取 article.url, article.title
  ├─ 检查去重: 同一 article_id 已有 pending/running → 返回 {ok: false, error: "..."}
  ├─ 创建通知记录 (status='pending'), 返回 {ok: true, task: {...}}
  ├─ 提交到 ThreadPoolExecutor
  │     │
  │     ├─ 更新 status='running'
  │     ├─ crawler.fetch(url, OutputStyle.POSTGRESQL, with_content=True, with_image=True)
  │     │     │
  │     │     ├─ 成功 → 更新 status='completed'
  │     │     └─ 异常 → 更新 status='failed', error_message=...
  │     │
  │     └─ 完成后从去重字典中移除 article_id
  │
  └─ 返回（不等待任务完成）
```

### 1.3 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/news/{id}/refetch` | 提交重新抓取 |
| `GET` | `/api/notifications` | 通知列表，支持 `?unread_only=true` |
| `GET` | `/api/notifications/unread-count` | 仅返回未读数 `{count: 3}` |
| `POST` | `/api/notifications/{id}/read` | 标记单条已读 |

---

## 二、前端设计

### 2.1 右侧工具栏（详情页 /news/{id}）

- **位置**：`position: fixed; right: 16px; top: 50%; transform: translateY(-50%)`
- **外观**：极窄竖条，宽约 36-40px，仅够展示图标
- **结构**：容器预留扩展空间，未来工具图标垂直追加
- **首期内容**：一个刷新图标按钮

**刷新按钮行为：**
- 点击 → `POST /api/news/{id}/refetch`
- 无动画，无前端防抖，无 loading 状态展示
- 后端返回结果静默处理（`ok: true` 或 `ok: false` 均不弹 toast）
- Toast 只在任务真正完成时由轮询触发弹出

### 2.2 Toast 通知

- **位置**：页面右上角，`position: fixed; top: 16px; right: 60px`（避免被工具栏遮挡）
- **可见范围**：热点新闻页（/hot-news）和新闻详情页（/news/{id}）
- **内容**：图标 + 新闻标题 + 状态（"抓取完成" / "抓取失败"）
- **状态颜色**：完成=绿色，失败=红色
- **行为**：
  - 前端轮询 `/api/notifications` 发现新完成/失败通知时弹出
  - 自动消失（约 5 秒后）
  - 点击 Toast → 跳转到该新闻详情页（如果不在该页面）

### 2.3 铃铛图标（热点新闻页 /hot-news）

- **位置**：section-header 区域，日期筛选按钮右侧
- **外观**：铃铛 SVG 图标
- **Badge**：未读数量红色小圆点 + 数字，无未读时不显示

### 2.4 通知抽屉

- **触发**：点击铃铛图标
- **外观**：右侧滑出面板，宽约 320px，覆盖在内容上方
- **遮罩**：半透明背景，点击遮罩关闭
- **内容**：仅展示未读通知列表
  - 每行：图标 + 新闻标题（截断到一行）+ 状态标签
  - 状态颜色：完成=绿色，失败=红色，进行中=灰色
- **行为**：
  - 点击某条通知 → 跳转到对应新闻详情页，自动标记已读
  - 关闭抽屉（点击遮罩或外部）→ 抽屉中可见通知自动标记已读

### 2.5 前端轮询

- 热点新闻页和新闻详情页加载后启动轮询
- 每 5 秒调用 `GET /api/notifications/unread-count`
- 发现新未读数 → 调用 `GET /api/notifications?unread_only=true` 获取详情
- 新完成的 toast 弹出一次（用已弹出 Set 去重）
- 更新铃铛 badge 数字

---

## 三、文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `web/app.py` | 修改 | 模块级状态管理 + 新增 API 端点 |
| `web/templates/pages/news_detail.html` | 修改 | 增加右侧工具栏 |
| `web/templates/pages/hot_news.html` | 修改 | 增加铃铛图标 + 通知抽屉 |
| `web/templates/base.html` | 修改 | 增加通知轮询 JS（或独立 JS 文件） |
| `web/static/css/app.css` | 修改 | 工具栏、Toast、铃铛、抽屉样式 |
| `main.py` | 修改 | 创建 Crawler 实例并传入 create_app |

---

## 四、边界情况

| 场景 | 处理 |
|------|------|
| 文章无 url | 拒绝 refetch，返回错误 |
| 同一文章重复提交 | 返回 `{ok: false, error: "该文章正在抓取中"}` |
| 线程池满 | 排队等待（ThreadPoolExecutor 内置队列） |
| 通知超过 50 条 | 淘汰最旧通知 |
| 服务重启 | 内存数据丢失，通知清空，不影响已持久化的新闻数据 |
| 抓取超时 | Crawler 内置 timeout 控制，超时视为 failed |
| 用户快速切换页面 | 轮询在页面 unload 时停止，Toast 不跨页面残留 |

---

## 五、不包含的内容

- 通知不持久化到数据库（纯内存）
- 不做 WebSocket/SSE 实时推送
- 工具栏仅刷新按钮，不预先实现未来工具
- 无"全部已读"功能
- 不做浏览器 Notification API 推送
