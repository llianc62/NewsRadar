# Web — FastAPI 前端

FastAPI + Jinja2 服务端渲染。模板在 `web/templates/`，静态资源在 `web/static/css/`。

## 页面路由

| 路由 | 用途 |
|------|------|
| `/` | 市场概览：tier 统计 + 来源排名 |
| `/hot-news` | 分页卡片流，支持 URL-as-state 筛选 |
| `/news/{id}` | 单篇文章详情，Markdown 渲染 |

### /hot-news 筛选参数

全部通过 URL query string 传递，可分享：
- `tier` / `sentiment` — 分层/情感筛选
- `keyword` — 关键词搜索
- `search` — 全文搜索
- `date_from` / `date_to` — 日期范围
- `source_tags` — 多标签筛选（标签从文章内容提取）
- `?all=1` — 清除日期筛选

标签为 PostgreSQL `TEXT[]` / SQLite JSON 字符串，前端渲染为带移除按钮的标签 chips。

## API 路由

| 方法 | 路由 | 用途 |
|------|------|------|
| `POST` | `/api/trigger/{crawl,sync}` | 手动触发 daemon 信号 |
| `POST` | `/api/news/fetch` | 提交 URL 后台抓取/重新抓取 |
| `POST` | `/api/news/{id}/refetch` | 后台重新下载文章正文 |
| `DELETE` | `/api/news/{id}` | 级联删除文章 + 图片 |
| `GET` | `/api/notifications` | 通知列表（`?unread_only=true`） |
| `GET` | `/api/notifications/unread-count` | 未读徽章计数 |
| `POST` | `/api/notifications/{id}/read` | 标记已读 |
| `GET` | `/media/{path}` | S3 预签名 URL 代理（文章图片） |

## 内容渲染

- **Markdown → HTML**：mistune GFM，`escape=False`（允许原始 HTML）
- **插件**：`strikethrough`、`footnotes`、`table`、`task_lists`
- **Jinja2 过滤器**：`|markdown`
- **H1 处理**：详情页去掉第一个 H1（避免标题重复）

## 通知系统

模块级内存存储（**不持久化**，daemon 重启丢失）：
- 容量上限 50 条，线程安全（`threading.Lock`）
- 后台任务完成后写入通知
- URL → article_id 回填：抓取完成后关联文章 ID

## 应用工厂

`create_app(config, crawler)` — Crawler 实例注入，共享 fetch 逻辑。Web 层和 daemon 使用同一个 Crawler，无需重复创建 session。

## 关键文件

| 文件 | 用途 |
|------|------|
| `web/app.py` | FastAPI 应用工厂 + 所有路由 |
| `web/templates/` | Jinja2 模板（base.html + components/） |
| `web/static/css/` | 静态样式 |
