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

历史开发记录在 `docs/superpowers/`（plans + specs），不主动加载。

## CLI 模块结构

CLI 使用 Typer 框架，入口在 `cli/__init__.py`：

```
cli/
├── __init__.py   # Typer app 创建 + 子命令注册
├── crawl.py      # python -m cli crawl — 抓取 → SQLite
├── notify.py     # python -m cli notify — 关键词匹配 → 邮件
├── grab.py       # python -m cli grab-one — 单 URL 测试
└── db.py         # python -m cli db clear — 数据库维护
```

`python -m cli` 自动路由到 `cli.__main__`，后者调用 `cli.app()`。

## 环境设置

```bash
# 1. 安装依赖 (Python >= 3.12)
uv sync
uv pip install pytest  # pytest 不在 pyproject.toml 中

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
python -m cli db clear --before 2026-06-01 --force
python -m cli db clear --all --force

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
- `news/crawler.py` — `Crawler` class. Public API: `fetch()`, `fetch_all()`, `enrich_content()`, `sync_from_cloud()`, `retry_failed_tasks()`
- `news/models.py` — `NewsItem` / `NewsData` dataclasses. `NewsItem.ranks` = `[[rank, total], ...]` JSONB; `heat_score` = 0-100
- `news/fetcher/` — `Fetcher` ABC + `NewsnowFetcher` + `RssFetcher`
- `news/parser/` — HtmlParser + Registry + 12 site-specific parsers. Three-tier extraction: custom hook → readability → HTML-strip fallback
- `news/analyzer/` — `Analyzer` ABC + `JiebaAnalyzer` (heat + sentiment + keywords) + `AgentAnalyzer` (reserved)
- `news/images.py` — `ImageProcessor`: concurrent download → `FileStorage` backend
- `news/notifier.py` — HTML report + SMTP email
- `news/keywords.py` — parses `frequency_words.txt` format
- `news/constants.py` — tier labels/colors, source types, sentiment thresholds
- `utils.py` — time formatting (timezone-aware, default `Asia/Shanghai`), `normalize_url()`
- `config/loader.py` — YAML + env vars, **env 优先级高于 YAML**

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

## Parser Registry

Three-tier routing in `news/parser/registry.py`: source_id exact match → URL hostname domain match → default `HtmlParser`.

To add a new site parser:
1. Create `news/parser/sites/<site>.py` — subclass `HtmlParser`, override `_extract()` and/or `_preprocess()`
2. Register in `news/parser/sites/__init__.py` via `registry.register(source_id, parser, domains=[...])`

## Refetch behavior ⚠️

`_download_and_parse()` overwrites ALL metadata fields directly — no manual clearing needed. **Only `content` must be cleared first** because `_run_batch_parse()` skips items that already have content.

## Tests

25+ test files in `tests/`. Site-specific parser tests use real HTML fixtures in `tests/parser_sites/`. Shared fixtures in `conftest.py` and `conftest_db.py`.

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

### Parser 站点测试

`tests/parser_sites/` 下每个文件测试一个站点解析器。`tests/parser_sites/test_framework.py` 包含 30 个通用解析器测试（标题提取、正文提取、边界情况等），使用参数化 fixture 对多个站点执行。`tests/helpers.py` 提供共享测试工具。

## Key env vars

`PG_*`, `CLOUD_S3_*` (SQLite transfer), `RESOURCE_S3_*` (images/MinIO), `EMAIL_*`, `AI_API_*` (LLM，预留给 AgentAnalyzer), `WEB_HOST/PORT`, `CONFIG_PATH`.
