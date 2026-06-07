# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Local daemon — PostgreSQL + FastAPI web dashboard
python main.py

# Cloud CI (GitHub Actions) — fetch + SQLite + S3
python cli.py crawl

# Cloud CI — keyword matching + email report
python cli.py notify

# Install dependencies
uv sync

# Docker infrastructure (PostgreSQL 16 + MinIO)
docker compose up -d
```

## Architecture

NewsRadar is a **news aggregation system** with two operational modes sharing the same fetch logic:

**Local daemon** (`main.py`) — long-running process with PostgreSQL, FastAPI web dashboard, and background workers.
**Cloud CI** (`cli.py` + GitHub Actions) — stateless cron jobs that fetch news, store in SQLite, and sync via S3.

### Dual-storage design

- **PostgreSQL** (`storage/postgres.py`) — canonical store for the local daemon. Schema with partial unique indexes for dedup (hotlist: `source_id + url`, RSS: `source_id + guid`). Full-text search via GIN index.
- **SQLite + S3** (`storage/sqlite.py`, `storage/s3.py`) — cloud CI backend. Each day gets its own `.db` file, uploaded to S3. The notifier downloads and queries these files.
- **Cloud sync** (`storage/sync.py`) — downloads daily SQLite DBs from S3, merges into PostgreSQL with `skip_existing=True` (local wins over cloud).
- **MinIO** (`storage/minio.py`) — S3-compatible image storage for article content extraction.

### News fetch pipeline

```
Config sources ──► NewsFetcher (NewsNow API) ──► NewsData ──► Storage backend
              ──► RSSFetcher (RSS/Atom/JSON Feed) ──┘
                                                    └── Optional: trafilatura content enrichment
```

- `news/fetcher.py` — NewsFetcher (hot-list API calls with retry + jitter), RSSFetcher, RSSParser (RSS 2.0, Atom, JSON Feed 1.1), ArticleParser (content extraction with MinIO image storage)
- `news/models.py` — `NewsItem` and `NewsData` dataclasses, plus converter functions from raw fetch results
- `news/crawler.py` — `fetch_all()` orchestrates both fetchers, `_enrich_with_content()` downloads article body via trafilatura
- `news/notifier.py` — HTML report generation + SMTP email. Loads keywords, matches titles, sends report
- `news/keywords.py` — keyword matching engine parsing `frequency_words.txt` format (`/regex/`, `!filter`, `+required`, `@N` limits, `=> Display Name`)
- `news/constants.py` — tier labels/colors, source types, sentiment thresholds — single source of truth shared by web and notifier

### Daemon pattern (`main.py`)

Semaphore-based background workers — no polling:

```
Timer ──set()──► asyncio.Event ◄──await── Worker ──exec──► Job
```

Each task type (crawl, sync) gets its own signal event and worker coroutine. Timers set the event every N minutes; workers wait, execute, clear, repeat. Sync signal is manually set at startup for immediate first run. All blocking I/O runs in a dedicated `ThreadPoolExecutor`.

### Web frontend (`web/app.py`)

FastAPI app factory with Jinja2 server-side rendering. Routes: `/` (market overview), `/hot-news` (paginated list with tier filter), `/news/{id}` (detail). Manual trigger API at `POST /api/trigger/{crawl,sync}` sets the corresponding semaphore signal. Static files served from `web/static/`, templates in `web/templates/`.

### Configuration (`config/loader.py`)

12-factor style: YAML file merged with environment variables. Each config section (`app`, `crawler`, `platforms`, `rss`, `notification`, `storage`, `postgresql`, `minio`, `web`) has its own `_load_*_config()` function with explicit env-var mapping. Env vars always take precedence over file values.

Key env vars: `PG_HOST/PORT/DATABASE/USER/PASSWORD`, `S3_ENDPOINT_URL/BUCKET_NAME/ACCESS_KEY_ID/SECRET_ACCESS_KEY/REGION`, `MINIO_*`, `EMAIL_PASSWORD`, `CONFIG_PATH`.
