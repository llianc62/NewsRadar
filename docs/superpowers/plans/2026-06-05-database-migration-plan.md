# Database Migration — PostgreSQL + Cloud Sync + MinIO Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate NewsRadar from SQLite-only to PostgreSQL as the local primary database, add cloud-to-local sync, replace web mock data with real queries, and set up MinIO for image storage.

**Architecture:** psycopg2 with ThreadedConnectionPool for PostgreSQL access (raw SQL, consistent with existing sqlite3 pattern). Local crawl reuses existing fetcher.py → models.py pipeline, switching write target from SQLite to PG. Cloud sync downloads S3 SQLite files, parses them, and UPSERTs missing items into PG. Web routes query PG directly. Images stored in MinIO via boto3 (S3-compatible API).

**Tech Stack:** PostgreSQL 16+, psycopg2-binary, boto3 (existing), FastAPI (existing), MinIO (Docker)

---

## File Structure

```
New files:
  database.py          — PG connection pool, schema init, CRUD operations
  sync.py              — Cloud SQLite → PG sync logic
  image_storage.py     — MinIO client, image upload/download

Modified files:
  config.yaml          — Add postgresql + minio sections
  config.py            — Add _load_postgresql_config, _load_minio_config
  main.py              — Modify crawl to write PG, add sync + init-db commands
  pyproject.toml       — Add psycopg2-binary dependency
  web.py               — Replace mock data with real PG queries

Not modified:
  storage.py           — GitHub Actions continues using this for SQLite/S3
  schema.sql           — GitHub Actions schema, unchanged
  fetcher.py           — Reused as-is for local crawl
  models.py            — Reused as-is (NewsItem/NewsData flow)
  frequency.py         — Unchanged
  notifier.py          — Unchanged
```

---

### Task 1: PostgreSQL 环境与依赖

**Files:**
- Modify: `pyproject.toml`
- Modify: `config.yaml`
- Modify: `config.py`

- [ ] **Step 1: Add psycopg2-binary dependency**

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "jinja2>=3.1",
    "requests>=2.33",
    "boto3>=1.42",
    "PyYAML>=6.0",
    "pytz>=2026.1",
    "feedparser>=6.0",
    "httpx>=0.28.1",
    "psycopg2-binary>=2.9",
]
```

- [ ] **Step 2: Install dependencies**

```bash
uv sync
```

- [ ] **Step 3: Add PostgreSQL and MinIO sections to config.yaml**

```yaml
postgresql:
  host: "localhost"
  port: 5432
  database: "newsradar"
  user: "newsradar"
  password: ""           # use env var PG_PASSWORD
  min_connections: 2
  max_connections: 10

minio:
  endpoint_url: "http://localhost:9000"
  bucket_name: "newsradar-images"
  access_key_id: ""      # use env var MINIO_ACCESS_KEY_ID
  secret_access_key: ""  # use env var MINIO_SECRET_ACCESS_KEY
  region: ""
```

Update existing `storage:` section — no changes needed, just verify:

```yaml
storage:
  local:
    data_dir: "output"
  remote:
    endpoint_url: ""
    bucket_name: ""
    access_key_id: ""
    secret_access_key: ""
    region: ""
```

- [ ] **Step 4: Add config loaders in config.py**

Append these two functions before `load_config()`, add them to the return dict:

```python
def _load_postgresql_config(raw: Dict) -> Dict:
    """Load PostgreSQL config."""
    pg = raw.get("postgresql", {})
    return {
        "host": _get_env_str("PG_HOST") or pg.get("host", "localhost"),
        "port": _get_env_int("PG_PORT") or pg.get("port", 5432),
        "database": _get_env_str("PG_DATABASE") or pg.get("database", "newsradar"),
        "user": _get_env_str("PG_USER") or pg.get("user", "newsradar"),
        "password": _get_env_str("PG_PASSWORD") or pg.get("password", ""),
        "min_connections": pg.get("min_connections", 2),
        "max_connections": pg.get("max_connections", 10),
    }


def _load_minio_config(raw: Dict) -> Dict:
    """Load MinIO config."""
    minio = raw.get("minio", {})
    return {
        "endpoint_url": _get_env_str("MINIO_ENDPOINT_URL") or minio.get("endpoint_url", ""),
        "bucket_name": _get_env_str("MINIO_BUCKET_NAME") or minio.get("bucket_name", ""),
        "access_key_id": _get_env_str("MINIO_ACCESS_KEY_ID") or minio.get("access_key_id", ""),
        "secret_access_key": _get_env_str("MINIO_SECRET_ACCESS_KEY") or minio.get("secret_access_key", ""),
        "region": _get_env_str("MINIO_REGION") or minio.get("region", ""),
    }
```

Update `load_config()` return dict to include both:

```python
config = {
    "app": _load_app_config(raw),
    "crawler": _load_crawler_config(raw),
    "platforms": _load_platforms_config(raw),
    "rss": _load_rss_config(raw),
    "notification": _load_notification_config(raw),
    "storage": _load_storage_config(raw),
    "postgresql": _load_postgresql_config(raw),
    "minio": _load_minio_config(raw),
}
```

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml config.yaml config.py uv.lock
git commit -m "chore: add PostgreSQL and MinIO config, psycopg2 dependency"
```

---

### Task 2: Database 模块 — Schema + CRUD

**Files:**
- Create: `database.py`

- [ ] **Step 1: Create database.py with connection pool and schema init**

```python
# coding=utf-8
"""PostgreSQL database layer — connection pool, schema init, CRUD."""

import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool


# Register UUID adapter so psycopg2 can handle JSONB arrays cleanly
psycopg2.extras.register_default_jsonb(loads=json.loads)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS news_articles (
    id              BIGSERIAL PRIMARY KEY,
    source_id       VARCHAR(100) NOT NULL,
    source_name     VARCHAR(200) NOT NULL,
    source_type     VARCHAR(10)  NOT NULL CHECK (source_type IN ('hotlist', 'rss')),
    tier            SMALLINT     NOT NULL DEFAULT 4 CHECK (tier BETWEEN 1 AND 4),
    priority        SMALLINT     NOT NULL DEFAULT 0,
    url             TEXT DEFAULT '',
    mobile_url      TEXT DEFAULT '',
    guid            TEXT DEFAULT '',
    title           TEXT NOT NULL,
    summary         TEXT DEFAULT '',
    content         TEXT DEFAULT '',
    tags            TEXT[] DEFAULT '{}',
    keywords        JSONB DEFAULT '[]',
    entities        JSONB DEFAULT '{}',
    heat_score      INTEGER DEFAULT NULL CHECK (heat_score BETWEEN 0 AND 100),
    sentiment_score INTEGER DEFAULT NULL CHECK (sentiment_score BETWEEN 0 AND 100),
    confidence      INTEGER DEFAULT NULL CHECK (confidence BETWEEN 0 AND 100),
    category        VARCHAR(50) DEFAULT NULL,
    rank            SMALLINT DEFAULT NULL,
    ranks           SMALLINT[] DEFAULT '{}',
    sync_status     VARCHAR(10) NOT NULL DEFAULT 'local' CHECK (sync_status IN ('local', 'cloud')),
    is_analyzed     BOOLEAN NOT NULL DEFAULT FALSE,
    notified        BOOLEAN NOT NULL DEFAULT FALSE,
    published_at     TIMESTAMPTZ DEFAULT NULL,
    first_crawled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_crawled_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS news_images (
    id           BIGSERIAL PRIMARY KEY,
    article_id   BIGINT NOT NULL REFERENCES news_articles(id) ON DELETE CASCADE,
    image_url    TEXT NOT NULL,
    original_url TEXT DEFAULT '',
    width        INTEGER DEFAULT NULL,
    height       INTEGER DEFAULT NULL,
    file_size    INTEGER DEFAULT NULL,
    sort_order   SMALLINT DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dedup indexes (partial unique, matching existing SQLite logic)
CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_hotlist
    ON news_articles (source_id, url)
    WHERE source_type = 'hotlist' AND url != '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_rss
    ON news_articles (source_id, guid)
    WHERE source_type = 'rss' AND guid != '';

-- Query indexes
CREATE INDEX IF NOT EXISTS idx_published_at   ON news_articles (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_tier_priority  ON news_articles (tier, priority DESC);
CREATE INDEX IF NOT EXISTS idx_heat_score     ON news_articles (heat_score DESC);
CREATE INDEX IF NOT EXISTS idx_category       ON news_articles (category);
CREATE INDEX IF NOT EXISTS idx_sync_status    ON news_articles (sync_status);
CREATE INDEX IF NOT EXISTS idx_is_analyzed    ON news_articles (is_analyzed);

-- GIN indexes
CREATE INDEX IF NOT EXISTS idx_tags_gin     ON news_articles USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_keywords_gin ON news_articles USING GIN (keywords);
CREATE INDEX IF NOT EXISTS idx_entities_gin ON news_articles USING GIN (entities);

-- Full-text search index
CREATE INDEX IF NOT EXISTS idx_fulltext ON news_articles
    USING GIN (to_tsvector('simple', title || ' ' || COALESCE(summary, '') || ' ' || COALESCE(content, '')));

-- Images index
CREATE INDEX IF NOT EXISTS idx_images_article ON news_images (article_id);
"""


# Global pool — lazy-init via init_db()
_pool: Optional[ThreadedConnectionPool] = None


def init_db(pg_config: Dict[str, Any]) -> None:
    """Initialize the PostgreSQL connection pool and create schema.

    Must be called once at application startup.
    """
    global _pool

    _pool = ThreadedConnectionPool(
        minconn=pg_config.get("min_connections", 2),
        maxconn=pg_config.get("max_connections", 10),
        host=pg_config["host"],
        port=pg_config["port"],
        dbname=pg_config["database"],
        user=pg_config["user"],
        password=pg_config["password"],
    )

    # Run schema DDL
    conn = _pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
        print("[DB] Schema initialized successfully")
    finally:
        _pool.putconn(conn)


def close_db() -> None:
    """Close the connection pool. Call at application shutdown."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        print("[DB] Connection pool closed")


@contextmanager
def get_conn():
    """Context manager that yields a connection from the pool."""
    if _pool is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)
```

- [ ] **Step 2: Add save_news_data for PostgreSQL**

Append to `database.py`:

```python
def save_news_data(
    news_data,           # NewsData from models.py
    source_tiers: Optional[Dict[str, Dict[str, int]]] = None,
    sync_status: str = "local",
    skip_existing: bool = False,
) -> Dict[str, int]:
    """Save NewsData to PostgreSQL with UPSERT logic.

    Dedup rules (matching partial unique indexes):

    * **Hot-list** — matched on ``(url, source_id)`` when ``url`` is non-empty.
    * **RSS** — matched on ``(guid, source_id)`` when ``guid`` is non-empty.
    * Items without a dedup key are always inserted.

    On **match** when skip_existing=False: title, rank, mobile_url,
    last_crawled_at, tier, priority are updated. ``notified`` is
    **never** touched.
    On **match** when skip_existing=True: item is skipped entirely
    (local data preserved).
    On **insert**: sync_status is set to the provided value.

    Args:
        news_data: NewsData from crawler converters.
        source_tiers: {source_id: {tier: int, priority: int}} mapping.
        sync_status: 'local' for local crawl, 'cloud' for cloud sync.
        skip_existing: If True, skip updates on existing rows (cloud sync).

    Returns:
        {"new": int, "updated": int, "skipped": int}
    """
    if source_tiers is None:
        source_tiers = {}

    new_total = 0
    updated_total = 0
    skipped_total = 0

    with get_conn() as conn:
        with conn.cursor() as cur:
            for source_id, news_list in news_data.items.items():
                tier_info = source_tiers.get(source_id, {})
                tier = tier_info.get("tier", 4)
                priority = tier_info.get("priority", 0)

                for item in news_list:
                    # ── Dedup lookup ──────────────────────
                    existing_id = None
                    if item.source_type == "hotlist" and item.url:
                        cur.execute(
                            """SELECT id FROM news_articles
                               WHERE url = %s AND source_id = %s
                                 AND source_type = 'hotlist'""",
                            (item.url, source_id),
                        )
                        row = cur.fetchone()
                        if row:
                            existing_id = row[0]
                    elif item.source_type == "rss" and item.guid:
                        cur.execute(
                            """SELECT id FROM news_articles
                               WHERE guid = %s AND source_id = %s
                                 AND source_type = 'rss'""",
                            (item.guid, source_id),
                        )
                        row = cur.fetchone()
                        if row:
                            existing_id = row[0]

                    if existing_id is not None:
                        if skip_existing:
                            skipped_total += 1
                            continue
                        # ── Update existing (preserve notified, is_analyzed) ──
                        cur.execute(
                            """UPDATE news_articles SET
                                title = %s, rank = %s, mobile_url = %s,
                                last_crawled_at = %s, priority = %s,
                                tier = %s, summary = %s
                               WHERE id = %s""",
                            (
                                item.title,
                                item.rank,
                                item.mobile_url,
                                _to_timestamptz(item.last_crawl_time, news_data.date),
                                priority,
                                tier,
                                item.summary,
                                existing_id,
                            ),
                        )
                        updated_total += 1
                    else:
                        # ── Insert new ────────────────────
                        cur.execute(
                            """INSERT INTO news_articles
                               (title, source_id, source_name, source_type,
                                tier, priority, url, mobile_url, rank,
                                guid, published_at, summary, author,
                                sync_status, notified,
                                first_crawled_at, last_crawled_at,
                                ranks)
                               VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, FALSE,
                                %s, %s, %s
                               )""",
                            (
                                item.title,
                                source_id,
                                item.source_name,
                                item.source_type,
                                tier,
                                priority,
                                item.url,
                                item.mobile_url,
                                item.rank,
                                item.guid,
                                _to_timestamptz(item.published_at, None),
                                item.summary,
                                item.author,
                                sync_status,
                                _to_timestamptz(item.first_crawl_time, news_data.date),
                                _to_timestamptz(item.last_crawl_time, news_data.date),
                                item.ranks if item.ranks else [],
                            ),
                        )
                        new_total += 1

    print(f"[DB] Saved: {new_total} new, {updated_total} updated, {skipped_total} skipped (sync_status={sync_status})")
    return {"new": new_total, "updated": updated_total, "skipped": skipped_total}


def _to_timestamptz(value: str, fallback_date: Optional[str]) -> Optional[datetime]:
    """Convert a time string (HH:MM or ISO 8601) to a datetime.

    If *value* is 'HH:MM' and *fallback_date* is 'YYYY-MM-DD',
    combine them into a full timestamp.
    """
    if not value:
        return None
    try:
        # Try ISO 8601 first
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    if ":" in value and len(value.split(":")[0]) <= 2 and fallback_date:
        # Probably HH:MM format
        try:
            return datetime.fromisoformat(f"{fallback_date}T{value}:00+08:00")
        except (ValueError, TypeError):
            pass
    return None
```

- [ ] **Step 3: Add query functions for web layer**

Append to `database.py`:

```python
def get_recent_news(
    limit: int = 50,
    offset: int = 0,
    tier: Optional[int] = None,
    category: Optional[str] = None,
    min_confidence: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return recent news articles with optional filters.

    Default filter: confidence IS NULL OR confidence >= 20.
    Ordered by published_at DESC (NULLs last), then heat_score DESC.
    """
    conditions = ["TRUE"]
    params: List[Any] = []

    if tier is not None:
        conditions.append(f"tier = %s")
        params.append(tier)
    if category is not None:
        conditions.append(f"category = %s")
        params.append(category)
    if min_confidence is not None:
        conditions.append("(confidence IS NULL OR confidence >= %s)")
        params.append(min_confidence)
    else:
        conditions.append("(confidence IS NULL OR confidence >= 20)")

    where = " AND ".join(conditions)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""SELECT id, title, source_id, source_name, source_type,
                           tier, priority, url, mobile_url, summary,
                           tags, heat_score, sentiment_score,
                           sync_status, is_analyzed,
                           published_at, created_at
                    FROM news_articles
                    WHERE {where}
                    ORDER BY published_at DESC NULLS LAST, heat_score DESC NULLS LAST
                    LIMIT %s OFFSET %s""",
                params + [limit, offset],
            )
            return cur.fetchall()


def get_news_count(
    tier: Optional[int] = None,
    category: Optional[str] = None,
) -> int:
    """Return total count of news articles matching filters."""
    conditions = ["(confidence IS NULL OR confidence >= 20)"]
    params: List[Any] = []

    if tier is not None:
        conditions.append(f"tier = %s")
        params.append(tier)
    if category is not None:
        conditions.append(f"category = %s")
        params.append(category)

    where = " AND ".join(conditions)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM news_articles WHERE {where}",
                params,
            )
            return cur.fetchone()[0]


def get_news_by_id(article_id: int) -> Optional[Dict[str, Any]]:
    """Return a single article by ID, including content and images."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM news_articles WHERE id = %s",
                (article_id,),
            )
            article = cur.fetchone()
            if article:
                cur.execute(
                    "SELECT * FROM news_images WHERE article_id = %s ORDER BY sort_order",
                    (article_id,),
                )
                article["images"] = cur.fetchall()
            return article


def get_stats() -> Dict[str, Any]:
    """Return dashboard stats: counts by tier, source, and today's new."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT
                     COUNT(*) FILTER (WHERE tier = 1) AS t1_count,
                     COUNT(*) FILTER (WHERE tier = 2) AS t2_count,
                     COUNT(*) FILTER (WHERE tier = 3) AS t3_count,
                     COUNT(*) FILTER (WHERE tier = 4) AS t4_count,
                     COUNT(*) AS total_count,
                     COUNT(*) FILTER (WHERE created_at >= CURRENT_DATE) AS today_count
                   FROM news_articles
                   WHERE confidence IS NULL OR confidence >= 20"""
            )
            stats = dict(cur.fetchone())

            cur.execute(
                "SELECT source_name, COUNT(*) AS cnt FROM news_articles GROUP BY source_name ORDER BY cnt DESC"
            )
            stats["by_source"] = cur.fetchall()

            return stats
```

- [ ] **Step 4: Commit**

```bash
git add database.py
git commit -m "feat: add PostgreSQL database layer with schema, CRUD, and queries"
```

---

### Task 3: 本地 Crawl 适配 — 写入 PostgreSQL

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Modify cmd_crawl to write to PostgreSQL**

In `main.py`, modify `cmd_crawl()` to initialize PG and write results there instead of SQLite/S3:

```python
def cmd_crawl(config: dict):
    """Run the crawler: fetch + store to PostgreSQL."""
    from database import init_db, save_news_data, close_db

    timezone = config["app"]["timezone"]
    date = format_date_folder(timezone)
    time_str = format_time_display(timezone)

    print(f"=== Crawler === {date} {time_str}")

    # Init PostgreSQL
    init_db(config["postgresql"])
    source_tiers = build_source_tiers(config)

    total_new = 0
    total_updated = 0

    # ── Fetch hot-list ─────────────────────────────────
    if config["platforms"]["enabled"]:
        request_interval = config["crawler"]["request_interval"]
        sources = config["platforms"]["sources"]
        ids_list = [(s["id"], s["name"]) for s in sources]

        print(f"\n[Hot-list] Fetching {len(ids_list)} platforms...")
        fetcher = DataFetcher()
        results, id_to_name, failed_ids = fetcher.crawl_websites(
            ids_list, request_interval
        )

        if results:
            news_data = convert_crawl_results_to_news_data(
                results, id_to_name, failed_ids, time_str, date
            )
            counts = save_news_data(news_data, source_tiers, sync_status="local")
            total_new += counts["new"]
            total_updated += counts["updated"]

    # ── Fetch RSS ──────────────────────────────────────
    rss_cfg = config["rss"]
    if rss_cfg["enabled"]:
        print("\n[RSS] Fetching feeds...")
        rss_fetcher = RSSFetcher.from_config(rss_cfg)
        rss_results, rss_id_to_name, rss_failed_ids = rss_fetcher.fetch_all()

        if rss_results:
            rss_news_data = convert_rss_items_to_news_data(
                rss_results, rss_id_to_name, rss_failed_ids, time_str, date
            )
            counts = save_news_data(rss_news_data, source_tiers, sync_status="local")
            total_new += counts["new"]
            total_updated += counts["updated"]

    close_db()
    print(f"=== Done: {total_new} new, {total_updated} updated ===")
```

- [ ] **Step 2: Update __main__ block to support new commands**

```python
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python main.py [crawl|notify|sync|init-db]")
        sys.exit(1)

    cmd = sys.argv[1]
    cfg = load_config("config.yaml")

    if cmd == "crawl":
        cmd_crawl(cfg)
    elif cmd == "notify":
        cmd_notify(cfg)
    elif cmd == "sync":
        cmd_sync(cfg)
    elif cmd == "init-db":
        cmd_init_db(cfg)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
```

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: adapt local crawl to write PostgreSQL instead of SQLite"
```

---

### Task 4: 云端同步模块

**Files:**
- Create: `sync.py`
- Modify: `main.py`

- [ ] **Step 1: Create sync.py**

```python
# coding=utf-8
"""Cloud-to-local sync: download SQLite from S3, merge into PostgreSQL."""

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from database import get_conn, save_news_data, init_db, close_db
from models import NewsData, NewsItem


def sync_from_cloud(
    pg_config: Dict[str, Any],
    s3_config: Dict[str, Any],
    dates: Optional[List[str]] = None,
    data_dir: str = "output",
) -> Dict[str, Any]:
    """Download daily SQLite DBs from S3 and merge into PostgreSQL.

    For each date:
    1. Download news/{date}.db from S3
    2. Parse all rows from the SQLite file
    3. UPSERT into PostgreSQL with sync_status='cloud'
    4. Clean up temp files

    Args:
        pg_config: PostgreSQL connection config.
        s3_config: S3 connection config (same format as Storage).
        dates: List of YYYY-MM-DD date strings. Defaults to yesterday
               through 7 days ago if not specified.
        data_dir: Local directory for temp downloads.

    Returns:
        {"dates_processed": int, "total_new": int, "total_skipped": int, "errors": [str]}
    """
    from storage import Storage

    init_db(pg_config)

    # Default: sync the last 7 days
    if dates is None:
        from datetime import datetime, timedelta
        import pytz
        tz = pytz.timezone("Asia/Shanghai")
        today = datetime.now(tz).date()
        dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]

    # We use Storage only for S3 download capability (not for writing)
    storage = Storage(data_dir=data_dir, s3_config=s3_config)

    total_new = 0
    total_skipped = 0
    errors = []

    for date_str in dates:
        print(f"\n[Sync] Processing {date_str}...")

        # Download from S3
        downloaded = storage._download_from_s3(date_str)
        if downloaded is None:
            print(f"[Sync] No S3 object for {date_str}, skipping")
            continue

        try:
            # Parse SQLite rows
            rows = _read_sqlite_db(downloaded)
            print(f"[Sync] Read {len(rows)} rows from {date_str}.db")

            if not rows:
                continue

            # Convert to NewsData format and save (skip existing = local wins)
            news_data = _rows_to_newsdata(rows, date_str)
            result = save_news_data(news_data, sync_status="cloud", skip_existing=True)
            total_new += result["new"]
            total_skipped += result["skipped"]

        except Exception as e:
            msg = f"Failed to sync {date_str}: {e}"
            print(f"[Sync] {msg}")
            errors.append(msg)
        finally:
            # Clean up temp file
            try:
                os.unlink(str(downloaded))
            except OSError:
                pass

    storage.cleanup()
    close_db()

    print(f"\n[Sync] Complete: {total_new} new, {total_skipped} skipped, {len(errors)} errors")
    return {
        "dates_processed": len(dates),
        "total_new": total_new,
        "total_skipped": total_skipped,
        "errors": errors,
    }


def _read_sqlite_db(db_path: Path) -> List[Dict[str, Any]]:
    """Read all rows from a SQLite news_items table."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute("SELECT * FROM news_items ORDER BY id")
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _rows_to_newsdata(rows: List[Dict[str, Any]], date_str: str) -> NewsData:
    """Convert SQLite rows to a NewsData object for save_news_data()."""
    items: Dict[str, List[NewsItem]] = {}

    for row in rows:
        source_id = row.get("source_id", "unknown")
        if source_id not in items:
            items[source_id] = []

        item = NewsItem(
            title=row.get("title", ""),
            source_id=source_id,
            source_name=row.get("source_name", ""),
            source_type=row.get("source_type", "hotlist"),
            tier=row.get("tier", 4),
            priority=row.get("priority", 0),
            url=row.get("url", ""),
            mobile_url=row.get("mobile_url", ""),
            rank=row.get("rank") or 0,
            guid=row.get("guid", ""),
            published_at=row.get("published_at", ""),
            summary=row.get("summary", ""),
            author=row.get("author", ""),
            first_crawl_time=row.get("first_crawl_time", ""),
            last_crawl_time=row.get("last_crawl_time", ""),
            crawl_count=row.get("crawl_count", 1),
        )
        items[source_id].append(item)

    return NewsData(
        date=date_str,
        crawl_time="",
        items=items,
        id_to_name={},
        failed_ids=[],
    )
```

- [ ] **Step 2: Add cmd_sync and cmd_init_db to main.py**

```python
def cmd_sync(config: dict):
    """Sync cloud SQLite data into local PostgreSQL."""
    from sync import sync_from_cloud

    print("=== Cloud Sync ===")

    pg_config = config["postgresql"]
    s3_config = config["storage"]["remote"]

    if not s3_config.get("bucket_name") or not s3_config.get("endpoint_url"):
        print("[Sync] S3 not configured — cannot sync. Set S3_* env vars.")
        return

    result = sync_from_cloud(
        pg_config=pg_config,
        s3_config=s3_config,
        data_dir=config["storage"]["local"]["data_dir"],
    )
    print(f"Result: {result}")


def cmd_init_db(config: dict):
    """Initialize PostgreSQL schema only."""
    from database import init_db, close_db

    print("=== Init DB ===")
    init_db(config["postgresql"])
    print("Schema created successfully.")
    close_db()
```

- [ ] **Step 3: Commit**

```bash
git add sync.py main.py
git commit -m "feat: add cloud-to-local sync module and CLI commands"
```

---

### Task 5: Web 服务迁移 — Mock → PostgreSQL

**Files:**
- Modify: `web.py`

- [ ] **Step 1: Add database init/shutdown to app lifecycle**

Replace the top of `web.py` after imports:

```python
"""NewsRadar Web Frontend — FastAPI + Jinja2 SSR with PostgreSQL."""

from pathlib import Path

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import load_config
from database import init_db, close_db, get_recent_news, get_news_count, get_stats

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Load config for DB connection
_config = load_config(str(BASE_DIR / "config.yaml"))

# Jinja2 environment
env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)

# ... ICONS dict stays the same ...

env.globals["icon_svg"] = lambda name: ICONS.get(name, "")
env.globals["len"] = len


def render_template(name: str, **context) -> str:
    """Render a Jinja2 template."""
    template = env.get_template(name)
    return template.render(**context)


app = FastAPI(title="NewsRadar", version="2.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def startup():
    """Initialize database on app start."""
    init_db(_config["postgresql"])
    print("[Web] Database initialized")


@app.on_event("shutdown")
async def shutdown():
    """Close database on app shutdown."""
    close_db()
```

- [ ] **Step 2: Replace market_overview with real data**

```python
@app.get("/", response_class=HTMLResponse)
async def market_overview(request: Request):
    """Market overview page with real stats."""
    stats = get_stats()

    # Build index cards from stats
    index_cards = [
        {"name": "T1·官媒", "value": str(stats["t1_count"]), "change": None},
        {"name": "T2·主流", "value": str(stats["t2_count"]), "change": None},
        {"name": "T3·垂直", "value": str(stats["t3_count"]), "change": None},
        {"name": "T4·资讯", "value": str(stats["t4_count"]), "change": None},
        {"name": "总计", "value": str(stats["total_count"]), "change": None},
        {"name": "今日新增", "value": str(stats["today_count"]), "change": None},
    ]

    hot_stocks = [
        {"name": s["source_name"], "change": s["cnt"]}
        for s in stats["by_source"][:8]
    ]

    html = render_template(
        "pages/market_overview.html",
        active_page="home",
        index_cards=index_cards,
        hot_stocks=hot_stocks,
    )
    return HTMLResponse(html)
```

- [ ] **Step 3: Replace hot_news with real data**

```python
@app.get("/hot-news", response_class=HTMLResponse)
async def hot_news(
    request: Request,
    page: int = Query(1, ge=1),
    tier: int = Query(None, ge=0, le=4),
):
    """Hot news page with real data from PostgreSQL."""
    from notifier import TIER_LABELS, TIER_COLORS, TIER_BG

    per_page = 10
    offset = (page - 1) * per_page

    # Fetch from DB
    articles = get_recent_news(
        limit=per_page,
        offset=offset,
        tier=tier if tier and tier > 0 else None,
    )
    total = get_news_count(tier=tier if tier and tier > 0 else None)
    total_pages = max(1, (total + per_page - 1) // per_page)

    # Stats
    stats_data = get_stats()

    stats = [
        {"label": "今日新增", "value": str(stats_data["today_count"]), "icon": "flame",
         "bg": "hsl(var(--primary) / 0.1)", "color": "hsl(var(--foreground))"},
        {"label": "新闻来源", "value": str(len(stats_data["by_source"])), "icon": "newspaper",
         "bg": "hsl(var(--info) / 0.1)", "color": "hsl(var(--foreground))"},
        {"label": "总计", "value": str(stats_data["total_count"]), "icon": "star",
         "bg": "hsl(var(--warning) / 0.1)", "color": "hsl(var(--foreground))"},
        {"label": "T1·官媒", "value": str(stats_data["t1_count"]), "icon": "trending-up-lg",
         "bg": "hsl(var(--danger) / 0.1)", "color": "hsl(var(--danger))"},
    ]

    tier_labels = [
        {"label": f"T{t}·{TIER_LABELS[t].split('·')[1]}", "color": c, "bg": TIER_BG[t]}
        for t, c in TIER_COLORS.items()
    ]

    # Convert articles to template format
    def _to_card(article: dict) -> dict:
        sentiment = "neutral"
        sentiment_bg = "hsl(var(--warning) / 0.1)"
        sentiment_color = "hsl(var(--warning))"
        score = article.get("sentiment_score")
        if score is not None:
            if score >= 67:
                sentiment = "利好"
                sentiment_bg = "hsl(var(--danger) / 0.1)"
                sentiment_color = "hsl(var(--danger))"
            elif score <= 33:
                sentiment = "利空"
                sentiment_bg = "hsl(var(--success) / 0.1)"
                sentiment_color = "hsl(var(--success))"

        return {
            "sentiment": sentiment,
            "sentiment_bg": sentiment_bg,
            "sentiment_color": sentiment_color,
            "source": article.get("source_name", ""),
            "time": _relative_time(article.get("published_at")),
            "heat": str(article.get("heat_score") or 0),
            "title": article.get("title", ""),
            "summary": article.get("summary", ""),
            "keywords": [{"text": t, "primary": i == 0} for i, t in enumerate(article.get("tags") or [])],
        }

    def _to_list_item(article: dict, seq: int) -> dict:
        return {
            "seq": seq,
            "title": article.get("title", ""),
            "source": article.get("source_name", ""),
        }

    tier1_cards = [_to_card(a) for a in articles]
    list_items = [_to_list_item(a, offset + i + 1) for i, a in enumerate(articles)]

    # Page numbers for pagination
    page_numbers = _build_page_numbers(page, total_pages)

    html = render_template(
        "pages/hot_news.html",
        active_page="hot-news",
        stats=stats,
        tier_labels=tier_labels,
        keywords=["央行", "AI", "港股", "外资", "芯片", "新能源"],
        tier1_cards=tier1_cards,
        list_items=list_items,
        total_count=total,
        page_start=offset + 1,
        page_end=min(offset + per_page, total),
        current_page=page,
        page_numbers=page_numbers,
    )
    return HTMLResponse(html)
```

- [ ] **Step 4: Add helper functions to web.py**

Append before `if __name__ == "__main__":`:

```python
def _relative_time(dt) -> str:
    """Convert datetime to relative time string like '2h前'."""
    if dt is None:
        return ""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    if diff < timedelta(minutes=1):
        return "刚刚"
    elif diff < timedelta(hours=1):
        return f"{int(diff.total_seconds() // 60)}min前"
    elif diff < timedelta(days=1):
        return f"{int(diff.total_seconds() // 3600)}h前"
    else:
        return f"{diff.days}d前"


def _build_page_numbers(current: int, total: int) -> list:
    """Build page number list like [1, 2, 3, '...', 8]."""
    if total <= 7:
        return list(range(1, total + 1))
    pages = [1, 2, 3]
    if current > 4:
        pages.append("...")
    for p in range(max(4, current - 1), min(total - 2, current + 2) + 1):
        if p not in pages:
            pages.append(p)
    if current < total - 3:
        pages.append("...")
    for p in [total - 2, total - 1, total]:
        if p not in pages:
            pages.append(p)
    return pages
```

- [ ] **Step 5: Add an article detail route**

```python
@app.get("/news/{article_id}", response_class=HTMLResponse)
async def news_detail(request: Request, article_id: int):
    """Single news article detail page."""
    from database import get_news_by_id

    article = get_news_by_id(article_id)
    if article is None:
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)

    html = render_template(
        "pages/news_detail.html",
        active_page="hot-news",
        article=article,
    )
    return HTMLResponse(html)
```

- [ ] **Step 6: Commit**

```bash
git add web.py
git commit -m "feat: migrate web service from mock data to PostgreSQL"
```

---

### Task 6: MinIO 部署与图片存储

**Files:**
- Create: `image_storage.py`
- Create: `docker-compose.yml` (MinIO)

- [ ] **Step 1: Create docker-compose.yml for MinIO**

```yaml
version: "3.8"
services:
  minio:
    image: minio/minio:latest
    container_name: newsradar-minio
    ports:
      - "9000:9000"   # API
      - "9001:9001"   # Console
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    volumes:
      - ./data/minio:/data
    command: server /data --console-address ":9001"
    restart: unless-stopped
```

- [ ] **Step 2: Create image_storage.py**

```python
# coding=utf-8
"""MinIO image storage — S3-compatible client wrapper."""

import os
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError


class ImageStorage:
    """MinIO / S3-compatible image storage.

    Usage::

        store = ImageStorage(endpoint_url="http://localhost:9000",
                             bucket_name="newsradar-images",
                             access_key="minioadmin",
                             secret_key="minioadmin")
        store.ensure_bucket()
        url = store.upload_image(local_path, "2026-06/1/image_01.jpg")
    """

    def __init__(
        self,
        endpoint_url: str,
        bucket_name: str,
        access_key: str,
        secret_key: str,
        region: str = "",
    ):
        self.endpoint_url = endpoint_url
        self.bucket_name = bucket_name

        boto_config = BotoConfig(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
        )

        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=boto_config,
            region_name=region or "us-east-1",
        )

    def ensure_bucket(self) -> None:
        """Create bucket if it doesn't exist."""
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            print(f"[ImageStorage] Bucket '{self.bucket_name}' exists")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchBucket", "Not Found"):
                self.client.create_bucket(Bucket=self.bucket_name)
                print(f"[ImageStorage] Created bucket '{self.bucket_name}'")
            else:
                raise

    def upload_image(
        self,
        local_path: Path,
        object_key: str,
        content_type: Optional[str] = None,
    ) -> str:
        """Upload an image file and return the public URL.

        Args:
            local_path: Path to the local image file.
            object_key: S3 object key (e.g. '2026-06/1/image_01.jpg').
            content_type: MIME type. Auto-detected from extension if None.

        Returns:
            Full URL to the uploaded image.
        """
        if content_type is None:
            content_type = self._guess_content_type(local_path)

        file_size = local_path.stat().st_size

        self.client.upload_file(
            str(local_path),
            self.bucket_name,
            object_key,
            ExtraArgs={"ContentType": content_type},
        )

        url = f"{self.endpoint_url}/{self.bucket_name}/{object_key}"
        print(f"[ImageStorage] Uploaded: {object_key} ({file_size} bytes)")
        return url

    def delete_image(self, object_key: str) -> bool:
        """Delete an image. Returns True on success."""
        try:
            self.client.delete_object(
                Bucket=self.bucket_name, Key=object_key
            )
            print(f"[ImageStorage] Deleted: {object_key}")
            return True
        except ClientError as e:
            print(f"[ImageStorage] Delete failed: {object_key}: {e}")
            return False

    @staticmethod
    def _guess_content_type(path: Path) -> str:
        ext = path.suffix.lower()
        mapping = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
        }
        return mapping.get(ext, "application/octet-stream")
```

- [ ] **Step 3: Add image download + upload helper to database.py**

Append to `database.py`:

```python
def save_article_image(
    article_id: int,
    image_url: str,
    original_url: str = "",
    width: Optional[int] = None,
    height: Optional[int] = None,
    file_size: Optional[int] = None,
    sort_order: int = 0,
) -> int:
    """Insert a news_images row and return its ID."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO news_images
                   (article_id, image_url, original_url, width, height, file_size, sort_order)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (article_id, image_url, original_url, width, height, file_size, sort_order),
            )
            return cur.fetchone()[0]
```

- [ ] **Step 4: Commit**

```bash
git add image_storage.py docker-compose.yml database.py
git commit -m "feat: add MinIO image storage and docker-compose setup"
```

---

## Verification

### End-to-end test workflow

1. **Start infrastructure:**
   ```bash
   # Ensure PostgreSQL is running locally
   sudo systemctl start postgresql

   # Create database and user
   sudo -u postgres createuser newsradar -P
   sudo -u postgres createdb newsradar -O newsradar

   # Start MinIO
   docker compose up -d
   ```

2. **Initialize database:**
   ```bash
   python main.py init-db
   ```
   Expected: "[DB] Schema initialized successfully"

3. **Run local crawl:**
   ```bash
   python main.py crawl
   ```
   Expected: fetches from 7 platforms + 1 RSS feed, prints new/updated counts

4. **Verify data in PostgreSQL:**
   ```bash
   psql -U newsradar -d newsradar -c "SELECT COUNT(*), sync_status FROM news_articles GROUP BY sync_status;"
   ```
   Expected: rows with sync_status='local'

5. **Run cloud sync:**
   ```bash
   python main.py sync
   ```
   Expected: downloads S3 SQLite files for past 7 days, inserts missing rows with sync_status='cloud'

6. **Start web service:**
   ```bash
   python web.py
   ```
   Visit http://localhost:8000/ — market overview shows real stats.
   Visit http://localhost:8000/hot-news — hot news page shows real articles from DB.

7. **Test image upload:**
   ```python
   from image_storage import ImageStorage
   from config import load_config
   cfg = load_config("config.yaml")
   store = ImageStorage(**cfg["minio"])
   store.ensure_bucket()
   url = store.upload_image(Path("test.jpg"), "test/upload.jpg")
   print(url)
   ```
