# coding=utf-8
"""SQLite storage with optional S3 sync (cloud crawler backend)."""

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from news.models import NewsData, NewsItem
from storage.s3 import S3Client


class Storage:
    """SQLite storage with optional S3 sync.

    Usage::

        s = Storage(data_dir="output", s3_config={...})
        s.save_news_data(news_data, source_tiers={"weibo": {"tier": 1, "priority": 10}})
        rows = s.get_unnotified("2026-06-06")
        s.mark_notified("2026-06-06")
        s.cleanup()
    """

    def __init__(
        self,
        data_dir: str = "output",
        timezone: str = "Asia/Shanghai",
        s3_config: Optional[Dict[str, str]] = None,
        s3_client: Optional[S3Client] = None,
    ):
        self.data_dir = Path(data_dir)
        self.timezone = timezone

        # Connection cache: date_str -> sqlite3.Connection
        self._connections: Dict[str, sqlite3.Connection] = {}

        # Temp files tracked for cleanup()
        self._temp_files: List[Path] = []

        # S3 client (can be injected or built from config)
        if s3_client is not None:
            self._s3 = s3_client
        elif s3_config:
            self._s3 = S3Client.from_config(s3_config)
        else:
            self._s3 = None

        if self._s3:
            print(f"[Storage] S3 enabled, bucket={self._s3.bucket_name}")

    # ── Path helpers ────────────────────────────────────────────────

    def _get_db_path(self, date: str) -> Path:
        """Return ``{data_dir}/news/{date}.db``."""
        path = self.data_dir / "news" / f"{date}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _s3_key(self, date: str) -> str:
        """Return S3 object key: ``news/{date}.db``."""
        return f"news/{date}.db"

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
        """Execute schema.sql to create tables."""
        # First try storage package schema, then root level (backward compat)
        schema_path = Path(__file__).parent / "schema.sql"
        if not schema_path.exists():
            schema_path = Path(__file__).parent.parent / "schema.sql"
        if not schema_path.exists():
            raise FileNotFoundError("Schema file not found: schema.sql")

        with open(schema_path, "r", encoding="utf-8") as f:
            schema_sql = f.read()

        conn.executescript(schema_sql)
        conn.commit()

    # ── Save news data (with S3 sync) ───────────────────────────────

    def save_news_data(
        self,
        news_data: NewsData,
        source_tiers: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> None:
        """Save *news_data* to SQLite, syncing with S3.

        Dedup rules match the partial unique indexes in schema.sql:

        * Hot-list — ``(url, source_id)`` when url is non-empty.
        * RSS — ``(guid, source_id)`` when guid is non-empty.
        * Items without a dedup key always insert as new.

        On match: title, rank, mobile_url, last_crawl_time,
        crawl_count (+1), priority, tier are updated.
        ``notified`` is **never** touched.
        """
        date = news_data.date

        # ── S3 pre-fetch ──────────────────────────────────────────
        if self._s3:
            key = self._s3_key(date)
            tmp_path = self._s3.download_to_temp(key)
            if tmp_path:
                local_path = self._get_db_path(date)

                # Close any cached connection so we reopen against
                # the freshly-copied file
                if date in self._connections:
                    self._connections[date].close()
                    del self._connections[date]

                shutil.copy2(str(tmp_path), str(local_path))
                self._temp_files.append(tmp_path)

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
                                tier = ?
                               WHERE id = ?""",
                            (
                                item.title,
                                item.rank,
                                item.mobile_url,
                                item.last_crawl_time,
                                priority,
                                tier,
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
                                notified, first_crawl_time, last_crawl_time,
                                crawl_count)
                               VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?,
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
                                item.first_crawl_time,
                                item.last_crawl_time,
                            ),
                        )
                        source_new += 1

                except sqlite3.Error as e:
                    print(
                        f"[Storage] Failed to save item "
                        f"[{item.title[:30]}...]: {e}"
                    )

            if source_new > 0 or source_updated > 0:
                print(
                    f"[Storage] {source_id}: "
                    f"{source_new} new, {source_updated} updated"
                )
            new_total += source_new
            updated_total += source_updated

        conn.commit()
        print(
            f"[Storage] Saved: {new_total} new, {updated_total} updated "
            f"(date={date})"
        )

        # ── S3 upload ────────────────────────────────────────────
        if self._s3:
            self._s3.upload_file(self._get_db_path(date), self._s3_key(date))

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
        print(f"[Storage] Marked items as notified (date={date})")

        if self._s3:
            self._s3.upload_file(self._get_db_path(date), self._s3_key(date))

    # ── Cleanup ─────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Close all cached connections and remove tracked temp files."""
        for conn in self._connections.values():
            try:
                conn.close()
            except Exception:
                pass
        self._connections.clear()

        for tmp_file in self._temp_files:
            try:
                os.unlink(str(tmp_file))
            except OSError:
                pass
        self._temp_files.clear()

        print("[Storage] Cleanup complete")
