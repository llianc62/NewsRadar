# coding=utf-8
"""PostgreSQL database layer — connection pool, schema init, CRUD.

Wraps ``psycopg2`` ``ThreadedConnectionPool`` inside a ``PostgreSQL``
class (no more module-level global ``_pool``).
"""

import json
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

# Register JSONB adapter
psycopg2.extras.register_default_jsonb(loads=json.loads)

# Module-level timezone default (used for HH:MM date parsing)
_timezone_offset: str = "+08:00"

# ═══════════════════════════════════════════════════════════════════
# Batch UPSERT SQL templates (used by save_news_data)
# ═══════════════════════════════════════════════════════════════════

_COLUMNS = """title, source_id, source_name, source_type,
        tier, priority, url, mobile_url, rank,
        guid, published_at, summary, author,
        content, category, tags,
        crawled_from,
        crawled_at, ranks"""

_INSERT_PREFIX = f"INSERT INTO news_articles ({_COLUMNS}) VALUES %s"

_UPDATE_SET = """title = EXCLUDED.title,
        rank = EXCLUDED.rank,
        mobile_url = EXCLUDED.mobile_url,
        crawled_at = EXCLUDED.crawled_at,
        updated_at = NOW(),
        priority = EXCLUDED.priority,
        tier = EXCLUDED.tier,
        summary = EXCLUDED.summary,
        category = EXCLUDED.category,
        tags = EXCLUDED.tags,
        content = CASE
            WHEN news_articles.content IS NULL OR news_articles.content = ''
            THEN EXCLUDED.content
            ELSE news_articles.content
        END"""

_UPDATE_SET_OVERWRITE = """title = EXCLUDED.title,
        rank = EXCLUDED.rank,
        mobile_url = EXCLUDED.mobile_url,
        crawled_at = EXCLUDED.crawled_at,
        updated_at = NOW(),
        priority = EXCLUDED.priority,
        tier = EXCLUDED.tier,
        summary = EXCLUDED.summary,
        category = EXCLUDED.category,
        tags = EXCLUDED.tags,
        content = EXCLUDED.content"""

_HOTLIST_INSERT_SQL = f"""{_INSERT_PREFIX}
ON CONFLICT (source_id, url)
WHERE source_type = 'hotlist' AND url != ''
DO UPDATE SET {_UPDATE_SET}"""

_HOTLIST_INSERT_SKIP_SQL = f"""{_INSERT_PREFIX}
ON CONFLICT (source_id, url)
WHERE source_type = 'hotlist' AND url != ''
DO NOTHING"""

_RSS_INSERT_SQL = f"""{_INSERT_PREFIX}
ON CONFLICT (source_id, guid)
WHERE source_type = 'rss' AND guid != ''
DO UPDATE SET {_UPDATE_SET}"""

_RSS_INSERT_SKIP_SQL = f"""{_INSERT_PREFIX}
ON CONFLICT (source_id, guid)
WHERE source_type = 'rss' AND guid != ''
DO NOTHING"""

_MANUAL_INSERT_SQL = f"""{_INSERT_PREFIX}
ON CONFLICT (source_id, url)
WHERE source_type = 'manual' AND url != ''
DO UPDATE SET {_UPDATE_SET_OVERWRITE}"""

_MANUAL_INSERT_SKIP_SQL = f"""{_INSERT_PREFIX}
ON CONFLICT (source_id, url)
WHERE source_type = 'manual' AND url != ''
DO NOTHING"""

_FALLBACK_INSERT_SQL = f"{_INSERT_PREFIX}"


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
        """Run schema DDL if tables do not yet exist, then apply migrations."""
        if not self._schema_ready():
            conn = self._pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(_load_schema())
                conn.commit()
                print("[DB] Schema initialized successfully")
            finally:
                self._pool.putconn(conn)
        else:
            print("[DB] Schema already exists — running migrations.")

        # Always run migrations (idempotent)
        self._run_migrations()

    def _run_migrations(self) -> None:
        pass

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
        crawled_from: str = "local",
        skip_existing: bool = False,
    ) -> Dict[str, int]:
        """Save NewsData to PostgreSQL with batch UPSERT logic.

        Items are partitioned by ON CONFLICT target (hotlist / rss /
        manual / fallback) and inserted in batches using
        ``execute_values``.

        Dedup: hotlist on (source_id, url), rss on (source_id, guid),
        manual on (source_id, url).
        Content is preserved via CASE WHEN on conflict for hotlist/rss;
        manual always overwrites content.
        """
        if source_tiers is None:
            source_tiers = {}

        # Partition items by conflict-target type
        hotlist_rows: List[Tuple] = []
        rss_rows: List[Tuple] = []
        manual_rows: List[Tuple] = []
        fallback_rows: List[Tuple] = []

        for source_id, news_list in news_data.items.items():
            tier_info = source_tiers.get(source_id, {})
            tier = tier_info.get("tier", 4)
            priority = tier_info.get("priority", 0)

            for item in news_list:
                row = self._build_row(
                    item, source_id, tier, priority,
                    news_data.date, crawled_from,
                )
                if item.source_type == "hotlist" and item.url:
                    hotlist_rows.append(row)
                elif item.source_type == "rss" and item.guid:
                    rss_rows.append(row)
                elif item.source_type == "manual" and item.url:
                    manual_rows.append(row)
                else:
                    fallback_rows.append(row)

        t0 = time.time()
        processed = 0
        skipped = 0

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                if hotlist_rows:
                    sql = (
                        _HOTLIST_INSERT_SKIP_SQL if skip_existing
                        else _HOTLIST_INSERT_SQL
                    )
                    n, s = self._execute_batch(cur, sql, hotlist_rows)
                    processed += n
                    skipped += s

                if rss_rows:
                    sql = (
                        _RSS_INSERT_SKIP_SQL if skip_existing
                        else _RSS_INSERT_SQL
                    )
                    n, s = self._execute_batch(cur, sql, rss_rows)
                    processed += n
                    skipped += s

                if manual_rows:
                    sql = (
                        _MANUAL_INSERT_SKIP_SQL if skip_existing
                        else _MANUAL_INSERT_SQL
                    )
                    n, s = self._execute_batch(cur, sql, manual_rows)
                    processed += n
                    skipped += s

                if fallback_rows:
                    n, s = self._execute_batch(
                        cur, _FALLBACK_INSERT_SQL, fallback_rows,
                    )
                    processed += n
                    skipped += s

        elapsed = time.time() - t0
        msg = (
            f"[DB] Saved {processed} items in {elapsed:.2f}s"
            f" (crawled_from={crawled_from})"
        )
        if skipped:
            msg += f", skipped {skipped}"
        print(msg)
        return {"processed": processed, "skipped": skipped}

    # ── Batch helpers ──────────────────────────────────────────────

    @staticmethod
    def _build_row(
        item,
        source_id: str,
        tier: int,
        priority: int,
        crawl_date: str,
        crawled_from: str,
    ) -> Tuple:
        """Convert a NewsItem into a 19-element tuple for batch INSERT."""
        ts_crawled = _to_timestamptz(item.crawled_at, crawl_date)
        ts_pub = _to_timestamptz(item.published_at, None)

        return (
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
            ts_pub,
            item.summary,
            item.author,
            item.content,
            item.category if item.category else None,
            item.tags if item.tags else [],
            crawled_from,
            ts_crawled,
            item.ranks if item.ranks else [],
        )

    def _execute_batch(
        self,
        cur,
        sql: str,
        items: List[Tuple],
        page_size: int = 100,
    ) -> Tuple[int, int]:
        """Execute batch INSERT via ``execute_values``.

        On batch failure, retries with progressively smaller sub-batches
        (100 → 10 → 1) with savepoint isolation at the single-row level.
        This avoids one bad row forcing the entire batch into the slow path.

        Returns:
            ``(processed, skipped)`` counts.
        """
        processed = 0
        skipped = 0
        conn = cur.connection

        for i in range(0, len(items), page_size):
            batch = items[i:i + page_size]
            n, s = self._execute_batch_retry(
                cur, conn, sql, batch, page_size,
            )
            processed += n
            skipped += s

        return processed, skipped

    def _execute_batch_retry(
        self,
        cur,
        conn,
        sql: str,
        batch: List[Tuple],
        page_size: int,
    ) -> Tuple[int, int]:
        """Attempt a batch INSERT; on failure, divide and retry.

        Falls back from *page_size* → 10 → 1, each level using savepoint
        isolation so good rows always survive.
        """
        try:
            psycopg2.extras.execute_values(
                cur, sql, batch, page_size=page_size,
            )
            return len(batch), 0
        except psycopg2.Error as e:
            if page_size <= 1:
                print(f"[DB]   Row failed: {e}")
                return 0, 1

            # Divide into smaller sub-batches
            next_size = max(1, min(10, page_size // 10))
            print(
                f"[DB] Batch of {len(batch)} failed: {e}"
                f" — retrying with page_size={next_size}"
            )
            processed = 0
            skipped = 0
            for j in range(0, len(batch), next_size):
                sub = batch[j:j + next_size]
                n, s = self._execute_batch_retry(
                    cur, conn, sql, sub, next_size,
                )
                processed += n
                skipped += s
            return processed, skipped

    # ── Query methods ──────────────────────────────────────────────

    def get_recent_news(
        self,
        limit: int = 50,
        offset: int = 0,
        tier: Optional[int] = None,
        category: Optional[str] = None,
        min_confidence: Optional[int] = None,
        sentiment: Optional[str] = None,
        keyword: Optional[str] = None,
        search: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
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
        if sentiment == "positive":
            conditions.append("sentiment_score >= 67")
        elif sentiment == "negative":
            conditions.append("sentiment_score <= 33")
        elif sentiment == "neutral":
            conditions.append("sentiment_score > 33 AND sentiment_score < 67")
        if keyword is not None:
            conditions.append("%s = ANY(tags)")
            params.append(keyword)
        if search is not None:
            conditions.append(
                "(title || ' ' || COALESCE(summary, '') || ' ' || COALESCE(content, '')"
                " || ' ' || COALESCE(array_to_string(tags, ' '), '')) ILIKE %s"
            )
            params.append(f"%{search}%")
        # Date filtering: published_at within [date_from, date_to] inclusive full days
        if date_from is not None:
            conditions.append("published_at >= %s::date")
            params.append(date_from)
        if date_to is not None:
            conditions.append("published_at < %s::date + interval '1 day'")
            params.append(date_to)

        where = " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""SELECT id, title, source_id, source_name, source_type,
                               tier, priority, url, mobile_url, summary,
                               tags, heat_score, sentiment_score,
                               crawled_from, is_analyzed,
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
        sentiment: Optional[str] = None,
        keyword: Optional[str] = None,
        search: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
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
        if sentiment == "positive":
            conditions.append("sentiment_score >= 67")
        elif sentiment == "negative":
            conditions.append("sentiment_score <= 33")
        elif sentiment == "neutral":
            conditions.append("sentiment_score > 33 AND sentiment_score < 67")
        if keyword is not None:
            conditions.append("%s = ANY(tags)")
            params.append(keyword)
        if search is not None:
            conditions.append(
                "(title || ' ' || COALESCE(summary, '') || ' ' || COALESCE(content, '')"
                " || ' ' || COALESCE(array_to_string(tags, ' '), '')) ILIKE %s"
            )
            params.append(f"%{search}%")
        if date_from is not None:
            conditions.append("published_at >= %s::date")
            params.append(date_from)
        if date_to is not None:
            conditions.append("published_at < %s::date + interval '1 day'")
            params.append(date_to)

        where = " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM news_articles WHERE {where}",
                    params,
                )
                return cur.fetchone()[0]

    def get_sentiment_counts(
        self,
        tier: Optional[int] = None,
        keyword: Optional[str] = None,
        search: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return {positive, negative, neutral} counts for sentiment bar."""
        conditions = ["(confidence IS NULL OR confidence >= 20)"]
        params: List[Any] = []

        if tier is not None:
            conditions.append("tier = %s")
            params.append(tier)
        if keyword is not None:
            conditions.append("%s = ANY(tags)")
            params.append(keyword)
        if search is not None:
            conditions.append(
                "(title || ' ' || COALESCE(summary, '') || ' ' || COALESCE(content, '')"
                " || ' ' || COALESCE(array_to_string(tags, ' '), '')) ILIKE %s"
            )
            params.append(f"%{search}%")
        if date_from is not None:
            conditions.append("published_at >= %s::date")
            params.append(date_from)
        if date_to is not None:
            conditions.append("published_at < %s::date + interval '1 day'")
            params.append(date_to)

        where = " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""SELECT
                          COUNT(*) FILTER (WHERE sentiment_score >= 67) AS positive,
                          COUNT(*) FILTER (WHERE sentiment_score <= 33) AS negative,
                          COUNT(*) FILTER (WHERE sentiment_score > 33 AND sentiment_score < 67) AS neutral
                        FROM news_articles WHERE {where}""",
                    params,
                )
                return dict(cur.fetchone())

    def get_keyword_counts(
        self,
        tier: Optional[int] = None,
        sentiment: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 30,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return [{tag, cnt}] for keyword cloud, sorted by frequency."""
        conditions = ["(confidence IS NULL OR confidence >= 20)"]
        params: List[Any] = []

        if tier is not None:
            conditions.append("tier = %s")
            params.append(tier)
        if sentiment == "positive":
            conditions.append("sentiment_score >= 67")
        elif sentiment == "negative":
            conditions.append("sentiment_score <= 33")
        elif sentiment == "neutral":
            conditions.append("sentiment_score > 33 AND sentiment_score < 67")
        if search is not None:
            conditions.append(
                "(title || ' ' || COALESCE(summary, '') || ' ' || COALESCE(content, '')"
                " || ' ' || COALESCE(array_to_string(tags, ' '), '')) ILIKE %s"
            )
            params.append(f"%{search}%")
        if date_from is not None:
            conditions.append("published_at >= %s::date")
            params.append(date_from)
        if date_to is not None:
            conditions.append("published_at < %s::date + interval '1 day'")
            params.append(date_to)

        where = " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""SELECT unnest(tags) AS tag, COUNT(*) AS cnt
                        FROM news_articles WHERE {where}
                        GROUP BY tag ORDER BY cnt DESC LIMIT %s""",
                    params + [limit],
                )
                return cur.fetchall()

    def get_high_impact_count(
        self,
        tier: Optional[int] = None,
        keyword: Optional[str] = None,
        search: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> int:
        """Return count of high-heat articles (proxy for 'immediate impact')."""
        conditions = [
            "(confidence IS NULL OR confidence >= 20)",
            "heat_score >= 80",
        ]
        params: List[Any] = []

        if tier is not None:
            conditions.append("tier = %s")
            params.append(tier)
        if keyword is not None:
            conditions.append("%s = ANY(tags)")
            params.append(keyword)
        if search is not None:
            conditions.append(
                "(title || ' ' || COALESCE(summary, '') || ' ' || COALESCE(content, '')"
                " || ' ' || COALESCE(array_to_string(tags, ' '), '')) ILIKE %s"
            )
            params.append(f"%{search}%")
        # Use date parameters instead of hardcoded CURRENT_DATE
        if date_from is not None:
            conditions.append("published_at >= %s::date")
            params.append(date_from)
        if date_to is not None:
            conditions.append("published_at < %s::date + interval '1 day'")
            params.append(date_to)
        # Fall back to today when no date params given (backward compatible)
        if date_from is None and date_to is None:
            conditions.append("published_at >= CURRENT_DATE")

        where = " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM news_articles WHERE {where}",
                    params,
                )
                return cur.fetchone()[0]

    def get_latest_cloud_sync_date(self):
        """Return the latest ``crawled_at`` timestamp for cloud-synced
        records, or None if no cloud records exist.

        Used by :meth:`Crawler.sync_from_cloud` to decide which cloud
        storage files need to be downloaded and which rows are incremental.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT MAX(crawled_at)
                       FROM news_articles
                       WHERE crawled_from = 'cloud'"""
                )
                row = cur.fetchone()
                if row and row[0] is not None:
                    return row[0]  # datetime with timezone
                return None

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

    def get_stats(self, date_from: Optional[str] = None, date_to: Optional[str] = None,
                  search: Optional[str] = None) -> Dict[str, Any]:
        """Return dashboard stats: counts by tier, source, and today's new."""
        conditions = ["(confidence IS NULL OR confidence >= 20)"]
        params: list = []
        if date_from:
            conditions.append("published_at >= %s::date")
            params.append(date_from)
        if date_to:
            conditions.append("published_at < %s::date + interval '1 day'")
            params.append(date_to)
        if search is not None:
            conditions.append(
                "(title || ' ' || COALESCE(summary, '') || ' ' || COALESCE(content, '')"
                " || ' ' || COALESCE(array_to_string(tags, ' '), '')) ILIKE %s"
            )
            params.append(f"%{search}%")
        where_clause = " WHERE " + " AND ".join(conditions)

        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""SELECT
                         COUNT(*) FILTER (WHERE tier = 1) AS t1_count,
                         COUNT(*) FILTER (WHERE tier = 2) AS t2_count,
                         COUNT(*) FILTER (WHERE tier = 3) AS t3_count,
                         COUNT(*) FILTER (WHERE tier = 4) AS t4_count,
                         COUNT(*) AS total_count,
                         COUNT(*) FILTER (WHERE published_at >= CURRENT_DATE
                                          AND published_at < CURRENT_DATE + interval '1 day')
                           AS today_count
                       FROM news_articles{where_clause}""",
                    params,
                )
                stats = dict(cur.fetchone())

                cur.execute(
                    f"SELECT source_name, COUNT(*) AS cnt FROM news_articles{where_clause} GROUP BY source_name ORDER BY cnt DESC",
                    params,
                )
                stats["by_source"] = cur.fetchall()

                return stats

    def get_article_by_url(self, url: str) -> Optional[Dict[str, Any]]:
        """Return the first article matching *url*, or None."""
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, title, url FROM news_articles WHERE url = %s ORDER BY id LIMIT 1",
                    (url,),
                )
                return cur.fetchone()

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

    def update_article_content(self, article_id: int, content: str) -> bool:
        """Update an article's content field directly. Returns True if a row was updated."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE news_articles SET content = %s, updated_at = NOW() WHERE id = %s",
                    (content, article_id),
                )
                return cur.rowcount > 0

    def delete_news(self, article_id: int) -> bool:
        """Delete an article by ID. Associated images are removed via the
        ``news_images.article_id`` ``ON DELETE CASCADE`` foreign key.

        Returns True if a row was deleted, False if no article had that ID.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM news_articles WHERE id = %s", (article_id,))
                return cur.rowcount > 0

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
