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

# Run tests
pytest
pytest tests/test_parser.py::TestTrimNoise::test_trims_footer_copyright -v
pytest --cov=. --cov-report=term-missing
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

Key modules:
- `news/crawler.py` — `Crawler` class. Public API: `fetch()`, `fetch_all()`, `enrich_content()`, `sync_from_cloud()`, `retry_failed_tasks()`
- `news/models.py` — `NewsItem` / `NewsData` dataclasses. `NewsItem.ranks` = `[[rank, total], ...]` JSONB; `heat_score` = 0-100
- `news/parser/` — HtmlParser + Registry + 12 site-specific parsers. Three-tier extraction: custom hook → readability → HTML-strip fallback
- `news/analyzer/` — `Analyzer` ABC + `JiebaAnalyzer` (heat + sentiment + keywords) + `AgentAnalyzer` (reserved)
- `news/images.py` — `ImageProcessor`: concurrent download → `FileStorage` backend
- `news/notifier.py` — HTML report + SMTP email
- `news/keywords.py` — parses `frequency_words.txt` format
- `news/constants.py` — tier labels/colors, source types, sentiment thresholds
- `utils.py` — time formatting (timezone-aware, default `Asia/Shanghai`), `normalize_url()`
- `config/loader.py` — YAML + env vars, env takes precedence

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

## Key env vars

`PG_*`, `CLOUD_S3_*` (SQLite transfer), `RESOURCE_S3_*` (images/MinIO), `EMAIL_PASSWORD`, `NEWSNOW_EMAIL_*`, `NEWSNOW_WEB_HOST/PORT`, `CONFIG_PATH`.
