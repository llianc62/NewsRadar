# NewsRadar — 新闻聚合与智能通知系统

NewsRadar 是一个多源新闻聚合系统，支持热点榜单、RSS/Atom/JSON Feed 抓取，提供正文提取、情感分析、热度评分、关键词匹配和邮件通知等能力。系统有两种运行模式：

- **本地常驻**（`main.py`）— PostgreSQL + FastAPI Web 面板，适合长期部署
- **云端 CI**（GitHub Actions）— SQLite + S3，每小时抓取 + 每日 4 次邮件通知，零服务器成本

## 功能概览

- **多源抓取** — 支持 NewsNow 热点 API 和 RSS/Atom/JSON Feed，覆盖 30+ 新闻源（华尔街见闻、财联社、新华社、澎湃新闻、36氪等）
- **正文提取** — 三级降级流水线：站点定制解析器 → readability → 裸 HTML 剥离，适配 12 个站点
- **内容富化** — 自动下载正文 + 图片，并发处理，支持失败重试
- **热度评分** — 百分位 + 增量衰减算法，0-100 热力值追踪新闻生命周期
- **情感分析** — jieba 分词 + 四组词典规则引擎，0-100 情感评分
- **关键词提取** — TF-IDF（自定义 IDF 语料）优先，TextRank 兜底
- **邮件通知** — 关键词匹配 → HTML 报告 → SMTP 发送，SMTP 自动探测
- **Web 面板** — FastAPI + Jinja2 SSR，市场概览、分页卡片流、多维度筛选、全文搜索
- **双存储** — PostgreSQL（本地）和 SQLite+S3（云端），共享抓取逻辑
- **云端同步** — daemon 启动时从 S3 拉取 CI 抓取的快照，补下载正文后合并到 PG

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

## 快速开始

### 环境要求

- Python >= 3.12
- Docker（本地 PostgreSQL + MinIO）

### 安装

```bash
# 安装依赖
uv sync
uv pip install pytest  # pytest 不在 pyproject.toml 中

# 启动基础设施（PostgreSQL 16 + MinIO）
docker compose up -d

# 复制并编辑环境变量
cp env.example .env
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
```

### 运行测试

```bash
pytest
pytest --cov=. --cov-report=term-missing
pytest tests/test_parser.py::TestTrimNoise::test_trims_footer_copyright -v
```

## 配置

配置文件为 `config.yaml`，环境变量优先级更高（12-factor 风格）。详细说明见 `env.example`。

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
```

## 项目结构

```
NewsRadar/
├── main.py                  # Daemon 入口（FastAPI + 定时任务）
├── config.yaml              # 默认配置文件
├── config/
│   └── loader.py            # 配置加载（YAML + env 合并）
├── news/
│   ├── crawler.py           # Crawler — fetch→enrich→analyze→persist 编排
│   ├── models.py            # NewsItem / NewsData dataclass
│   ├── constants.py         # Tier / source type / sentiment 常量
│   ├── keywords.py          # frequency_words.txt 解析 + 关键词匹配
│   ├── notifier.py          # HTML 报告 + SMTP 邮件发送
│   ├── images.py            # ImageProcessor — 并发下载 + 路径替换
│   ├── utils.py             # 时间格式化、URL 规范化
│   ├── fetcher/
│   │   ├── newsnow.py       # NewsnowFetcher — 热点列表 API
│   │   └── rss.py           # RssFetcher — RSS/Atom/JSON Feed
│   ├── parser/
│   │   ├── parser.py        # HtmlParser 基类 — 三级提取流水线
│   │   ├── registry.py      # Registry — source_id/域名 三级路由
│   │   └── sites/           # 12 个站点定制解析器
│   └── analyzer/
│       ├── analyzer.py      # Analyzer 抽象基类
│       ├── jieba.py         # JiebaAnalyzer — heat + sentiment + keywords
│       └── agent.py         # AgentAnalyzer 预留桩
├── storage/
│   ├── postgres.py          # PostgreSQL 后端（连接池 + UPSERT + 全文搜索）
│   ├── sqlite.py            # SQLite 后端（按日分库）
│   ├── s3.py                # S3/MinIO 客户端
│   └── files.py             # FileStorage ABC（Local + S3 实现）
├── web/
│   ├── app.py               # FastAPI 应用工厂 + 全部路由
│   └── templates/           # Jinja2 模板
├── cli/                     # CLI 入口（crawl / notify / grab-one / db）
├── data/                    # 情感词典 + IDF 语料
├── tests/                   # 25+ 测试文件
├── docs/                    # 模块设计文档
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

## License

MIT
