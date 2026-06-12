# coding=utf-8
"""PostgreSQL database layer — connection pool, schema init, CRUD.

Wraps ``psycopg2`` ``ThreadedConnectionPool`` inside a ``PostgreSQL``
class (no more module-level global ``_pool``).
"""

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

# Module-level timezone default (used for HH:MM date parsing)
_timezone_offset: str = "+08:00"


def _load_schema() -> str:
    """Read the PostgreSQL schema DDL from schema_postgres.sql."""
    schema_path = Path(__file__).parent / "postgres.sql"
    if not schema_path.exists():
        raise FileNotFoundError(
            f"PostgreSQL schema file not found: {schema_path}"
        )
    return schema_path.read_text(encoding="utf-8")


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


class PostgreSQL:
    """PostgreSQL connection pool and CRUD operations.

    Usage::

        db = PostgreSQL({"host": "localhost", "port": 5432, ...})
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
                cur.execute(_load_schema())
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
            raise RuntimeError("PostgreSQL not connected. Call connect() first.")
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
            item.content,
            item.category if item.category else None,
            item.tags if item.tags else [],
            sync_status, ts_first, ts_last,
            item.ranks if item.ranks else [],
        )

        if item.source_type == "hotlist" and item.url:
            if skip_existing:
                cur.execute(
                    """INSERT INTO news_articles
                       (title, source_id, source_name, source_type,
                        tier, priority, url, mobile_url, rank,
                        guid, published_at, summary, author,
                        content, category, tags,
                        sync_status, notified,
                        first_crawled_at, last_crawled_at, ranks)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s,
                               FALSE, %s, %s, %s)
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
                        content, category, tags,
                        sync_status, notified,
                        first_crawled_at, last_crawled_at, ranks)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s,
                               FALSE, %s, %s, %s)
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
                           category = EXCLUDED.category,
                           tags = EXCLUDED.tags,
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
                        content, category, tags,
                        sync_status, notified,
                        first_crawled_at, last_crawled_at, ranks)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s,
                               FALSE, %s, %s, %s)
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
                        content, category, tags,
                        sync_status, notified,
                        first_crawled_at, last_crawled_at, ranks)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s,
                               FALSE, %s, %s, %s)
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
                           category = EXCLUDED.category,
                           tags = EXCLUDED.tags,
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
                    content, category, tags,
                    sync_status, notified,
                    first_crawled_at, last_crawled_at, ranks)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s,
                           FALSE, %s, %s, %s)""",
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
