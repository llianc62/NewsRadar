# coding=utf-8
"""Cloud-to-local sync: download SQLite from S3, merge into PostgreSQL.

Uses the public ``S3Client`` (no more private-method calls on
``Storage._download_from_s3``).
"""

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from news.models import NewsData, NewsItem
from storage.s3 import S3Client
from storage.postgres import Database


def sync_from_cloud(
    db: Database,
    s3_config: Dict[str, Any],
    dates: Optional[List[str]] = None,
    data_dir: str = "output",
) -> Dict[str, Any]:
    """Download daily SQLite DBs from S3 and merge into PostgreSQL.

    For each date:
    1. Download news/{date}.db from S3 via S3Client
    2. Parse all rows from the SQLite file
    3. UPSERT into PostgreSQL with sync_status='cloud', skip_existing=True
    4. Clean up temp files

    Args:
        db: Connected Database instance.
        s3_config: S3 connection config dict.
        dates: List of YYYY-MM-DD date strings. Defaults to past 7 days.
        data_dir: Local directory for temp downloads.

    Returns:
        {"dates_processed": int, "total_new": int, "total_skipped": int, "errors": [str]}
    """
    s3 = S3Client.from_config(s3_config)
    if s3 is None:
        return {"dates_processed": 0, "total_new": 0, "total_skipped": 0, "errors": ["S3 not configured"]}

    # Default: sync the last 7 days
    if dates is None:
        from datetime import datetime, timedelta
        import pytz
        tz = pytz.timezone("Asia/Shanghai")
        today = datetime.now(tz).date()
        dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]

    total_new = 0
    total_skipped = 0
    errors: List[str] = []
    data_dir_path = Path(data_dir)

    for date_str in dates:
        print(f"\n[Sync] Processing {date_str}...")

        key = f"news/{date_str}.db"
        tmp_path = s3.download_to_temp(key)

        if tmp_path is None:
            print(f"[Sync] No S3 object for {date_str}, skipping")
            continue

        try:
            # Parse SQLite rows
            rows = _read_sqlite_db(tmp_path)
            print(f"[Sync] Read {len(rows)} rows from {date_str}.db")

            if not rows:
                continue

            # Convert to NewsData format and save (skip existing = local wins)
            news_data = _rows_to_newsdata(rows, date_str)
            result = db.save_news_data(news_data, sync_status="cloud", skip_existing=True)
            total_new += result.get("new", 0)
            total_skipped += result.get("skipped", 0)

        except Exception as e:
            msg = f"Failed to sync {date_str}: {e}"
            print(f"[Sync] {msg}")
            errors.append(msg)
        finally:
            # Clean up temp file
            try:
                os.unlink(str(tmp_path))
            except OSError:
                pass

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
    """Convert SQLite rows to a NewsData object."""
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
