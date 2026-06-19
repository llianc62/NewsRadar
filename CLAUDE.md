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

# Install dependencies (requires Python >= 3.12; code uses match/case & PEP 604 unions)
uv sync

# pytest is installed in the venv but NOT declared in pyproject.toml — a fresh
# `uv sync` alone will NOT install it. Add it manually:
uv pip install pytest

# Docker infrastructure (PostgreSQL 16 + MinIO)
docker compose up -d

# Run the test suite
pytest

# Run a single test file / single test
pytest tests/test_parser.py
pytest tests/test_parser.py::TestTrimNoise::test_trims_footer_copyright -v

# Run tests with coverage
pytest --cov=. --cov-report=term-missing
```

## Architecture

NewsRadar is a **news aggregation system** with two operational modes sharing the same fetch logic:

**Local daemon** (`main.py`) — long-running process with PostgreSQL, FastAPI web dashboard, and background workers.
**Cloud CI** (`cli/` package + GitHub Actions) — stateless cron jobs that fetch news, store in SQLite, and sync via S3.

### GitHub Actions workflows

| Workflow | Schedule | Purpose |
|----------|----------|---------|
| `crawler.yml` | Hourly | Fetch all sources → SQLite → S3 upload |
| `notifier.yml` | Beijing 8:00/12:00/16:00/22:00 | Download daily DB from S3 → keyword match → HTML report → email |
| `check-in.yml` | Weekly | Reset workflow runs to keep repository active |

### CLI package (`cli/`)

Typer-based CLI with commands registered as submodules:

| Command | Module | Purpose |
|---------|--------|---------|
| `python -m cli crawl` | `cli/crawl.py` | Full pipeline: fetch all sources → SQLite |
| `python -m cli notify` | `cli/notify.py` | Keyword match → HTML report → email |
| `python -m cli grab-one` | `cli/grab.py` | Single-URL test: download, parse, optionally download images |
| `python -m cli db clear` | `cli/db.py` | Clear PostgreSQL and/or SQLite by time range or all |

### Dual-storage design

- **PostgreSQL** (`storage/postgres.py`) — canonical store for the local daemon. Schema with partial unique indexes for dedup (hotlist: `source_id + url`, RSS: `source_id + guid`). Full-text search via GIN index. Uses `ThreadedConnectionPool` with batch UPSERT templates — `_UPDATE_SET` preserves existing content on conflict while `_UPDATE_SET_OVERWRITE` replaces it.
- **SQLite + S3** (`storage/sqlite.py`, `storage/s3.py`) — cloud CI backend. Each day gets its own `.db` file, uploaded to S3. The notifier downloads and queries these files.
- **Cloud sync** (`Crawler.sync_from_cloud`) — downloads daily SQLite DBs from S3, filters to rows newer than PG's latest cloud `crawled_at`, enriches incremental content (body + images), then merges into PostgreSQL via **UPSERT** (`skip_existing=False`, `crawled_from="cloud"`) so previously synced rows get metadata refreshed on re-crawl — not skipped.
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
- `news/parser.py` — `HtmlParser` (trafilatura-based HTML → Markdown with lxml fallback). Boundary-detection trimming (`_trim_noise`) removes nav/copyright/footer cruft via block-level DOM analysis (`Block` dataclass). Also includes `ImageProcessor` for parallel image download.
- `news/images.py` — `ImageProcessor` class: downloads article images concurrently via `ThreadPoolExecutor`, saves through a `FileStorage` backend, returns `{url: saved_path}` mapping for Markdown content replacement. Supports batch download with automatic Content-Type → extension detection.
- `news/notifier.py` — HTML report generation + SMTP email. `run_notifier()` loads keywords, matches titles, sends report, marks notified.
- `news/keywords.py` — keyword matching engine parsing `frequency_words.txt` format (`/regex/`, `!filter`, `+required`, `@N` limits, `=> Display Name`)
- `news/constants.py` — tier labels/colors (both hex and CSS-variable forms via `TIER_COLORS` / `TIER_COLORS_CSS`), source types, sentiment thresholds (>= 67 positive, <= 33 negative) — single source of truth shared by web and notifier

### Shared utilities (`utils.py`)

- Time formatting: `format_date_folder()`, `format_time_display()`, `format_datetime_now()` — all timezone-aware (default `Asia/Shanghai`)
- `sanitize_filename()` — safe filename generation preserving Chinese/English readability
- `normalize_url()` — removes tracking parameters (`utm_*`, `ref`, etc.) with per-platform customisation (`PLATFORM_PARAMS_TO_REMOVE`)

### Daemon pattern (`main.py`)

Semaphore-based background workers — no polling:

```
Timer ──set()──► asyncio.Event ◄──await── Worker ──exec──► Job
```

Each task type (crawl, sync) gets its own `asyncio.Event` signal. Timers set the event every N minutes (configurable via `crawler.daemon_interval_minutes` for crawl and `crawler.sync_interval_minutes` for sync — both default to 60); workers wait, execute, clear, repeat. Sync signal is manually set at startup for immediate first run. All blocking I/O runs in a dedicated `ThreadPoolExecutor` (max 4 workers). Shutdown is signal-driven (SIGINT/SIGTERM → `asyncio.Event` → graceful cancel + executor shutdown with 10s timeout).

Startup sequence: config → PG connect + schema init → FastAPI web server → background workers + timers → manual sync trigger.

### Web frontend (`web/app.py`)

FastAPI app factory with Jinja2 server-side rendering. Templates in `web/templates/` (base.html with component partials in `web/templates/components/`), static assets in `web/static/css/`.

**Page routes:**
- `/` — market overview with tier stats and source rankings
- `/hot-news` — paginated masonry card list with filters (tier, sentiment, keyword, search, date range). URL-as-state for all filters. Supports `?all=1` to clear date filters.
- `/news/{article_id}` — single article detail with Markdown content rendered via mistune GFM

**Content rendering:** mistune with `escape=False` (allows raw HTML in source), plugins: `strikethrough`, `footnotes`, `table`, `task_lists`. Exposed as Jinja2 `|markdown` filter. Leading H1 is stripped from detail pages to avoid duplicate titles.

**API routes:**
- `POST /api/trigger/{crawl,sync}` — manually set the daemon semaphore signal
- `POST /api/news/fetch` — submit a URL for background fetch/refetch (dedup by URL; refetches existing articles)
- `POST /api/news/{article_id}/refetch` — re-download article body in background
- `DELETE /api/news/{article_id}` — cascade-delete article + images
- `GET /api/notifications` — list in-memory notifications (capped at 50), with optional `?unread_only=true`
- `GET /api/notifications/unread-count` — unread badge count
- `POST /api/notifications/{notif_id}/read` — mark notification read
- `GET /media/{path}` — S3 presigned URL redirect proxy for article images

**Refetch + notification subsystem** (module-level, in-memory, **not** persisted): background jobs run in a `ThreadPoolExecutor` (max 10 workers). Notifications are capped at 50, thread-safe via `threading.Lock`. Article IDs are backfilled into notifications when the fetch completes (URL → article_id resolution). State resets on daemon restart. A `Crawler` instance is injected via `create_app(..., crawler=web_crawler)` so the web layer and daemon share fetch logic without re-creating sessions per request.

### Configuration (`config/loader.py`)

12-factor style: YAML file merged with environment variables. Each config section (`app`, `crawler`, `notification`, `storage`, `postgresql`, `web`) has its own `_load_*_config()` function with explicit env-var mapping. Env vars always take precedence over file values.

Two separate S3 config sections under `storage`:
- **`cloud`** (`CLOUD_S3_*` env vars) — SQLite DB file transfer between CI and daemon
- **`resource`** (`RESOURCE_S3_*` env vars) — article images and project files (local MinIO object storage)

Key env vars: `PG_HOST/PORT/DATABASE/USER/PASSWORD`, `CLOUD_S3_ENDPOINT_URL/BUCKET_NAME/ACCESS_KEY_ID/SECRET_ACCESS_KEY/REGION`, `RESOURCE_S3_ENDPOINT_URL/BUCKET_NAME/ACCESS_KEY_ID/SECRET_ACCESS_KEY/REGION`, `EMAIL_PASSWORD`, `NEWSNOW_EMAIL_*`, `NEWSNOW_WEB_HOST/PORT`, `CONFIG_PATH`.

### Tests

Four test files in `tests/`:
- `test_parser.py` — HTML noise trimming (head navigation, footer copyright, link density pruning)
- `test_refetch.py` — refetch API endpoint behaviour
- `test_notification_frontend.py` — notification list/unread-count/mark-read APIs
- `test_delete.py` — article deletion cascade

### Migration scripts

`scripts/migrate_strip_tag_hash.py` — one-off data migration utility for stripping tag hash suffixes.
