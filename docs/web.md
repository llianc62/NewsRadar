# Web — FastAPI 前端

FastAPI + Jinja2 服务端渲染。模板在 `web/templates/`，静态资源在 `web/static/`。

## 模块结构

Web 层在 Phase 0 重构中从单文件拆分为多文件结构，按职责分离：

| 文件 | 职责 |
|------|------|
| `web/app.py` | FastAPI 工厂函数 `create_app()` — 组装 lifespan、static、state、router |
| `web/news.py` | 新闻相关路由（概览 / 详情 / 触发 / 通知 / 情感 / refetch） |
| `web/agent.py` | Agent 聊天页面 + 会话管理 REST + WebSocket 实时通道 |
| `web/settings.py` | 设置面板路由：HTML 页面（/settings*）+ 系统设置/新闻源/模型 JSON API（/api/settings*, /api/models*） |
| `web/background.py` | `BackgroundTaskRunner` — 通用后台任务执行器 |
| `web/notification.py` | `NotificationState` — 线程安全内存通知系统 + SSE 分发 |
| `web/config.py` | Jinja2 模板渲染配置 |

## 页面路由

| 路由 | 用途 |
|------|------|
| `/` | 市场概览：tier 统计 + 来源排名 |
| `/hot-news` | 分页卡片流，支持 URL-as-state 筛选 |
| `/news/{id}` | 单篇文章详情，Markdown 渲染 |
| `/agent` | Agent 聊天页面 |

### /hot-news 筛选参数

全部通过 URL query string 传递，可分享：
- `tier` / `sentiment` — 分层/情感筛选
- `keyword` — 关键词搜索
- `search` — 全文搜索
- `date_from` / `date_to` — 日期范围
- `source_tags` — 多标签筛选（标签从文章内容提取）
- `?all=1` — 清除日期筛选

标签为 PostgreSQL `TEXT[]` / SQLite JSON 字符串，前端渲染为带移除按钮的标签 chips。

## News API 路由

| 方法 | 路由 | 用途 |
|------|------|------|
| `POST` | `/api/trigger/{crawl,sync}` | 手动触发 daemon 信号（通过 asyncio.Queue + callback） |
| `POST` | `/api/news/fetch` | 提交 URL 后台抓取/重新抓取（通过 BackgroundTaskRunner） |
| `POST` | `/api/news/{id}/refetch` | 后台重新下载文章正文 |
| `DELETE` | `/api/news/{id}` | 级联删除文章 + 图片 |
| `GET` | `/api/notifications` | 通知列表（`?unread_only=true`） |
| `GET` | `/api/notifications/unread-count` | 未读徽章计数 |
| `POST` | `/api/notifications/{id}/read` | 标记已读 |
| `GET` | `/api/notifications/stream` | SSE 端点 — 推送实时通知更新 |
| `GET` | `/media/{path}` | S3 预签名 URL 代理（文章图片） |

手动触发通过 daemon 的 asyncio.Queue 传递回调 closure，任务完成后回调推送 SSE 通知。refetch 和 URL fetch 通过 `BackgroundTaskRunner`（ThreadPoolExecutor）执行，零业务依赖。

## Agent API 路由

| 方法 | 路由 | 用途 |
|------|------|------|
| `GET` | `/agent` | Agent 聊天页面（HTML） |
| `GET` | `/api/agent/sessions` | 会话列表（分页） |
| `POST` | `/api/agent/sessions` | 新建会话（设 httponly cookie） |
| `DELETE` | `/api/agent/sessions/{session_id}` | 删除会话 |
| `GET` | `/api/agent/sessions/{session_id}/messages` | 消息历史（兜底/调试端点，前端显示走 WS snapshot） |
| `WS` | `/api/agent/ws` | 实时聊天 WebSocket（`?session_id=` 连接即发 snapshot） |

### WebSocket 协议

客户端发送 JSON 消息，类型字段 `type`：

| 类型 | 方向 | 说明 |
|------|------|------|
| `chat` | client → server | 发送用户消息，含 `session_id`、`message`、`model`、`running_mode` |
| `switch` | client → server | 点击触发的显式切换智能体（轮间），含 `session_id`、`agent_id`（空 = 默认助手） |
| `stop` | client → server | 取消正在生成的回答 |
| `tool_approval_response` | client → server | 用户对工具调用的审批结果 |
| `snapshot` | server → client | 连接/重连的会话快照：`messages`（就近读 agent、兜底 DB）+ `partial`（运行中任务已累积回复）+ `running` + `agent`（当前执行体 key） |
| `token` | server → client | 流式输出片段（`content` 字段） |
| `done` | server → client | 生成完成（含 `full_reply`、`stopped`） |
| `switch_ack` | server → client | 切换完成确认 |
| `tool_approval_request` | server → client | 请求用户审批工具调用 |
| `error` | server → client | 错误信息 |

**快照与续推**：`_forward` 对运行中任务先 `subscribe()` 再同步读 `buffer`（两步间无 await），订阅前的 token 全在 `partial` 快照、之后全在队列，不重不漏。已完成任务不 replay（快照已含最终回复）。WS 断开只取消转发协程，绝不取消生成任务。

## 内容渲染

- **Markdown → HTML**：mistune GFM，`escape=False`（允许原始 HTML）
- **插件**：`strikethrough`、`footnotes`、`table`、`task_lists`
- **Jinja2 过滤器**：`|markdown`
- **H1 处理**：详情页去掉第一个 H1（避免标题重复）

## 通知系统

`NotificationState`（`web/notification.py`）— 模块级内存存储（**不持久化**，daemon 重启丢失）：
- 容量上限 50 条，线程安全（`threading.Lock`）
- 支持 scope / category 过滤、未读计数
- SSE 端点 `/api/notifications/stream` 推送实时更新
- 跨线程 SSE 分发：`call_soon_threadsafe` + `asyncio.Queue`

## 后台任务执行器

`BackgroundTaskRunner`（`web/background.py`）— 通用后台任务执行器：
- 基于 `ThreadPoolExecutor`，零业务依赖
- 按 `task_id` 去重（pending/running 时拒绝重复提交）
- 状态追踪：pending → running → completed / failed
- 用于 refetch 和 URL fetch 场景

## 应用工厂

`create_app(db, s3_config, queues, crawler, agent_config, tool_registry, agent_factory, base_prompt)`：

```python
app = create_app(
    db, s3_config,
    queues={"crawl": crawl_queue, "sync": sync_queue},
    crawler=crawler,
    agent_config=config,           # 完整 config dict
    tool_registry=tool_registry,   # 内置工具注册中心
    agent_factory=agent_factory,   # AgentDefinition -> agent 构建器
    base_prompt=base_prompt,       # agent/Agent.md 系统提示词
)
```

Agent 路由始终注册。聊天室 agent 由 `_build_chat_agent` per-session 惰性构建，依赖 `agent_config` / `base_prompt` / MCP Server。

## 关键文件

| 文件 | 用途 |
|------|------|
| `web/app.py` | FastAPI 应用工厂 |
| `web/news.py` | 新闻 + 通知路由 |
| `web/agent.py` | Agent 路由 + WebSocket |
| `web/settings.py` | 设置面板路由（HTML 页面 + /api/settings、/api/models JSON API） |
| `web/background.py` | 通用后台任务执行器 |
| `web/notification.py` | 内存通知系统 |
| `web/config.py` | 模板渲染配置 |
| `web/templates/` | Jinja2 模板 |
| `web/static/` | CSS / JS 静态资源 |
