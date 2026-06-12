# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Local daemon — PostgreSQL + FastAPI web dashboard
python main.py

# Cloud CI (GitHub Actions) — fetch + SQLite + S3
python -m cli crawl

# Cloud CI — keyword matching + email report
python -m cli notify

# Test content extraction on a single URL
python -m cli grab-one "https://example.com" --output-style markdown
python -m cli grab-one "https://example.com" --output-style postgresql --images

# Database maintenance (time-range or full clear)
python -m cli db clear --before 2026-06-01 --force
python -m cli db clear --all --force
python -m cli db clear --backend postgresql --before 2026-06-01

# Install dependencies
uv sync

# Docker infrastructure (PostgreSQL 16 + MinIO)
docker compose up -d
```

## Architecture

NewsRadar is a **news aggregation system** with two operational modes sharing the same fetch logic:

**Local daemon** (`main.py`) — long-running process with PostgreSQL, FastAPI web dashboard, and background workers.
**Cloud CI** (`cli/` package + GitHub Actions) — stateless cron jobs that fetch news, store in SQLite, and sync via S3.

### CLI package (`cli/`)

Typir-based CLI with commands registered as submodules:

| Command | Module | Purpose |
|---------|--------|---------|
| `python -m cli crawl` | `cli/crawl.py` | Full pipeline: fetch all sources → SQLite |
| `python -m cli notify` | `cli/notify.py` | Keyword match → HTML report → email |
| `python -m cli grab-one` | `cli/grab.py` | Single-URL test: download, parse, optionally download images |
| `python -m cli db clear` | `cli/db.py` | Clear PostgreSQL and/or SQLite by time range or all |

### Dual-storage design

- **PostgreSQL** (`storage/postgres.py`) — canonical store for the local daemon. Schema with partial unique indexes for dedup (hotlist: `source_id + url`, RSS: `source_id + guid`). Full-text search via GIN index.
- **SQLite + S3** (`storage/sqlite.py`, `storage/s3.py`) — cloud CI backend. Each day gets its own `.db` file, uploaded to S3. The notifier downloads and queries these files.
- **Cloud sync** (method on `Crawler`) — downloads daily SQLite DBs from S3, merges into PostgreSQL with `skip_existing=True` (local wins over cloud).
- **File storage** (`storage/files.py`) — unified `FileStorage` ABC with `LocalStorage` (filesystem) and `S3Storage` (MinIO/S3-compatible) implementations. Used by `ImageProcessor` for article image storage. Factory: `create_storage(config)`.

### News fetch pipeline

```
Config sources ──► NewsnowFetcher (hot-list API) ──► NewsData ──► Storage backend
              ──► RssFetcher (RSS/Atom/JSON Feed) ──┘
                                                    └── Optional: trafilatura content enrichment
```

- `news/fetcher/` — `Fetcher` ABC, `NewsnowFetcher` (hot-list API with retry + jitter), `RssFetcher` (RSS 2.0 / Atom / JSON Feed 1.1)
- `news/models.py` — `NewsItem` and `NewsData` dataclasses, plus converter functions from raw fetch results
- `news/crawler.py` — `Crawler` class with `OutputStyle` enum (`MARKDOWN`, `HTML`, `SQLITE`, `POSTGRESQL`). Public API: `fetch()` (single URL), `fetch_all()` (all configured sources). Phased pipeline: hot-list → RSS → optional content body download → optional image download → persistence. Uses `ThreadPoolExecutor` for concurrent HTML download/parse.
- `news/parser.py` — `HtmlParser` (trafilatura-based HTML → Markdown) and `ImageProcessor` (parallel image download with storage backend)
- `news/notifier.py` — HTML report generation + SMTP email. `run_notifier()` loads keywords, matches titles, sends report, marks notified.
- `news/keywords.py` — keyword matching engine parsing `frequency_words.txt` format (`/regex/`, `!filter`, `+required`, `@N` limits, `=> Display Name`)
- `news/constants.py` — tier labels/colors (both hex and CSS-variable forms), source types, sentiment thresholds — single source of truth shared by web and notifier

### Daemon pattern (`main.py`)

Semaphore-based background workers — no polling:

```
Timer ──set()──► asyncio.Event ◄──await── Worker ──exec──► Job
```

Each task type (crawl, sync) gets its own `asyncio.Event` signal. Timers set the event every N minutes (configurable via `crawler.daemon_interval_minutes` and `crawler.sync_interval_minutes`); workers wait, execute, clear, repeat. Sync signal is manually set at startup for immediate first run. All blocking I/O runs in a dedicated `ThreadPoolExecutor`. Shutdown is signal-driven (SIGINT/SIGTERM → `asyncio.Event` → graceful cancel + executor shutdown).

### Web frontend (`web/app.py`)

FastAPI app factory with Jinja2 server-side rendering. Routes: `/` (market overview with tier stats), `/hot-news` (paginated list with tier filter), `/news/{id}` (detail page with content rendered via mistune GFM). Manual trigger API at `POST /api/trigger/{crawl,sync}` sets the corresponding semaphore signal. Templates in `web/templates/`, static assets in `web/static/`.

### Configuration (`config/loader.py`)

12-factor style: YAML file merged with environment variables. Each config section (`app`, `crawler`, `notification`, `storage`, `postgresql`, `web`) has its own `_load_*_config()` function with explicit env-var mapping. Env vars always take precedence over file values.

Key env vars: `PG_HOST/PORT/DATABASE/USER/PASSWORD`, `S3_ENDPOINT_URL/BUCKET_NAME/ACCESS_KEY_ID/SECRET_ACCESS_KEY/REGION`, `EMAIL_PASSWORD`, `CONFIG_PATH`.
