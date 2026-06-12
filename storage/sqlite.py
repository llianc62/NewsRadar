# coding=utf-8
"""SQLite database backend — pure CRUD, no cloud sync.

S3 upload of the resulting ``.db`` files is handled at the CLI /
orchestration layer, not here.
"""

import sqlite3
import json
from pathlib import Path
from typing import Dict, List, Optional

from news.models import NewsData, NewsItem


def _tags_to_json(tags: Optional[List[str]]) -> str:
    """Convert tags list to JSON string for SQLite storage."""
    return json.dumps(tags, ensure_ascii=False) if tags else ""


class Sqlite:
    """SQLite database for news items.

    Usage::

        db = Sqlite(data_dir="output")
        db.save_news_data(news_data, source_tiers={"weibo": {"tier": 1, "priority": 10}})
        rows = db.get_unnotified("2026-06-06")
        db.mark_notified("2026-06-06")
        db.cleanup()
    """

    def __init__(
        self,
        data_dir: str = "output",
        timezone: str = "Asia/Shanghai",
    ):
        self.data_dir = Path(data_dir)
        self.timezone = timezone

        # Connection cache: date_str -> sqlite3.Connection
        self._connections: Dict[str, sqlite3.Connection] = {}

    # ── Path helpers ────────────────────────────────────────────────

    def _get_db_path(self, date: str) -> Path:
        """Return ``{data_dir}/db/{date}.db``."""
        path = self.data_dir / "db" / f"{date}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # ── Connection management ───────────────────────────────────────

    def _get_connection(self, date: str) -> sqlite3.Connection:
        """Get or create a cached sqlite3 connection for *date*."""
        if date not in self._connections:
            db_path = self._get_db_path(date)

            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")

            self._init_tables(conn)
            self._connections[date] = conn

        return self._connections[date]

    def _init_tables(self, conn: sqlite3.Connection) -> None:
        """Execute sqlite.sql to create tables, then apply migrations."""
        schema_path = Path(__file__).parent / "sqlite.sql"
        if not schema_path.exists():
            raise FileNotFoundError(f"SQLite schema file not found: {schema_path}")

        with open(schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()

        conn.executescript(schema_sql)

        # ── Migrations — add columns that may not exist in older DBs ──
        migrations = [
            "ALTER TABLE news_items ADD COLUMN category TEXT DEFAULT ''",
            "ALTER TABLE news_items ADD COLUMN tags TEXT DEFAULT ''",
        ]
        for stmt in migrations:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists

        conn.commit()

    # ── Save news data ────────────────────────────────────────────

    def save_news_data(
        self,
        news_data: NewsData,
        source_tiers: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> None:
        """Save *news_data* to SQLite.

        Dedup rules match the partial unique indexes in schema.sql:

        * Hot-list — ``(url, source_id)`` when url is non-empty.
        * RSS — ``(guid, source_id)`` when guid is non-empty.
        * Items without a dedup key always insert as new.

        On match: title, rank, mobile_url, last_crawl_time,
        crawl_count (+1), priority, tier are updated.
        ``notified`` is **never** touched.
        """
        date = news_data.date
        conn = self._get_connection(date)
        cursor = conn.cursor()

        if source_tiers is None:
            source_tiers = {}

        new_total = 0
        updated_total = 0

        for source_id, news_list in news_data.items.items():
            tier_info = source_tiers.get(source_id, {})
            tier = tier_info.get("tier", 4)
            priority = tier_info.get("priority", 0)

            source_new = 0
            source_updated = 0

            for item in news_list:
                try:
                    existing = None

                    # ── Dedup lookup ──────────────────────────
                    if item.source_type == "hotlist" and item.url:
                        cursor.execute(
                            """SELECT id FROM news_items
                               WHERE url = ? AND source_id = ?
                                 AND source_type = 'hotlist'""",
                            (item.url, source_id),
                        )
                        existing = cursor.fetchone()
                    elif item.source_type == "rss" and item.guid:
                        cursor.execute(
                            """SELECT id FROM news_items
                               WHERE guid = ? AND source_id = ?
                                 AND source_type = 'rss'""",
                            (item.guid, source_id),
                        )
                        existing = cursor.fetchone()

                    if existing is not None:
                        cursor.execute(
                            """UPDATE news_items SET
                                title = ?,
                                rank = ?,
                                mobile_url = ?,
                                last_crawl_time = ?,
                                crawl_count = crawl_count + 1,
                                priority = ?,
                                tier = ?,
                                category = ?,
                                tags = ?
                               WHERE id = ?""",
                            (
                                item.title,
                                item.rank,
                                item.mobile_url,
                                item.last_crawl_time,
                                priority,
                                tier,
                                item.category,
                                _tags_to_json(item.tags),
                                existing[0],
                            ),
                        )
                        source_updated += 1
                    else:
                        cursor.execute(
                            """INSERT INTO news_items
                               (title, source_id, source_name, source_type,
                                tier, priority, url, mobile_url, rank,
                                guid, published_at, summary, author,
                                category, tags,
                                notified, first_crawl_time, last_crawl_time,
                                crawl_count)
                               VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?,
                                0, ?, ?, 1
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
                                item.published_at,
                                item.summary,
                                item.author,
                                item.category,
                                _tags_to_json(item.tags),
                                item.first_crawl_time,
                                item.last_crawl_time,
                            ),
                        )
                        source_new += 1

                except sqlite3.Error as e:
                    print(
                        f"[Sqlite] Failed to save item "
                        f"[{item.title[:30]}...]: {e}"
                    )

            if source_new > 0 or source_updated > 0:
                print(
                    f"[Sqlite] {source_id}: "
                    f"{source_new} new, {source_updated} updated"
                )
            new_total += source_new
            updated_total += source_updated

        conn.commit()
        print(
            f"[Sqlite] Saved: {new_total} new, {updated_total} updated "
            f"(date={date})"
        )

    # ── Query methods ───────────────────────────────────────────────

    def get_unnotified(self, date: str) -> List[sqlite3.Row]:
        """Return all unnotified rows, ordered by tier ASC, priority DESC."""
        conn = self._get_connection(date)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """SELECT * FROM news_items
               WHERE notified = 0
               ORDER BY tier ASC, priority DESC"""
        )
        return cursor.fetchall()

    def mark_notified(self, date: str) -> None:
        """Mark every unnotified news item as notified and commit."""
        conn = self._get_connection(date)
        conn.execute(
            "UPDATE news_items SET notified = 1 WHERE notified = 0"
        )
        conn.commit()
        print(f"[Sqlite] Marked items as notified (date={date})")

    # ── Cleanup ─────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Close all cached connections."""
        for conn in self._connections.values():
            try:
                conn.close()
            except Exception:
                pass
        self._connections.clear()

        print("[Sqlite] Cleanup complete")
