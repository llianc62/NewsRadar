# coding=utf-8
"""PostgreSQL database layer — connection pool, schema init, CRUD.

Wraps ``psycopg2`` ``ThreadedConnectionPool`` inside a ``Database``
class (no more module-level global ``_pool``).
"""

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

# Register JSONB adapter
psycopg2.extras.register_default_jsonb(loads=json.loads)

# Module-level timezone default (used for HH:MM date parsing)
_timezone_offset: str = "+08:00"


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

-- Dedup indexes (partial unique, matching SQLite logic)
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


def _to_timestamptz(value: str, fallback_date: Optional[str]) -> Optional[datetime]:
    """Convert a time string (HH:MM or ISO 8601) to a datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    if ":" in value and len(value.split(":")[0]) <= 2 and fallback_date:
        try:
            return datetime.fromisoformat(
                f"{fallback_date}T{value}:00{_timezone_offset}"
            )
        except (ValueError, TypeError):
            pass
    return None


class Database:
    """PostgreSQL connection pool and CRUD operations.

    Usage::

        db = Database({"host": "localhost", "port": 5432, ...})
        db.connect()
        db.init_schema()
        db.save_news_data(news_data, source_tiers)
        articles = db.get_recent_news(limit=10)
        db.close()
    """

    def __init__(self, pg_config: Dict[str, Any]):
        self._config = pg_config
        self._pool: Optional[ThreadedConnectionPool] = None

    # ── Lifecycle ──────────────────────────────────────────────────

    def connect(self) -> None:
        """Create the connection pool."""
        if self._pool is not None:
            return
        self._pool = ThreadedConnectionPool(
            minconn=self._config.get("min_connections", 2),
            maxconn=self._config.get("max_connections", 10),
            host=self._config["host"],
            port=self._config["port"],
            dbname=self._config["database"],
            user=self._config["user"],
            password=self._config["password"],
        )

    def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None
            print("[DB] Connection pool closed")

    def init_schema(self) -> None:
        """Run schema DDL if tables do not yet exist."""
        if self._schema_ready():
            print("[DB] Schema already exists — skipping init.")
            return
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()
            print("[DB] Schema initialized successfully")
        finally:
            self._pool.putconn(conn)

    def _schema_ready(self) -> bool:
        """Check whether the schema tables already exist."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name = 'news_articles'
                    )"""
                )
                return cur.fetchone()[0]
        finally:
            self._pool.putconn(conn)

    @property
    def is_connected(self) -> bool:
        return self._pool is not None

    # ── Connection context manager ─────────────────────────────────

    @contextmanager
    def get_conn(self):
        """Yield a connection from the pool with auto commit/rollback."""
        if self._pool is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    # ── Save news data ─────────────────────────────────────────────

    def save_news_data(
        self,
        news_data,           # NewsData from news.models
        source_tiers: Optional[Dict[str, Dict[str, int]]] = None,
        sync_status: str = "local",
        skip_existing: bool = False,
    ) -> Dict[str, int]:
        """Save NewsData to PostgreSQL with UPSERT logic.

        Dedup: hotlist on (url, source_id), rss on (guid, source_id).
        Each item uses a savepoint so a single failure doesn't poison the batch.
        """
        if source_tiers is None:
            source_tiers = {}

        new_total = 0
        updated_total = 0
        skipped_total = 0

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                for source_id, news_list in news_data.items.items():
                    tier_info = source_tiers.get(source_id, {})
                    tier = tier_info.get("tier", 4)
                    priority = tier_info.get("priority", 0)

                    for item in news_list:
                        try:
                            with conn:
                                with conn.cursor() as item_cur:
                                    self._upsert_one(
                                        item_cur, item, source_id, tier,
                                        priority, news_data.date,
                                        sync_status, skip_existing,
                                    )
                            new_total += 1
                        except psycopg2.Error as e:
                            print(
                                f"[DB] Failed to save item "
                                f"[{item.title[:30]}...]: {e}"
                            )
                            skipped_total += 1

        print(
            f"[DB] Saved: items processed (sync_status={sync_status})"
        )
        return {"new": new_total, "updated": updated_total, "skipped": skipped_total}

    def _upsert_one(
        self,
        cur,
        item,
        source_id: str,
        tier: int,
        priority: int,
        crawl_date: str,
        sync_status: str,
        skip_existing: bool,
    ) -> None:
        """Insert or update a single NewsItem."""
        ts_first = _to_timestamptz(item.first_crawl_time, crawl_date)
        ts_last = _to_timestamptz(item.last_crawl_time, crawl_date)
        ts_pub = _to_timestamptz(item.published_at, None)

        common_values = (
            item.title, source_id, item.source_name, item.source_type,
            tier, priority, item.url, item.mobile_url, item.rank,
            item.guid, ts_pub, item.summary, item.author,
            item.content, sync_status, ts_first, ts_last,
            item.ranks if item.ranks else [],
        )

        if item.source_type == "hotlist" and item.url:
            if skip_existing:
                cur.execute(
                    """INSERT INTO news_articles
                       (title, source_id, source_name, source_type,
                        tier, priority, url, mobile_url, rank,
                        guid, published_at, summary, author,
                        content,
                        sync_status, notified,
                        first_crawled_at, last_crawled_at, ranks)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s)
                       ON CONFLICT (source_id, url)
                       WHERE source_type = 'hotlist' AND url != ''
                       DO NOTHING""",
                    common_values,
                )
            else:
                cur.execute(
                    """INSERT INTO news_articles
                       (title, source_id, source_name, source_type,
                        tier, priority, url, mobile_url, rank,
                        guid, published_at, summary, author,
                        content,
                        sync_status, notified,
                        first_crawled_at, last_crawled_at, ranks)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s)
                       ON CONFLICT (source_id, url)
                       WHERE source_type = 'hotlist' AND url != ''
                       DO UPDATE SET
                           title = EXCLUDED.title,
                           rank = EXCLUDED.rank,
                           mobile_url = EXCLUDED.mobile_url,
                           last_crawled_at = EXCLUDED.last_crawled_at,
                           priority = EXCLUDED.priority,
                           tier = EXCLUDED.tier,
                           summary = EXCLUDED.summary,
                           content = CASE
                               WHEN news_articles.content IS NULL OR news_articles.content = ''
                               THEN EXCLUDED.content
                               ELSE news_articles.content
                           END""",
                    common_values,
                )
        elif item.source_type == "rss" and item.guid:
            if skip_existing:
                cur.execute(
                    """INSERT INTO news_articles
                       (title, source_id, source_name, source_type,
                        tier, priority, url, mobile_url, rank,
                        guid, published_at, summary, author,
                        content,
                        sync_status, notified,
                        first_crawled_at, last_crawled_at, ranks)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s)
                       ON CONFLICT (source_id, guid)
                       WHERE source_type = 'rss' AND guid != ''
                       DO NOTHING""",
                    common_values,
                )
            else:
                cur.execute(
                    """INSERT INTO news_articles
                       (title, source_id, source_name, source_type,
                        tier, priority, url, mobile_url, rank,
                        guid, published_at, summary, author,
                        content,
                        sync_status, notified,
                        first_crawled_at, last_crawled_at, ranks)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s)
                       ON CONFLICT (source_id, guid)
                       WHERE source_type = 'rss' AND guid != ''
                       DO UPDATE SET
                           title = EXCLUDED.title,
                           rank = EXCLUDED.rank,
                           mobile_url = EXCLUDED.mobile_url,
                           last_crawled_at = EXCLUDED.last_crawled_at,
                           priority = EXCLUDED.priority,
                           tier = EXCLUDED.tier,
                           summary = EXCLUDED.summary,
                           content = CASE
                               WHEN news_articles.content IS NULL OR news_articles.content = ''
                               THEN EXCLUDED.content
                               ELSE news_articles.content
                           END""",
                    common_values,
                )
        else:
            cur.execute(
                """INSERT INTO news_articles
                   (title, source_id, source_name, source_type,
                    tier, priority, url, mobile_url, rank,
                    guid, published_at, summary, author,
                    sync_status, notified,
                    first_crawled_at, last_crawled_at, ranks)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, FALSE, %s, %s, %s)""",
                common_values,
            )

    # ── Query methods ──────────────────────────────────────────────

    def get_recent_news(
        self,
        limit: int = 50,
        offset: int = 0,
        tier: Optional[int] = None,
        category: Optional[str] = None,
        min_confidence: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return recent news articles with optional filters."""
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

        with self.get_conn() as conn:
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
        self,
        tier: Optional[int] = None,
        category: Optional[str] = None,
        min_confidence: Optional[int] = None,
    ) -> int:
        """Return total count of news articles matching filters."""
        conditions: List[str] = []
        params: List[Any] = []

        if min_confidence is not None:
            conditions.append("(confidence IS NULL OR confidence >= %s)")
            params.append(min_confidence)
        else:
            conditions.append("(confidence IS NULL OR confidence >= 20)")

        if tier is not None:
            conditions.append("tier = %s")
            params.append(tier)
        if category is not None:
            conditions.append("category = %s")
            params.append(category)

        where = " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM news_articles WHERE {where}",
                    params,
                )
                return cur.fetchone()[0]

    def get_news_by_id(self, article_id: int) -> Optional[Dict[str, Any]]:
        """Return a single article by ID, including content and images."""
        with self.get_conn() as conn:
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

    def get_stats(self) -> Dict[str, Any]:
        """Return dashboard stats: counts by tier, source, and today's new."""
        with self.get_conn() as conn:
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

    def get_articles_without_content(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return articles where content is NULL/empty, ordered by priority."""
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT id, title, url, source_name, tier
                       FROM news_articles
                       WHERE (content IS NULL OR content = '')
                         AND url != ''
                       ORDER BY tier ASC, priority DESC
                       LIMIT %s""",
                    (limit,),
                )
                return cur.fetchall()

    def update_article_content(self, article_id: int, content: str) -> None:
        """Set the content (Markdown) for an article."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE news_articles SET content = %s WHERE id = %s",
                    (content, article_id),
                )

    def save_article_image(
        self,
        article_id: int,
        image_url: str,
        original_url: str = "",
        width: Optional[int] = None,
        height: Optional[int] = None,
        file_size: Optional[int] = None,
        sort_order: int = 0,
    ) -> int:
        """Insert a news_images row and return its ID."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO news_images
                       (article_id, image_url, original_url, width, height, file_size, sort_order)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (article_id, image_url, original_url, width, height, file_size, sort_order),
                )
                return cur.fetchone()[0]
