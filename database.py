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


# Register JSONB adapter
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
    author          TEXT DEFAULT '',
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


def save_news_data(
    news_data,           # NewsData from models.py
    source_tiers: Optional[Dict[str, Dict[str, int]]] = None,
    sync_status: str = "local",
    skip_existing: bool = False,
) -> Dict[str, int]:
    """Save NewsData to PostgreSQL with UPSERT logic.

    Dedup rules:
    * Hot-list — matched on (url, source_id) when url is non-empty.
    * RSS — matched on (guid, source_id) when guid is non-empty.

    On match when skip_existing=False: title, rank, mobile_url,
    last_crawled_at, tier, priority are updated. notified is never touched.
    On match when skip_existing=True: item is skipped (local data preserved).
    On insert: sync_status is set to the provided value.

    Returns: {"new": int, "updated": int, "skipped": int}
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
                    # Dedup lookup
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
                        # Update existing (preserve notified, is_analyzed)
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
                        # Insert new
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
        conditions.append("tier = %s")
        params.append(tier)
    if category is not None:
        conditions.append("category = %s")
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
        conditions.append("tier = %s")
        params.append(tier)
    if category is not None:
        conditions.append("category = %s")
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
