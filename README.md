# NewsRadar — 新闻聚合与智能通知系统

NewsRadar 是一个多源新闻聚合系统，支持热点榜单、RSS/Atom/JSON Feed 抓取，提供正文提取、情感分析、热度评分、关键词匹配和邮件通知等能力。系统有两种运行模式：

- **本地常驻**（`main.py`）— PostgreSQL + FastAPI Web 面板，适合长期部署
- **云端 CI**（GitHub Actions）— SQLite + S3，每小时抓取 + 每日 4 次邮件通知，零服务器成本

## 功能概览

- **多源抓取** — 支持 NewsNow 热点 API 和 RSS/Atom/JSON Feed，覆盖 30+ 新闻源（华尔街见闻、财联社、新华社、澎湃新闻、36氪等）
- **正文提取** — 三级降级流水线：站点定制解析器 → readability → 裸 HTML 剥离，24 个站点解析器 / 29 条 source_id 注册
- **内容富化** — 自动下载正文 + 图片，并发处理，支持失败重试
- **热度评分** — 百分位 + 增量衰减算法，0-100 热力值追踪新闻生命周期
- **情感分析** — jieba 分词 + 四组词典规则引擎，0-100 情感评分
- **关键词提取** — TF-IDF（自定义 IDF 语料）优先，TextRank 兜底
- **邮件通知** — 关键词匹配 → HTML 报告 → SMTP 发送，SMTP 自动探测
- **Web 面板** — FastAPI + Jinja2 SSR，市场概览、分页卡片流、多维度筛选、全文搜索
- **双存储** — PostgreSQL（本地）和 SQLite+S3（云端），共享抓取逻辑
- **云端同步** — daemon 启动时从 S3 拉取 CI 抓取的快照，补下载正文后合并到 PG
- **AI 聊天 Agent** — ReAct 推理循环 + 多角色定义，支持工具调用、流式回复、WebSocket 审批
- **记忆系统** — 短期对话记忆（PostgreSQL）+ 长期记忆提炼与检索
- **知识库检索** — pgvector 语义检索，文档切片入库，embedding 走本地 Ollama（`nomic-embed-text`，768 维）或任意 OpenAI 兼容 `/v1/embeddings` 服务，作为角色专业知识的来源
- **MCP Server** — 暴露新闻查询工具（`search_news` / `get_hot_topics` / `get_news_detail` / `analyze_sentiment` / `get_source_stats`），支持 stdio / SSE 两种传输

## 架构

```
配置源 ──► NewsnowFetcher（热点 API）──► NewsData ──► Storage
       ──► RssFetcher（RSS/Atom）  ──┘            ├── PostgreSQL（本地）
                                                   └── SQLite + S3（云端）
                         │
                         ▼
                  enrich_content()
                  并发下载正文 + 图片
                         │
                         ▼
                   Analyzer（可选）
                  heat + sentiment + keywords
                         │
                         ▼
                     Notifier
                  关键词匹配 → HTML 报告 → 邮件
```

### AI Agent 子系统（独立于抓取管线）

`DefaultAgent` 编排：**Brain**（ModelHub 多模型池）→ **Executor**（ReAct 推理循环）→ **Memory**（短期/长期记忆）+ **Knowledge**（pgvector 检索）+ **Tools**（内置 + MCP 外部工具）。详见 [docs/agent/architecture-v1.md](docs/agent/architecture-v1.md)。

知识库检索需要独立的 embedding 服务（DeepSeek 无 embedding 端点）：默认走本地 Ollama 的 `nomic-embed-text`（768 维），也可把 `KNOWLEDGE_EMBEDDING_BASE_URL` 指向任意 OpenAI 兼容 `/v1/embeddings` 服务。向量维度在建表时固化，换模型需重建 `knowledge_chunks`。

## 快速开始

### 环境要求

- Python 3.13（`.python-version` 已钉住；3.14/3.15 暂无 `psycopg2-binary` wheel）
- Docker（本地 PostgreSQL 16 + MinIO；compose 里的对象存储由 **Silo** 提供 —— MinIO 社区版已归档、官方镜像下架，Silo 是其社区 fork，S3 API / `MINIO_*` 环境变量 / `.minio.sys` 磁盘格式全兼容）
- Ollama（知识库 embedding，`knowledge.enabled: true` 时必需）

### 安装

```bash
# 1. Python 依赖
uv sync
uv pip install pytest  # pytest 不在 pyproject.toml 中
crawl4ai-setup  # 安装 patchright 浏览器（WAF 站点抓取用）

# 2. 基础设施（PostgreSQL 16 + MinIO）
docker compose up -d

# 3. 环境变量（config.py 在 import 时自动加载 .env，不需要 source）
cp env.example .env
# 编辑 .env：EMAIL_*、S3、MODELS_*_API_KEY、KNOWLEDGE_EMBEDDING_*

# 4. Ollama + embedding 模型（知识库用；不用知识库可跳过并设 KNOWLEDGE_ENABLED=false）
brew install ollama
brew services start ollama        # 常驻 :11434
ollama pull nomic-embed-text      # 768 维，需与 KNOWLEDGE_EMBEDDING_DIM 一致
```

### 本地运行

```bash
# 启动 daemon（PostgreSQL + FastAPI + 定时抓取）
python main.py

# 访问 Web 面板
# http://localhost:8000
```

### CLI 命令

```bash
# 抓取新闻（SQLite 模式，用于 CI）
python -m cli crawl

# 发送通知邮件
python -m cli notify

# 单 URL 测试正文提取
python -m cli grab-one "https://example.com" --output-style markdown
python -m cli grab-one "https://example.com" --output-style postgresql --images

# 数据库维护
python -m cli db clear --start "2026-07-02" --end "2026-07-04" --force
python -m cli db clear --all --force

# 知识库（pgvector，需 knowledge.enabled: true + embedding key）
python -m cli knowledge ingest path/to/doc.md --namespace buffett   # 导入文档
python -m cli knowledge search "查询语句" --namespace buffett        # 语义检索
python -m cli knowledge list --namespace buffett                    # 查看切片数
python -m cli knowledge clear --namespace buffett --force           # 清空命名空间

# Agent 定义管理
python -m cli agent create --input agent/agents/buffett.md   # 导入角色定义
python -m cli agent list                                     # 列出所有定义
python -m cli agent delete <id> --force                      # 删除定义

# MCP Server（独立模式）
python -m agent.mcp.news_server                            # stdio（默认）
python -m agent.mcp.news_server --transport sse --port 8001  # HTTP SSE
```

### 运行测试

```bash
pytest
pytest --cov=. --cov-report=term-missing
pytest tests/test_parser.py::TestTrimNoise::test_trims_footer_copyright -v
```

## 配置

配置分三层，优先级从高到低：**shell 环境变量 > `.env` > `config.yaml`**（12-factor 风格）。`.env` 由 `config.py` 在 import 时 `load_dotenv(override=False)` 自动加载，`main.py`、`python -m cli`、MCP server 等所有入口共用同一份配置，无需手动 `source`。也可用 `CONFIG_PATH` 指定另一个 YAML。详细说明见 `env.example`。

### 主要配置项

| 配置段 | 说明 |
|--------|------|
| `app.timezone` | 时区，默认 `Asia/Shanghai` |
| `crawler` | 抓取间隔、超时、并发数，`newsnow`/`rss` 子段控制新闻源 |
| `notification` | 通知词文件、邮件配置 |
| `storage.cloud` | S3 配置（SQLite DB 传输），env: `CLOUD_S3_*` |
| `storage.resource` | S3/MinIO 配置（图片/文件），env: `RESOURCE_S3_*` |
| `postgresql` | PostgreSQL 连接信息，env: `PG_*` |
| `analyzer` | 分析引擎开关与后端选择（`jieba`） |
| `agent` | AI Agent 模型档位（`default`/`quick`/`deep`，是模型选择 key 而非 agent 类型），env: `MODELS_<NAME>_{MODEL,BASE_URL,API_KEY}` |
| `knowledge` | 知识库开关与 embedding 配置（`enabled`/`embedding_*`/`top_k`），env: `KNOWLEDGE_*`；本机 embedding 走 Ollama |
| `web` | FastAPI 监听地址与端口，env: `WEB_*` |

### 关键环境变量

```bash
# Email
EMAIL_FROM_ADDR    # 发件人地址
EMAIL_TO_ADDR      # 收件人地址
EMAIL_PASSWORD     # SMTP 授权码

# S3（SQLite 传输）
CLOUD_S3_ENDPOINT_URL
CLOUD_S3_BUCKET_NAME
CLOUD_S3_ACCESS_KEY_ID
CLOUD_S3_SECRET_ACCESS_KEY
CLOUD_S3_REGION

# S3（图片/资源）
RESOURCE_S3_ENDPOINT_URL
RESOURCE_S3_BUCKET_NAME
RESOURCE_S3_ACCESS_KEY_ID
RESOURCE_S3_SECRET_ACCESS_KEY

# PostgreSQL
PG_HOST / PG_PORT / PG_DATABASE / PG_USER / PG_PASSWORD

# LLM（agent.models 的 env 覆盖，每档一组）
MODELS_DEFAULT_MODEL / MODELS_DEFAULT_BASE_URL / MODELS_DEFAULT_API_KEY
MODELS_QUICK_MODEL   / MODELS_QUICK_BASE_URL   / MODELS_QUICK_API_KEY
MODELS_DEEP_MODEL    / MODELS_DEEP_BASE_URL    / MODELS_DEEP_API_KEY

# 知识库 embedding（默认指向本地 Ollama）
KNOWLEDGE_ENABLED
KNOWLEDGE_EMBEDDING_BASE_URL   # http://localhost:11434/v1
KNOWLEDGE_EMBEDDING_API_KEY    # Ollama 不需要真实 key，填任意非空值
KNOWLEDGE_EMBEDDING_MODEL      # nomic-embed-text
KNOWLEDGE_EMBEDDING_DIM        # 768，必须与建表时的向量维度一致
KNOWLEDGE_TOP_K
```

## 项目结构

```
NewsRadar/
├── main.py                  # Daemon 入口（FastAPI + 定时任务）
├── config.py                # 配置加载（.env + YAML + env 合并，env 优先）
├── config/                  # 运行时配置与数据
│   ├── config.yaml          # 默认配置文件
│   └── frequency_words.txt  # 通知关键词词表
├── env.example              # 环境变量模板（cp 成 .env）
├── .python-version          # 钉住 Python 3.13
├── docker-compose.yml       # PostgreSQL(pgvector) + MinIO
├── utils.py                 # 时间格式化、URL 规范化
├── news/
│   ├── crawler.py           # Crawler — fetch→enrich→analyze→persist 编排
│   ├── models.py            # NewsItem / NewsData dataclass
│   ├── constants.py         # Tier / source type / sentiment 常量
│   ├── keywords.py          # frequency_words.txt 解析 + 关键词匹配
│   ├── notifier.py          # HTML 报告 + SMTP 邮件发送
│   ├── images.py            # ImageProcessor — 并发下载 + 路径替换
│   ├── fetcher/
│   │   ├── fetcher.py       # Fetcher ABC
│   │   ├── newsnow.py       # NewsnowFetcher — 热点列表 API
│   │   └── rss.py           # RssFetcher — RSS/Atom/JSON Feed
│   ├── parser/
│   │   ├── parser.py        # HtmlParser 基类 — 三级提取流水线
│   │   ├── registry.py      # Registry — source_id/域名 三级路由
│   │   └── sites/           # 24 个站点定制解析器（29 条注册）
│   └── analyzer/
│       ├── analyzer.py      # Analyzer 抽象基类
│       ├── jieba.py         # JiebaAnalyzer — heat + sentiment + keywords
│       └── agent.py         # AgentAnalyzer 预留桩
├── storage/
│   ├── postgres.py          # PostgreSQL 后端（连接池 + UPSERT + 全文搜索 + Agent 表）
│   ├── postgres.sql         # PG schema DDL
│   ├── sqlite.py            # SQLite 后端（按日分库）
│   ├── sqlite.sql           # SQLite schema DDL
│   ├── s3.py                # S3/MinIO 客户端
│   └── files.py             # FileStorage ABC（Local / S3 实现）
├── agent/                   # AI Agent 子系统
│   ├── Agent.md             # 系统级基座提示词
│   ├── agent.py             # DefaultAgent 编排器
│   ├── executor.py          # ReAct / Direct 推理循环
│   ├── model_hub.py         # ModelHub 多模型池
│   ├── memory.py            # MemoryModule（短期/长期记忆）
│   ├── factory.py           # create_agent / AgentFactory
│   ├── data.py              # AgentDefinition / AgentKnowledge
│   ├── llm/                 # LLM 客户端（base / openai / anthropic / deepseek）
│   ├── tools/               # @tool 装饰器 + Registry + 内置工具
│   ├── knowledge/           # pgvector 知识库（chunker / embedding / engine / store）
│   ├── mcp/                 # MCP Server（news_server）+ Client + MCPTool
│   └── agents/              # 9 个角色定义 markdown
├── web/
│   ├── app.py               # FastAPI 应用工厂（lifespan / state / routers）
│   ├── news.py              # 新闻路由（hot-news / detail / trigger / refetch / SSE）
│   ├── agent.py             # Agent 聊天路由 + WebSocket
│   ├── settings.py          # 设置面板路由
│   ├── background.py        # BackgroundTaskRunner 通用后台任务执行器
│   ├── notification.py      # NotificationState — 内存通知 + SSE
│   ├── config.py            # Jinja2 模板渲染配置
│   ├── static/              # 静态资源（CSS / JS）
│   └── templates/           # Jinja2 模板
├── cli/                     # CLI 入口（crawl / notify / grab / db / knowledge / agent）
├── data/                    # 情感词典（正/负/否定/程度，IDF 语料从 DB 统计）
├── scripts/                 # 一次性数据迁移脚本（fonts / tag hash / tier）
├── tests/                   # 46 个测试文件 + parser_sites/（20 个站点 fixture）
├── docs/                    # 模块设计文档（含 docs/agent/ AI 子系统设计、docs/bugs/）
└── .github/workflows/       # CI 工作流（crawler.yml + notifier.yml）
```

## 扩展开发

### 添加新站点解析器

1. 创建 `news/parser/sites/<site>.py`，继承 `HtmlParser`，覆写 `_extract()` 和/或 `_preprocess()`
2. 在 `news/parser/sites/__init__.py` 中注册：
   ```python
   registry.register("source_id", SiteParser(), domains=["example.com"])
   ```
3. 在 `tests/parser_sites/` 添加真实 HTML fixture 测试

详见 [docs/parser.md](docs/parser.md)。

## 文档

| 文档 | 内容 |
|------|------|
| [docs/crawler.md](docs/crawler.md) | 爬取管线、内容富化、失败重试 |
| [docs/parser.md](docs/parser.md) | HTML 提取流水线 + Registry 路由 |
| [docs/analyzer.md](docs/analyzer.md) | 热度评分 + 情感分析 + 关键词提取 |
| [docs/storage.md](docs/storage.md) | PostgreSQL / SQLite+S3 双存储 |
| [docs/web.md](docs/web.md) | FastAPI 前端 + 通知系统 |
| [docs/daemon.md](docs/daemon.md) | 后台调度 + 启动序列 |
| [docs/agent/architecture-v1.md](docs/agent/architecture-v1.md) | AI Agent 子系统（Phase 1-3） |
| [docs/agent/configuration.md](docs/agent/configuration.md) | Agent 配置参考 |
| [docs/agent/phase0-chat.md](docs/agent/phase0-chat.md) | 聊天 Agent 设计 |

## License

MIT
