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
from utils import format_datetime_now


def _tags_to_json(tags: Optional[List[str]]) -> str:
    """Convert tags list to JSON string for SQLite storage."""
    return json.dumps(tags, ensure_ascii=False) if tags else ""


class Sqlite:
    """SQLite database for news items.

    Usage::

        db = Sqlite(data_dir="output")
        db.save_news_data(news_data, source_tiers={"weibo": {"tier": 1, "priority": 10}})
        rows = db.get_all("2026-06-06")
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
        """Save *news_data* to SQLite with INSERT OR IGNORE.

        Dedup is enforced by the partial unique indexes in schema.sql:

        * Hot-list — ``(source_id, url)`` when url is non-empty.
        * RSS — ``(source_id, guid)`` when guid is non-empty.
        * Items without a dedup key always insert as new.

        On conflict (matching dedup key), the row is silently ignored.
        No updates are performed — SQLite is write-once per item per day.
        """
        date = news_data.date
        conn = self._get_connection(date)
        cursor = conn.cursor()

        if source_tiers is None:
            source_tiers = {}

        created_at = format_datetime_now(self.timezone)

        inserted_total = 0

        for source_id, news_list in news_data.items.items():
            tier_info = source_tiers.get(source_id, {})
            tier = tier_info.get("tier", 4)
            priority = tier_info.get("priority", 0)

            source_inserted = 0

            for item in news_list:
                try:
                    cursor.execute(
                        """INSERT OR IGNORE INTO news_items
                           (title, source_id, source_name, source_type,
                            tier, priority, url, mobile_url, rank,
                            guid, published_at, summary, author,
                            category, tags, created_at)
                           VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?
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
                            created_at,
                        ),
                    )
                    if cursor.rowcount > 0:
                        source_inserted += 1

                except sqlite3.Error as e:
                    print(
                        f"[Sqlite] Failed to save item "
                        f"[{item.title[:30]}...]: {e}"
                    )

            if source_inserted > 0:
                print(
                    f"[Sqlite] {source_id}: "
                    f"{source_inserted} inserted"
                )
            inserted_total += source_inserted

        conn.commit()
        print(
            f"[Sqlite] Saved: {inserted_total} inserted "
            f"(date={date})"
        )

    # ── Query methods ───────────────────────────────────────────────

    def get_all(self, date: str) -> List[sqlite3.Row]:
        """Return all rows for *date*, ordered by tier ASC, priority DESC."""
        conn = self._get_connection(date)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """SELECT * FROM news_items
               ORDER BY tier ASC, priority DESC"""
        )
        return cursor.fetchall()

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
