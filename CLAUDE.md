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

## Commands

```bash
# Local daemon — PostgreSQL + FastAPI web dashboard
python main.py

# Cloud CI (GitHub Actions) — fetch + SQLite + S3
python -m cli crawl
python -m cli notify

# Test content extraction on a single URL
python -m cli grab-one "https://example.com" --output-style markdown
python -m cli grab-one "https://example.com" --output-style postgresql --images

# Database maintenance
python -m cli db clear --before 2026-06-01 --force
python -m cli db clear --all --force

# Install dependencies (requires Python >= 3.12)
uv sync
uv pip install pytest  # pytest NOT in pyproject.toml

# Docker infrastructure (PostgreSQL 16 + MinIO)
docker compose up -d

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
- **Heat Score**: percentile-based for new items, delta-adjusted for existing, decay for dropped. `_calc_heat_score()` is a pure static method.
- **Sentiment**: jieba tokenization + 4 dictionaries (positive/negative/negation/degree), tanh mapping to 0-100.
- **Keywords**: TF-IDF with custom IDF corpus from DB articles, TextRank fallback with POS filtering.

Config: `analyzer.enabled: true/false`, `analyzer.backend: jieba`.

### Web frontend (详见 [docs/web.md](docs/web.md))

FastAPI + Jinja2 SSR. `/` (overview), `/hot-news` (paginated cards, URL-as-state filters), `/news/{id}` (detail). Mistune GFM rendering. Notifications in-memory (capped 50, resets on restart).

### Daemon (详见 [docs/daemon.md](docs/daemon.md))

Signal-driven: `Timer → set() → asyncio.Event ← await → Worker → exec`. Blocking I/O in ThreadPoolExecutor (max 4). Configurable intervals in `config.yaml`.

## Parser Registry

Three-tier routing in `news/parser/registry.py`: source_id exact match → URL hostname domain match → default `HtmlParser`.

To add a new site parser:
1. Create `news/parser/sites/<site>.py` — subclass `HtmlParser`, override `_extract()` and/or `_preprocess()`
2. Register in `news/parser/sites/__init__.py` via `registry.register(source_id, parser, domains=[...])`

## Refetch behavior ⚠️

`_download_and_parse()` overwrites ALL metadata fields directly — no manual clearing needed. **Only `content` must be cleared first** because `_run_batch_parse()` skips items that already have content.

## Tests

25+ test files in `tests/`. Site-specific parser tests use real HTML fixtures in `tests/parser_sites/`. Shared fixtures in `conftest.py` and `conftest_db.py`.

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

`tests/parser_sites/` 下每个文件测试一个站点解析器。`tests/parser_sites/test_framework.py` 包含 30 个通用解析器测试（标题提取、正文提取、边界情况等），使用参数化 fixture 对多个站点执行。

### 运行

```bash
pytest
pytest tests/test_parser.py::TestTrimNoise::test_trims_footer_copyright -v
pytest --cov=. --cov-report=term-missing
```

## Key env vars

`PG_*`, `CLOUD_S3_*` (SQLite transfer), `RESOURCE_S3_*` (images/MinIO), `EMAIL_PASSWORD`, `NEWSNOW_EMAIL_*`, `NEWSNOW_WEB_HOST/PORT`, `CONFIG_PATH`.
