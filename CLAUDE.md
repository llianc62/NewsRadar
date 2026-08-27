# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Documentation

模块设计文档在 `docs/` 目录，按需读取：
- [analyzer.md](docs/analyzer.md) — 热度评分 + 情感分析 + 关键词提取
- [parser.md](docs/parser.md) — HTML 提取流水线 + Registry 路由
- [storage.md](docs/storage.md) — PostgreSQL / SQLite+S3 双存储
- [crawler.md](docs/crawler.md) — 爬取管线 + 内容富化 + 失败重试
- [web.md](docs/web.md) — FastAPI 前端 + 通知系统
- [daemon.md](docs/daemon.md) — 后台调度 + 启动序列
- [agent/architecture-v1.md](docs/agent/architecture-v1.md) — AI Agent 子系统设计（Phase 1-3 完成）
- [agent/configuration.md](docs/agent/configuration.md) — 配置参考
- [agent/phase0-chat.md](docs/agent/phase0-chat.md) — 聊天 Agent 设计

历史开发记录在 `docs/superpowers/`（plans + specs），不主动加载。

## CLI 模块结构

CLI 使用 Typer 框架，入口在 `cli/__init__.py`：

```
cli/
├── __init__.py   # Typer app 创建 + 子命令注册
├── crawl.py      # python -m cli crawl — 抓取 → SQLite
├── notify.py     # python -m cli notify — 关键词匹配 → 邮件
├── grab.py       # python -m cli grab-one — 单 URL 测试
├── knowledge.py  # python -m cli knowledge - 知识库 ingest/search/list/clear
└── db.py         # python -m cli db clear — 数据库维护
```

`python -m cli` 自动路由到 `cli.__main__`，后者调用 `cli.app()`。

## 环境设置

```bash
# 1. 安装依赖 (Python >= 3.12)
uv sync
uv pip install pytest  # pytest 不在 pyproject.toml 中
crawl4ai-setup  # 装 patchright 浏览器(WAF 站点抓取)

# 2. 启动基础设施 (PostgreSQL 16 + MinIO)
docker compose up -d

# 3. 配置环境变量
cp env.example .env
# 按需编辑 .env，至少填写 EMAIL_* 和 S3 相关变量
```

配置优先级：**环境变量 > config.yaml**。可通过 `CONFIG_PATH` 指定配置文件路径。

## Commands

```bash
# Local daemon — PostgreSQL + FastAPI web dashboard
python main.py
CONFIG_PATH=/path/to/custom.yaml python main.py  # 指定配置文件

# Cloud CI (GitHub Actions) — fetch + SQLite + S3
python -m cli crawl
python -m cli notify

# Test content extraction on a single URL
python -m cli grab-one "https://example.com" --output-style markdown
python -m cli grab-one "https://example.com" --output-style postgresql --images

# Database maintenance
python -m cli db clear --start "2026-07-02" --end "2026-07-04" --force
python -m cli db clear --all --force

# Knowledge base (pgvector) - requires `knowledge.enabled: true` + embedding key
python -m cli knowledge ingest path/to/doc.md --namespace buffett   # 导入文档
python -m cli knowledge search "查询语句" --namespace buffett        # 语义检索
python -m cli knowledge list --namespace buffett                    # 查看切片数
python -m cli knowledge clear --namespace buffett --force           # 清空命名空间

# MCP Server (standalone)
python -m agent.mcp.news_server                           # stdio mode (default)
python -m agent.mcp.news_server --transport sse --port 8001  # HTTP SSE mode

# Tests
pytest                                                           # unit tests only (default)
pytest -m integration                                            # integration tests
pytest tests/test_agent_agent.py                                 # agent unit tests
pytest tests/test_agent_tools.py                                 # tool tests
pytest tests/test_agent_memory.py                                # memory module tests
pytest tests/test_agent_memory_integration.py -m integration     # memory PG integration
pytest tests/test_agent_routes.py                                # agent API routes
pytest tests/test_agent_db.py                                    # agent DB operations
pytest --cov=. --cov-report=term-missing                         # coverage
```

## Architecture

NewsRadar is a **news aggregation system** with two operational modes sharing the same fetch logic:

- **Local daemon** (`main.py`) — PostgreSQL, FastAPI, background workers (signal-driven Event model)
- **Cloud CI** (`cli/` + GitHub Actions) — hourly crawl + 4× daily notify, SQLite → S3

### News fetch pipeline

```
Config sources ──► NewsnowFetcher (hot-list API) ──► NewsData ──► Storage backend
              ──► RssFetcher (RSS/Atom/JSON Feed) ──┘
                                                    └── Optional: trafilatura body + images
```

### Fetcher hierarchy

```
Fetcher (ABC, news/fetcher/fetcher.py)
├── NewsnowFetcher — NewsNow 热点列表 API，逐 source 并发拉取
└── RssFetcher     — RSS/Atom/JSON Feed，支持 If-Modified-Since
```

All fetchers return `list[dict]` — flat list of standardised item dicts. Failures are logged internally; fetchers never raise.

### OutputStyle 枚举

`OutputStyle`（`news/crawler.py:38`）决定 `Crawler.persist()` 的路由：

| 值 | 存储目标 | 场景 |
|----|----------|------|
| `POSTGRESQL` | PostgreSQL（UPSERT） | daemon 本地运行 |
| `SQLITE` | SQLite（按日分库 `output/news_YYYY-MM-DD.db`） | Cloud CI |
| `MARKDOWN` | 本地 `.md` 文件 | `grab-one` 调试 |

### Crawler: fetch_all() 完整流程

`Crawler.fetch_all()` 执行以下步骤（在 daemon 和 CLI 中共享）：

1. **Fetch** — NewsnowFetcher + RssFetcher 并发拉取
2. **Dedup** — `_dedup_items_by_url()` 按 URL 去重，同 URL 保留 priority 最高的来源
3. **Skip existing** — `_filter_existing_content_urls()` 查询 PG，跳过已有正文的 URL
4. **Enrich** — `enrich_content()` 双阶段管线：
   - Phase 1: `_run_batch_parse()` — ThreadPoolExecutor 并发下载 HTML → parse Markdown
   - Phase 2: `_run_batch_image_download()` — 收集图片 URL → 并发下载 → replace in-place
5. **Analyze** — sentiment（仅正文非空项）+ heat score（全部项）
6. **Persist** — `persist()` 按 OutputStyle 路由到对应后端
7. **Retry** （仅 daemon） — `retry_failed_tasks()` 重试之前失败的 content_fetch 和 image_download

### Cloud sync 流程

`Crawler.sync_from_cloud()` 将 CI 抓取的 SQLite 快照增量合并到 PG：
1. 查询 PG 中最新的 `crawled_from='cloud'` 记录的 `updated_at`
2. 列出 S3 `db/` 前缀下所有 `.db` 文件，过滤日期 >= 阈值
3. 逐日下载，按 `created_at` 过滤增量行，enrich → UPSERT（非 DO NOTHING，会刷新元数据）

### 失败重试系统

- `failed_tasks` 表记录失败的 content_fetch 和 image_download，带 `max_retry` 和 `retry_count`
- `retry_failed_tasks()` 查询 pending 任务，重试成功则标记 completed，失败则递增 retry_count
- image 重试必须在 content 重试 + persist 之后执行（需要文章已在 DB 中才能 UPDATE 图片路径）

### Key modules
- `news/crawler.py` — `Crawler` class. Public API: `fetch()`, `fetch_all()`, `enrich_content()`, `sync_from_cloud()`, `retry_failed_tasks()`. WAF 站点(huxiu/xueqiu/juejin)用 crawl4ai(undetected+magic+simulate_user)下载 HTML 绕过阿里云滑块,拿到 HTML 后走现有 parser;非 WAF 站点走 requests
- `news/models.py` — `NewsItem` / `NewsData` dataclasses. `NewsItem.ranks` = `[[rank, total], ...]` JSONB; `heat_score` = 0-100
- `news/fetcher/` — `Fetcher` ABC + `NewsnowFetcher` + `RssFetcher`
- `news/parser/` — HtmlParser + Registry + 12 site-specific parsers. Three-tier extraction: custom hook → readability → HTML-strip fallback
- `news/analyzer/` — `Analyzer` ABC + `JiebaAnalyzer` (heat + sentiment + keywords) + `AgentAnalyzer` (reserved)
- `news/images.py` — `ImageProcessor`: concurrent download → `FileStorage` backend
- `news/notifier.py` — HTML report + SMTP email
- `news/keywords.py` — parses `frequency_words.txt` format
- `news/constants.py` — tier labels/colors, source types, sentiment thresholds
- `utils.py` — time formatting (timezone-aware, default `Asia/Shanghai`), `normalize_url()`
- `config.py` — YAML + env vars, **env 优先级高于 YAML**

### Storage (详见 [docs/storage.md](docs/storage.md))

- **PostgreSQL** — canonical store. ThreadedConnectionPool, batch UPSERT, GIN+trigram CJK search. Two UPSERT templates: `_UPDATE_SET` (preserve content) / `_UPDATE_SET_OVERWRITE` (replace).
- **SQLite + S3** — Cloud CI backend. One `.db` per day, uploaded to S3.
- **Cloud sync** — downloads daily DBs, enriches content, UPSERTs into PG.
- **FileStorage** — ABC with Local/S3 implementations for article images.

### Analyzer (详见 [docs/analyzer.md](docs/analyzer.md))

`JiebaAnalyzer` provides three capabilities:
- **Heat Score**: percentile-based for new items, delta-adjusted for existing, decay for dropped. `_calc_heat_score()` is a pure static method. Config 参数：`half_life_hours`（衰减半衰期）、`tier_base`（各 tier 基础分）、`boost_cap`（各 tier 排名加成上限）。
- **Sentiment**: jieba tokenization + 4 dictionaries (positive/negative/negation/degree), tanh mapping to 0-100.
- **Keywords**: TF-IDF with custom IDF corpus from DB articles, TextRank fallback with POS filtering.

Config: `analyzer.enabled: true/false`, `analyzer.backend: jieba`.

### Web frontend (详见 [docs/web.md](docs/web.md))

FastAPI + Jinja2 SSR，路由：

| 路由 | 说明 |
|------|------|
| `GET /` | 市场概览（T1-T4 统计、热门来源） |
| `GET /hot-news` | 分页卡片流，URL-as-state 筛选（tier / sentiment / keyword / search / date） |
| `GET /news/{id}` | 新闻详情，Mistune GFM 渲染 |
| `GET /media/{path}` | 图片代理 — S3 presigned URL 重定向或本地文件 |
| `POST /api/trigger/crawl` | 手动触发抓取（409 如果正在运行），通过 SSE 推送通知 |
| `POST /api/trigger/sync` | 手动触发云端同步 |
| `POST /api/news/fetch` | 按 URL 提交后台抓取任务（refetch） |
| `GET /api/notifications/stream` | SSE 端点，推送实时通知更新 |

通知系统：内存存储（上限 50 条），重启后重置。

### Daemon (详见 [docs/daemon.md](docs/daemon.md))

**Channel 模式**（Go-style signal + data carrier）— 每个任务类型拥有独立的 `asyncio.Queue`：

```
Timer ──put(None)──▶ asyncio.Queue ◀──get── Worker ──job──▶
Manual trigger ──put(callback)──▶  (callback 用于 SSE 通知)
```

- **Timer** 每 N 分钟向 queue 放入 `None`（跳过通知）
- **Manual trigger**（Web API）向 queue 放入 callback（通过 SSE 推送完成通知）
- **Worker** 从 queue 取任务执行，`asyncio.Lock` 防止重复触发
- Blocking I/O 在 `ThreadPoolExecutor(max_workers=4)` 中执行
- 优雅关闭：signal → set event → cancel tasks → shutdown executor → close DB

启动序列（7 步）：DB init → signal handlers → web server → Workers → Timers → manual trigger（当前已注释）→ await shutdown。

## Agent Subsystem (`agent/`)

模块化 AI Agent 基座（Phase 1-3 完成）。详见 [docs/agent/architecture-v1.md](docs/agent/architecture-v1.md)。

### Architecture

```
DefaultAgent (编排器,构造时注入组件给 executor)
├── Brain (ModelHub) - LLM Client 池
│   ├── OpenAIClient      - OpenAI 兼容 API（含 tool calling）
│   └── AnthropicClient   - Anthropic API
├── Executor - 推理循环（构造注入 brain/memory/knowledge/tools,run(ctx) 只收数据）
│   ├── DirectExecutor    - 单次 chat 无工具（继承 ReActExecutor 共享 _prepare/_finalize,override _loop）
│   └── ReActExecutor     - ReAct 循环,三阶段 _prepare/_loop/_finalize
├── MemoryModule - 记忆系统（可选,load/save 接口）
│   ├── NullMemory        - 无记忆
│   ├── ShortTermMemory   - 持 db,load 历史对话(agent_messages 表)
│   └── LongTermMemory    - 继承 ShortTerm + agent_memories 检索/提炼
├── Registry - 工具注册中心
│   ├── FunctionTool      - 内置函数（@tool 装饰器）
│   └── MCPTool           - 外部 MCP 协议工具
├── ExecutorHook - 生命周期 hook（before_chat/after_chat/before_tool/after_tool/on_error）
└── Context - 数据总线（输入区 history_messages/memories + 执行区 messages/step_count + final_output property）
```

### Key patterns

- **@tool decorator** (`agent/tools/base.py`): 自动从类型注解 + Google-style docstring 生成 OpenAI format JSON Schema。支持 sync/async、默认值、可选参数。
- **Executor 三阶段** (`agent/executor.py`): `_prepare`(memory.load + knowledge.search + 组装 messages) -> `_loop`(LLM 推理 + 工具执行循环) -> `_finalize`(memory.save)。`run(ctx)` 只收数据,组件构造注入。DirectExecutor 继承 ReActExecutor 共享骨架,只 override `_loop`(单次 chat)。
- **流式必须绑 tools** (`agent/executor.py` `_loop_stream`): 流式路径每步 `chat_stream(tools=...)` 且始终绑定 tool schemas,`AIMessageChunk` 聚合(`+`)还原 tool_calls。**请求缺 tools 时 DeepSeek 会把工具调用意图写成 DSML 标记纯文本流给用户**(session 44 事故);`max_tool_rounds` 打满后摘 schemas 的同时注入收尾引导(临时 system 消息,不进 ctx.messages)同理。`_chunk_text` 兼容 OpenAI str content 与 Anthropic block 列表(thinking/text/tool_use)。
- **ExecutorHook**: 生命周期 hook(`before_chat`/`after_chat`/`before_tool`/`after_tool`/`on_error`),子类按需 override,默认 no-op。`approval_callback`/`_normalize_tool_result` 内置策略,不混入 hook。
- **鲁棒性**: LLM 调用重试(`llm_max_retries` + 退避)、单工具异常隔离(记 `ToolResult.error` 继续循环)、memory/knowledge 失败降级、整体 try/except 兜底(错误文本不进 messages,返回调用方)。executor 永不向调用方抛未捕获异常。
- **ToolResult**: 结构化记录(name/args/result/error/timing/retries/success),归 tool 消息 `tool_result` 字段(不存 ctx),供 troubleshooting + memory 消费。
- **Policy-based tool security**: 工具分 level 1-4，`running_mode`（strict/normal/loose）决定自动/需审批。
- **WebSocket 审批通道**: 前端通过 WebSocket 收 tool_approval_request，用户决定允许/拒绝，后端继续执行。
- **MCP Client**: 基于官方 mcp SDK 的 `ClientSession`，封装 stdio/SSE 传输层生命周期。

### MCP Server (`agent/mcp/`)

MCP 协议实现的新闻查询服务，两种传输模式：

- **stdio**: 子进程管道，Agent 启动时自动连接（`create_agent(register_mcp=True)`）
- **SSE**: HTTP 服务，外部 Agent 可通过 `MCPClient.connect_sse()` 连接

工具列表：`search_news` / `get_hot_topics` / `get_news_detail` / `analyze_sentiment` / `get_source_stats`

MCP Server 在 `news_server.py` 中使用 FastMCP，全局延迟初始化 DB 连接。

### Session 托管 (`web/agent.py`)

WebSocket 端点瘦身为纯传输层，对话任务由 `ChatSession` 托管，与 WS 连接解耦。三角色：`ChatSession`(会话，跨轮) > `ChatTask`(轮) > `asyncio.Task`；Agent 与 ChatTask 平行→session 同时管"谁在执行"(agent,保活)与"跑到哪了"(ChatTask,随轮生灭)。

| 组件 | 职责 |
|------|------|
| `ChatTask` | 单轮任务运行时：`task`(驱动协程句柄) + `buffer`(本轮 partial,先累积后广播,轮结束作废;完成态权威在 agent ctx/DB) + `subscribe()`/`broadcast()` 订阅通道 + `request_approval()`/`respond_approval()` 审批通道(120s 超时拒绝)。WS 断开只退订,不取消任务。 |
| `ChatSession` | 会话运行时：`agents`(多 agent 缓存) + `current_agent`/`current_key`(当前执行体) + `chat_task`(本轮任务,防重入:同时最多一轮) + `db`。`history_messages()` 就近读 `current_agent.get_conversation()`,None/异常/空兜底 DB;`destroy()` 取消任务 + 清空缓存。 |
| `_sessions` | 模块级 `dict[int, ChatSession]`。`get_session(sid, db)` 命中返回,未命中新建空壳(agent 惰性构建);`destroy_session(sid)` cancel 任务 + 清 agents + 移出表。 |
| `_drive` | 泵 `agent.chat_stream`:`buffer += token`(先) + `broadcast`(后,同一同步步骤→保证快照不重不漏)。`CancelledError`(用户 stop)→广播 `done(stopped)`;assistant 落库由 agent 内部 `_finalize`。 |
| `_forward` | 转发事件到 WS。`snapshot=True`(连接/重连):running 时先 `subscribe()` 再同步读 `buffer`(两步间无 await,原子快照)→发 `snapshot{messages, partial, running, agent}`→续推队列至 done/error;已完成任务不 replay(快照已含最终回复)。`snapshot=False`(新 chat)仅续推队列。 |
| `_start_chat` | 启动对话任务:防重入 + 复用/惰性构建 `current_agent`(无则构建默认 agent 并 `activate`)+ **接口层落库 user**(接收即存,不进 agent)+ 绑定审批通道。chat 路径零 agent 判断。 |
| `_switch_agent` | 显式切换(WS `switch` 消息,点击触发,只在轮间):freeze 旧→build/复用→activate 新(reload 全量历史,切回缓存 agent 不再 ctx 陈旧)→换 `current_agent`。生成中拒绝。 |

**写权限契约：** user 消息由接口层(`_start_chat`)接收即落库;assistant 回复 + 长期记忆由 agent 的 `memory.save`(`_finalize`,增量水位幂等)落库;读取开放(显示走 snapshot 就近读 agent、兜底 DB)。stop 中断时 executor 先把已流出 partial append 进 ctx 再 `_finalize`,不丢。

**重连续推流程：** WS 连接带 `?session_id=` 即发 `snapshot`(历史 + 运行中任务的 partial + 当前 agent key),运行中任务续推 token。WS 断开(`WebSocketDisconnect`)仅 cancel `forward_task`,**绝不取消 agent 任务或动 ChatSession**。

### Web Agent routes (`web/agent.py`)

| 路由 | 方法 | 说明 |
|------|------|------|
| `/agent` | GET | 聊天页面 |
| `/api/agent/sessions` | GET | 会话列表（分页） |
| `/api/agent/sessions` | POST | 新建会话（设 cookie） |
| `/api/agent/sessions/{session_id}` | DELETE | 删除会话 |
| `/api/agent/sessions/{session_id}/messages` | GET | 消息历史 |
| `/api/agents` | GET | 列出所有角色定义 |
| `/api/agents` | POST | 创建角色定义 |
| `/api/agents/{defn_id}` | GET | 查询角色定义 |
| `/api/agents/{defn_id}` | PUT | 更新角色定义 |
| `/api/agents/{defn_id}` | DELETE | 删除角色定义 |
| `/api/agent/knowledge` | GET | 列出所有知识库 |
| `/api/agent/knowledge` | POST | 创建知识库 |
| `/api/agent/knowledge/{knowledge_id}` | DELETE | 删除知识库（含切片） |
| `/api/agent/knowledge/{knowledge_id}/ingest` | POST | 上传文档到知识库（multipart） |
| `/api/tools` | GET | 列出所有可用工具 |

**WebSocket 端点：**

| 端点 | 说明 |
|------|------|
| `WS /api/agent/ws` | 统一聊天通道,`?session_id=` 连接即发 snapshot(历史 + 运行中任务续推) |

**WebSocket 协议：** 输入 `chat` / `switch` / `stop` / `tool_approval_response` 四种消息类型。`chat` 消息支持 `model`、`running_mode` 参数;`switch` 为点击触发的显式智能体切换(轮间,含 `agent_id`,空 = 默认助手)。输出事件:`snapshot`(连接快照:`messages`/`partial`/`running`/`agent`)、`token`(流式文本)、`done`(含 `full_reply`、`stopped`)、`switch_ack`、`error`(异常)、`tool_approval_request`(审批请求)。前端流式 markdown 用 rAF 节流增量渲染,历史渲染不再走 REST `loadMessages`(messages 端点保留作兜底/调试)。

### Web Settings routes (`web/settings.py`)

设置面板：HTML 页面（`/settings*`）+ 系统设置/新闻源/模型的 JSON API（只读，数据来自 `app.state.agent_config` 即 config.yaml）。写操作（POST/PUT/DELETE）当前为只读模式占位，已禁用。

| 路由 | 方法 | 说明 |
|------|------|------|
| `/api/settings` | GET | 系统配置（只读） |
| `/api/settings/sources` | GET | 列出新闻源（可过滤 source_type） |
| `/api/settings/sources` | POST | 新建新闻源（占位，已禁用） |
| `/api/settings/sources/{source_id}` | PUT | 更新新闻源（占位，已禁用） |
| `/api/settings/sources/{source_id}` | DELETE | 删除新闻源（占位，已禁用） |
| `/api/settings/sources/{source_id}/test` | POST | 测试 RSS 连通性 |
| `/api/settings/sources/seed` | POST | 从 config.yaml 种子（占位，已禁用） |
| `/api/models` | GET | 列出模型配置（隐藏 api_key） |
| `/api/models` | POST | 添加模型（占位，已禁用） |
| `/api/models/{model_name}` | PUT | 更新模型（占位，已禁用） |
| `/api/models/{model_name}` | DELETE | 删除模型（占位，已禁用） |

### Agent DB (PostgreSQL)

`storage/postgres.py` 包含 5 个 agent 相关方法：
- `create_agent_session()` / `get_agent_sessions()` / `delete_agent_session()`
- `save_agent_message()` / `get_agent_messages()`

表：`agent_sessions` / `agent_messages` / `agent_memories`（由 `LongTermMemory` 读写）

### Memory system

`MemoryModule` ABC 提供 `load(ctx)` / `save(ctx)` 接口,executor 在 `_prepare` 调 `load`(注入历史对话 + 长期记忆到 ctx)、`_finalize` 调 `save`(存当前对话 + 提炼长期记忆)。

| 记忆类型 | 存储 | load 行为 | save 行为 |
|---------|------|---------|---------|
| NullMemory | 无 | no-op | no-op |
| ShortTermMemory | PG (`agent_messages`) | load 历史对话 -> `ctx.messages` | 增量存 assistant(`messages[_saved_up_to:]`,水位为 memory 私有状态:load 建基线、save 逐条推进;user 由接口层落库) |
| LongTermMemory | PG (`agent_messages` + `agent_memories`) | 继承 ShortTerm + 检索 `agent_memories` -> `ctx.memories`(MemoryBlock) | 继承 ShortTerm + 提炼关键信息 -> `agent_memories` |

`ShortTermMemory` 持 db(构造注入),`web/agent.py` 不再自己加载 history,统一靠 `memory.load`。`PgMemoryStorage` 通过 `asyncio.to_thread` 桥接同步 psycopg2 到异步接口。CJK 搜索拆单字 ILIKE，ASCII 搜索用 PG `to_tsvector`。

### Knowledge Base (`agent/knowledge/`) — Phase A

pgvector 语义检索知识库，作为角色扮演 agent 的专业知识来源。详见 [docs/agent/phase3-knowledge.md](docs/agent/phase3-knowledge.md)。

| 模块 | 职责 |
|------|------|
| `engine.py` | `KnowledgeEngine`：文档切片 -> embedding -> 存 pgvector -> 语义检索 -> `retrieve_render()` 文本块 |
| `embedding.py` | `EmbeddingClient`：OpenAI 兼容 `/v1/embeddings`（独立 base_url/api_key，DeepSeek 无 embedding 端点） |
| `chunker.py` | 段落 + 长度切片（~512 token，重叠 64） |
| `store.py` | `KnowledgeStore` ABC + `PgVectorKnowledgeStore`（委托 `storage/postgres.py`） |

- **表**：`knowledge_chunks`（`vector(N)` + HNSW `vector_cosine_ops` 索引），namespace 隔离（`investing/buffett`、`macro-economics`…）。DDL 在 `storage/postgres.py:_init_agent_schema()`，需 `pgvector/pgvector:pg16` 镜像。
- **注入**：`KnowledgeEngine.search(ctx)` 由 executor 在 `_prepare` 调用 -> `ctx.memories.append(MemoryBlock(source="knowledge", order=20))`。`_build_llm_messages` 按 order 拼成 `## 知识库` system 块。KnowledgeEngine 构造时绑定 `namespace`。
- **配置**：`knowledge:` 段（`enabled`/`embedding_*`/`top_k`/`table`）。`enabled: false` 时 KnowledgeEngine=None，角色退化为纯 prompt。
- **CLI**：`python -m cli knowledge ingest <file> --namespace buffett` / `search` / `list` / `clear`。

### Factory

`agent/factory.create_agent()` 一键构建 Agent（ReActExecutor 构造注入 brain/memory/knowledge/tools + 内置工具 + MCP 工具）。`db` 参数驱动持久化（传 db -> `ShortTermMemory(db)`；不传 -> `NullMemory`）：

```python
agent = await create_agent(
    config["agent"]["models"],
    system_prompt="你是 NewsRadar 新闻助手",
    register_mcp=True,
    mcp_cfg=config["mcp_server"],
    db=db,
)
```

Daemon 启动时启动 MCP Server 并构建 `AgentFactory`（`agent.models` 必填，缺失时 load_config 报错），通过 `app.state.agent_factory` / `app.state.base_prompt` 注入 Web 应用。聊天室 agent 由 `_build_chat_agent` 在首个请求时 per-session 惰性构建。

`AgentFactory.build(defn)` 从 `AgentDefinition` 构建：`LongTermMemory(db, PgMemoryStorage(db))` + `KnowledgeEngine(namespace=kb.namespace)` + `ReActExecutor(brain, memory, knowledge, tools)`。

### Agent 使用场景与隔离约定（重要）

NewsRadar 的 agent 仅用于 **AI 聊天室**场景：每个 session **严格隔离**，各自独立 build agent（无 `agent_id` 时由 `_build_chat_agent` 构建聊天室默认 agent，有 `agent_id` 时走 `AgentFactory.build`），session 间互不影响。切换智能体走 WS `switch` 消息（`_switch_agent`：freeze 旧→activate 新），不再经 WS query param。Agent 跟随 session 生命周期（session 不删除则 agent 不清理）。

不再有页面级全局 agent 单例——原 `app.state.agent_instance` 预留位已移除（无后续实现需求）。

**`default`/`quick`/`deep` 是 `agent.models` 配置的模型选择 key**（不同模型/速度档位），**不是 agent 类型**，不要与 agent 概念混淆。聊天室由 `agent.default_model` 指定默认模型。

## Web refactoring (`web/`)

Web 模块从单个 `web/app.py` 拆分为多文件结构：

| 文件 | 职责 |
|------|------|
| `app.py` | FastAPI 工厂函数 `create_app()`，组装 lifespan / static / state / routers |
| `news.py` | 新闻相关路由（hot-news / detail / trigger / refetch / SSE / sentiment） |
| `agent.py` | Agent 聊天路由 + WebSocket |
| `settings.py` | 设置面板路由：HTML 页面（/settings*）+ 系统设置/新闻源/模型 JSON API（/api/settings*, /api/models*） |
| `background.py` | `BackgroundTaskRunner` — 通用后台任务执行器（ThreadPoolExecutor） |
| `notification.py` | `NotificationState` — 内存通知系统 + SSE 分发 |
| `config.py` | 模板渲染配置（Jinja2） |

`BackgroundTaskRunner` 是零业务依赖的通用任务执行器，提供 dedup（by task_id）、状态追踪、生命周期管理。Crawler refetch 和 URL fetch 都使用它。

`NotificationState` 线程安全，支持 scope/category 过滤、未读计数、SSE 广播。上限 50 条。

## Parser Registry

Three-tier routing in `news/parser/registry.py`: source_id exact match → URL hostname domain match → default `HtmlParser`.

To add a new site parser:
1. Create `news/parser/sites/<site>.py` — subclass `HtmlParser`, override `_extract()` and/or `_preprocess()`
2. Register in `news/parser/sites/__init__.py` via `registry.register(source_id, parser, domains=[...])`

## Refetch behavior ⚠️

`_download_and_parse()` overwrites ALL metadata fields directly — no manual clearing needed. **Only `content` must be cleared first** because `_run_batch_parse()` skips items that already have content.

## Tests

39 test files in `tests/`. Site-specific parser tests use real HTML fixtures in `tests/parser_sites/`. Shared fixtures in `conftest.py` and `conftest_db.py`.

### Agent test files

| 文件 | 覆盖内容 |
|------|---------|
| `test_agent_agent.py` | DefaultAgent 构造/chat/chat_stream，DirectExecutor 运行，ModelHub 多模型 |
| `test_agent_tools.py` | @tool 装饰器、FunctionTool、Registry、MCPTool、schema 生成 |
| `test_agent_memory.py` | NullMemory / ShortTermMemory / LongTermMemory / PgMemoryStorage |
| `test_agent_memory_integration.py` | PG 记忆持久化集成测试 |
| `test_agent_routes.py` | Web agent 路由（页面 / sessions / messages API） |
| `test_agent_session.py` | ChatTask 订阅/广播/审批、history_messages 就近+兜底、_drive 三种收尾、_forward snapshot 语义、_start_chat user 落库/防重入、_switch_agent freeze/activate、WS 集成 |
| `test_agent_data.py` | AgentDefinition / AgentKnowledge dataclass 序列化 |
| `test_agent_factory.py` | AgentFactory.build 从 AgentDefinition 构建完整 agent |
| `test_agent_db.py` | Agent DB 操作（create/get/delete session, save/get messages） |
| `test_knowledge_engine.py` | KnowledgeEngine 单元（mock embedding + store）+ pgvector 集成 |
| `test_mcp_news_server.py` | MCP `analyze_sentiment` 路由真分析器 + 兜底词典 |

### 运行

```bash
pytest                                          # 默认跳过集成测试
pytest -m integration                           # 仅集成测试（需要 PostgreSQL/MinIO/httpbin.org）
pytest -m "not integration"                     # 仅单元测试（与默认行为相同）
pytest tests/test_parser.py::TestTrimNoise::test_trims_footer_copyright -v
pytest --cov=. --cov-report=term-missing
```

默认 `addopts = "-m 'not integration'"`（见 `pyproject.toml`），因此 `pytest` 会跳过标记为 `integration` 的测试。

### DB 测试模式

`tests/conftest_db.py` 提供 mock PostgreSQL 连接链：`db` → `mock_pool` → `mock_conn` → `mock_cursor`。`capture_sql(mock_cursor)` 工具函数从最后一次 `execute()` 调用中提取 `(sql_template, params_tuple)`，用于断言生成的 SQL 而非 mock 返回值：

```python
def test_xxx(db, mock_cursor):
    mock_cursor.fetchone.return_value = [42]
    result = db.get_news_count()
    sql, params = capture_sql(mock_cursor)
    assert "COUNT(*)" in sql
```

### Agent 测试模式

`test_agent_agent.py` 使用 `MockClient(BaseClient)` 模拟 LLM 响应，不调真实 API：

```python
mock_client = MockClient(api_key="test")
mock_client.tool_calls_to_return = [...]  # 模拟工具调用
hub = ModelHub({"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "test"}})
hub._clients["default"] = mock_client  # 注入 mock
```

`test_agent_tools.py` 测试 @tool 装饰器的 schema 自动生成（类型注解 → JSON Schema）、工具注册/去重/移除、执行 sync/async 工具。

### Parser 站点测试

`tests/parser_sites/` 下每个文件测试一个站点解析器。`tests/parser_sites/test_framework.py` 包含 30 个通用解析器测试（标题提取、正文提取、边界情况等），使用参数化 fixture 对多个站点执行。`tests/helpers.py` 提供共享测试工具。

## Key env vars

`PG_*`, `CLOUD_S3_*` (SQLite transfer), `RESOURCE_S3_*` (images/MinIO), `EMAIL_*`, `AI_API_*` (LLM，预留给 AgentAnalyzer), `WEB_HOST/PORT`, `CONFIG_PATH`.

Agent 依赖 `config.yaml` 的 `agent.models` 段，通过环境变量 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 注入 API key：

```yaml
agent:
  models:
    default:
      protocol: openai            # openai | anthropic
      model: gpt-4o-mini
      api_key: ${OPENAI_API_KEY}
    quick:
      protocol: openai
      model: gpt-4o-mini
      api_key: ${OPENAI_API_KEY}
```

Daemon 启动时构建 Agent（`agent.models` 必填）。Agent 路由始终注册。
