# coding=utf-8
"""PostgreSQL database layer — connection pool, schema init, CRUD.

Wraps ``psycopg2`` ``ThreadedConnectionPool`` inside a ``PostgreSQL``
class (no more module-level global ``_pool``).
"""

import json
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

# Detect CJK characters for search routing:
#   CJK search  → ILIKE + pg_trgm GIN index
#   ASCII search → FTS (to_tsvector @@ plainto_tsquery)
_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")


def _contains_cjk(text: str) -> bool:
    """Return True if *text* contains any CJK character."""
    return bool(_CJK_RE.search(text))

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
        crawled_at, ranks, heat_score"""

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
        ranks = EXCLUDED.ranks,
        heat_score = EXCLUDED.heat_score,
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
        """Idempotent schema migrations."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                # Migration 001: rebuild full-text index to include content
                # (previous index may have been dropped or never included content)
                cur.execute(
                    """SELECT EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE indexname = 'idx_fulltext'
                          AND indexdef LIKE '%COALESCE(content%'
                    )"""
                )
                has_content_in_index = cur.fetchone()[0]
                if not has_content_in_index:
                    print("[DB] Migrating: rebuilding idx_fulltext to include content...")
                    cur.execute("DROP INDEX IF EXISTS idx_fulltext")
                    cur.execute(
                        """CREATE INDEX idx_fulltext ON news_articles
                           USING GIN (to_tsvector('simple',
                               title || ' ' || COALESCE(summary, '') || ' '
                               || COALESCE(content, '')))"""
                    )
                    conn.commit()
                    print("[DB] Migration complete: idx_fulltext rebuilt with content.")

                # Migration 002: create pg_trgm extension + trigram index for CJK search
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                cur.execute(
                    """SELECT EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE indexname = 'idx_fulltext_trgm'
                    )"""
                )
                has_trgm_index = cur.fetchone()[0]
                if not has_trgm_index:
                    print("[DB] Migrating: creating idx_fulltext_trgm for CJK ILIKE search...")
                    cur.execute(
                        """CREATE INDEX idx_fulltext_trgm ON news_articles
                           USING GIN ((title || ' ' || COALESCE(summary, '')
                           || ' ' || COALESCE(content, '')) gin_trgm_ops)"""
                    )
                    conn.commit()
                    print("[DB] Migration complete: idx_fulltext_trgm created.")

                # Migration 003: create failed_tasks table for failure recording & lazy retry
                cur.execute(
                    """SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name = 'failed_tasks'
                    )"""
                )
                has_failed_tasks = cur.fetchone()[0]
                if not has_failed_tasks:
                    print("[DB] Migrating: creating failed_tasks table...")
                    cur.execute(
                        """CREATE TABLE IF NOT EXISTS failed_tasks (
                            id              BIGSERIAL PRIMARY KEY,
                            task_type       VARCHAR(50) NOT NULL,
                            context         JSONB NOT NULL DEFAULT '{}',
                            retry_times     INTEGER NOT NULL DEFAULT 0,
                            max_retry       INTEGER NOT NULL DEFAULT 3,
                            last_retry      TIMESTAMPTZ DEFAULT NULL,
                            status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                                                CHECK (status IN ('pending', 'failed', 'completed')),
                            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )"""
                    )
                    cur.execute(
                        """CREATE UNIQUE INDEX IF NOT EXISTS idx_failed_tasks_dedup
                           ON failed_tasks (task_type, (context->>'url'))
                           WHERE status = 'pending'"""
                    )
                    cur.execute(
                        """CREATE INDEX IF NOT EXISTS idx_failed_tasks_status
                           ON failed_tasks (status, task_type, retry_times)"""
                    )
                    conn.commit()
                    print("[DB] Migration complete: failed_tasks table created.")

                # Migration 004: change ranks column from SMALLINT[] to JSONB
                # for heat_score tracking with [rank, total] snapshots.
                cur.execute(
                    """SELECT data_type
                       FROM information_schema.columns
                       WHERE table_schema = 'public'
                         AND table_name = 'news_articles'
                         AND column_name = 'ranks'"""
                )
                col_type = cur.fetchone()
                if col_type and col_type[0] == 'ARRAY':
                    print("[DB] Migrating: changing ranks from SMALLINT[] to JSONB...")
                    cur.execute(
                        """ALTER TABLE news_articles
                           ALTER COLUMN ranks TYPE JSONB USING '[]'::jsonb"""
                    )
                    cur.execute(
                        """ALTER TABLE news_articles
                           ALTER COLUMN ranks SET DEFAULT '[]'::jsonb"""
                    )
                    conn.commit()
                    print("[DB] Migration complete: ranks column converted to JSONB.")
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

        # Process heat scores for hotlist items (before building rows).
        # Heat computation needs today's DB snapshot → done per source
        # upfront so _build_row picks up the computed values.
        for source_id, news_list in news_data.items.items():
            hotlist_items = [
                item for item in news_list
                if item.source_type == "hotlist" and item.url
            ]
            if hotlist_items:
                self._process_hotlist_heat(source_id, hotlist_items)

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
        """Convert a NewsItem into a 20-element tuple for batch INSERT."""
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
            json.dumps(item.ranks) if item.ranks else '[]',
            item.heat_score,
        )

    # ── Heat score ──────────────────────────────────────────────────

    @staticmethod
    def _calc_heat_score(
        prev_heat: Optional[int],
        prev_ranks: list,       # [[7,20], [5,20]]
        new_ranks_entry: list,  # [rank, total] from current round
    ) -> int:
        """Calculate heat score, returns 0-100."""
        new_rank, new_total = new_ranks_entry
        if not prev_ranks or prev_heat is None:
            # First appearance: percentile
            return round(max(0, min(100, (1 - new_rank / new_total) * 100)))

        # Still on the list: incremental adjustment
        last_r, last_t = prev_ranks[-1]
        last_pct = (1 - last_r / last_t) * 100
        new_pct = (1 - new_rank / new_total) * 100
        delta = new_pct - last_pct  # percentage-point difference

        return round(max(0, min(100, prev_heat + delta * 0.3)))

    def _process_hotlist_heat(
        self,
        source_id: str,
        items: list,
    ) -> None:
        """Process heat score for hotlist items of one source.

        Compares this round's items against today's DB records for the
        same source, then classifies each URL as:

        - New (first appearance) → percentile-based score
        - Existing (still on list) → delta-adjusted score
        - Dropped (in DB but not this round) → ×0.7 decay

        Items are mutated in-place: ``heat_score`` and ``ranks`` are set.
        """
        # Filter: only items with valid ranking data participate in heat
        # calculation.  Items without ranks (e.g. RSS, or synced data)
        # are skipped — they keep their existing heat_score.
        valid_items = [it for it in items if it.ranks]

        # ① Query today's DB records for this source (as previous snapshot)
        db_map: dict = {}
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT url, heat_score, ranks
                       FROM news_articles
                       WHERE source_id = %s
                         AND source_type = 'hotlist'
                         AND crawled_at::date = CURRENT_DATE""",
                    (source_id,),
                )
                for row in cur.fetchall():
                    db_map[row["url"]] = {
                        "heat_score": row["heat_score"],
                        "ranks": row["ranks"] if row["ranks"] else [],
                    }

        # ② Compare sets
        this_urls = {item.url for item in valid_items if item.url}
        db_urls = set(db_map.keys())

        new_urls = this_urls - db_urls
        existing_urls = this_urls & db_urls
        dropped_urls = db_urls - this_urls

        # ③ First appearance — percentile
        for item in valid_items:
            if item.url in new_urls:
                r, t = item.ranks[0]
                item.heat_score = round(
                    max(0, min(100, (1 - r / t) * 100))
                )
                # Keep only the latest ranks entry for history tracking
                item.ranks = [[r, t]]

        # ④ Still on list — delta adjustment
        for item in valid_items:
            if item.url in existing_urls:
                prev = db_map[item.url]
                item.heat_score = PostgreSQL._calc_heat_score(
                    prev_heat=prev["heat_score"],
                    prev_ranks=prev["ranks"],
                    new_ranks_entry=item.ranks[0],
                )
                item.ranks = (prev["ranks"] or []) + [item.ranks[0]]

        # ⑤ Dropped from list — ×0.7 decay
        if dropped_urls:
            with self.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE news_articles
                           SET heat_score = CAST(
                               ROUND(GREATEST(0, LEAST(100,
                                   COALESCE(heat_score, 0) * 0.7
                               ))) AS INTEGER
                           )
                           WHERE source_id = %s
                             AND source_type = 'hotlist'
                             AND url = ANY(%s)""",
                        (source_id, list(dropped_urls)),
                    )
            print(
                f"[DB] Heat decay: {len(dropped_urls)} URLs dropped"
                f" from {source_id}"
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
        keywords: Optional[List[str]] = None,
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
        if keywords:
            for kw in keywords:
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')"
                    " || ' ' || array_to_string(tags, ' ')) ILIKE %s"
                )
                params.append(f"%{kw}%")
        if search is not None:
            if _contains_cjk(search):
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')) ILIKE %s"
                )
                params.append(f"%{search}%")
            else:
                conditions.append(
                    "to_tsvector('simple', title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, ''))"
                    " @@ plainto_tsquery('simple', %s)"
                )
                params.append(search)
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
        keywords: Optional[List[str]] = None,
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
        if keywords:
            for kw in keywords:
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')"
                    " || ' ' || array_to_string(tags, ' ')) ILIKE %s"
                )
                params.append(f"%{kw}%")
        if search is not None:
            if _contains_cjk(search):
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')) ILIKE %s"
                )
                params.append(f"%{search}%")
            else:
                conditions.append(
                    "to_tsvector('simple', title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, ''))"
                    " @@ plainto_tsquery('simple', %s)"
                )
                params.append(search)
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
        keywords: Optional[List[str]] = None,
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
        if keywords:
            for kw in keywords:
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')"
                    " || ' ' || array_to_string(tags, ' ')) ILIKE %s"
                )
                params.append(f"%{kw}%")
        if search is not None:
            if _contains_cjk(search):
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')) ILIKE %s"
                )
                params.append(f"%{search}%")
            else:
                conditions.append(
                    "to_tsvector('simple', title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, ''))"
                    " @@ plainto_tsquery('simple', %s)"
                )
                params.append(search)
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
            if _contains_cjk(search):
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')) ILIKE %s"
                )
                params.append(f"%{search}%")
            else:
                conditions.append(
                    "to_tsvector('simple', title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, ''))"
                    " @@ plainto_tsquery('simple', %s)"
                )
                params.append(search)
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
        keywords: Optional[List[str]] = None,
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
        if keywords:
            for kw in keywords:
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')"
                    " || ' ' || array_to_string(tags, ' ')) ILIKE %s"
                )
                params.append(f"%{kw}%")
        if search is not None:
            if _contains_cjk(search):
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')) ILIKE %s"
                )
                params.append(f"%{search}%")
            else:
                conditions.append(
                    "to_tsvector('simple', title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, ''))"
                    " @@ plainto_tsquery('simple', %s)"
                )
                params.append(search)
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
            if _contains_cjk(search):
                conditions.append(
                    "(title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, '')) ILIKE %s"
                )
                params.append(f"%{search}%")
            else:
                conditions.append(
                    "to_tsvector('simple', title || ' ' || COALESCE(summary, '')"
                    " || ' ' || COALESCE(content, ''))"
                    " @@ plainto_tsquery('simple', %s)"
                )
                params.append(search)
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

    def update_article_full(
        self,
        article_id: int,
        title: str = "",
        content: str = "",
        published_at=None,
        author: str = "",
        summary: str = "",
        category: str = "",
        tags: list | None = None,
    ) -> bool:
        """Update all content and metadata fields after a refetch.

        Unlike the UPSERT path (which preserves non-empty content on
        conflict), this unconditionally overwrites every field so the
        DB stays consistent with what the parser extracted — including
        ``published_at`` which drives the ``/media/`` image path
        resolution in the web layer.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE news_articles
                       SET title = COALESCE(NULLIF(%s, ''), title),
                           content = %s,
                           published_at = COALESCE(NULLIF(%s, ''), published_at),
                           author = COALESCE(NULLIF(%s, ''), author),
                           summary = COALESCE(NULLIF(%s, ''), summary),
                           category = COALESCE(NULLIF(%s, ''), category),
                           tags = COALESCE(%s, tags),
                           updated_at = NOW()
                       WHERE id = %s""",
                    (title, content, published_at, author, summary,
                     category, tags, article_id),
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

    # ── Failed tasks (failure recording & lazy retry) ──────────────

    def record_failure(
        self,
        task_type: str,
        context: dict,
        max_retry: int = 3,
    ) -> Optional[int]:
        """Record a failed task for later retry.

        Uses ``INSERT ... ON CONFLICT DO NOTHING`` so duplicate pending
        tasks for the same URL + task_type are silently ignored.

        Returns:
            The new task ``id``, or ``None`` if a pending task for the
            same URL + task_type already exists.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO failed_tasks (task_type, context, max_retry)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (task_type, (context->>'url'))
                       WHERE status = 'pending'
                       DO NOTHING
                       RETURNING id""",
                    (task_type, json.dumps(context), max_retry),
                )
                row = cur.fetchone()
                return row[0] if row else None

    def get_pending_failures(
        self,
        task_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return pending failed tasks where retry_times < max_retry.

        Args:
            task_type: Optional filter.  When ``None``, returns all types.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                if task_type:
                    cur.execute(
                        """SELECT id, task_type, context, retry_times, max_retry,
                                  last_retry, status, created_at, updated_at
                           FROM failed_tasks
                           WHERE status = 'pending'
                             AND retry_times < max_retry
                             AND task_type = %s
                           ORDER BY created_at""",
                        (task_type,),
                    )
                else:
                    cur.execute(
                        """SELECT id, task_type, context, retry_times, max_retry,
                                  last_retry, status, created_at, updated_at
                           FROM failed_tasks
                           WHERE status = 'pending'
                             AND retry_times < max_retry
                           ORDER BY created_at"""
                    )
                rows = cur.fetchall()
                return [
                    {
                        "id": r[0],
                        "task_type": r[1],
                        "context": r[2],
                        "retry_times": r[3],
                        "max_retry": r[4],
                        "last_retry": r[5],
                        "status": r[6],
                        "created_at": r[7],
                        "updated_at": r[8],
                    }
                    for r in rows
                ]

    def article_has_content(self, url: str) -> bool:
        """Check whether any article with *url* already has non-empty content.

        Used to skip content_fetch retry when the article was already
        fetched successfully through the normal crawl path.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT 1 FROM news_articles
                       WHERE url = %s
                         AND content IS NOT NULL
                         AND content != ''
                       LIMIT 1""",
                    (url,),
                )
                return cur.fetchone() is not None

    def mark_failure_completed(self, task_id: int) -> None:
        """Mark a failed task as successfully retried."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE failed_tasks
                       SET status = 'completed',
                           updated_at = NOW()
                       WHERE id = %s
                         AND status = 'pending'""",
                    (task_id,),
                )

    def mark_failure_retried(self, task_id: int, error: str = "") -> None:
        """Increment retry_times and set last_retry.

        If retry_times reaches max_retry after increment, set status to
        ``'failed'`` (permanent).
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE failed_tasks
                       SET retry_times = retry_times + 1,
                           last_retry = NOW(),
                           updated_at = NOW(),
                           status = CASE
                               WHEN retry_times + 1 >= max_retry
                               THEN 'failed'
                               ELSE 'pending'
                           END
                       WHERE id = %s
                         AND status = 'pending'""",
                    (task_id,),
                )

    def find_articles_by_image_url(self, image_url: str) -> List[int]:
        """Return article IDs whose content contains *image_url*."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id FROM news_articles
                       WHERE position(%s in content) > 0""",
                    (image_url,),
                )
                return [r[0] for r in cur.fetchall()]

    def update_article_image_url(
        self,
        article_id: int,
        old_url: str,
        new_path: str,
    ) -> None:
        """Replace *old_url* with *new_path* in an article's content."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE news_articles
                       SET content = REPLACE(content, %s, %s),
                           updated_at = NOW()
                       WHERE id = %s""",
                    (old_url, new_path, article_id),
                )
