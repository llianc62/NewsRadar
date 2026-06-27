# coding=utf-8
"""Tests for :mod:`storage.sqlite` — time-range filtering."""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from storage.sqlite import Sqlite


@pytest.fixture
def db(tmp_path):
    """Sqlite instance with an in-memory connection for a fixed date."""
    db = Sqlite(data_dir=str(tmp_path), timezone="Asia/Shanghai")

    # Replace _get_connection to return a cached in-memory DB
    _cache: dict[str, sqlite3.Connection] = {}

    def _make_conn(date: str):
        if date in _cache:
            return _cache[date]
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Create table without partial indexes (simpler for unit test)
        conn.execute("""
            CREATE TABLE news_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                tier INTEGER NOT NULL DEFAULT 4,
                priority INTEGER NOT NULL DEFAULT 0,
                url TEXT DEFAULT '',
                mobile_url TEXT DEFAULT '',
                rank INTEGER,
                heat_score INTEGER DEFAULT NULL,
                guid TEXT,
                published_at TEXT,
                summary TEXT,
                author TEXT,
                category TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.row_factory = sqlite3.Row
        _cache[date] = conn
        return conn

    db._get_connection = _make_conn
    return db


def _insert_row(conn, title, created_at, tier=3, priority=0, source_id="test", source_name="test", source_type="hotlist"):
    conn.execute(
        """INSERT INTO news_items (title, source_id, source_name, source_type, tier, priority, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (title, source_id, source_name, source_type, tier, priority, created_at),
    )
    conn.commit()


class TestGetAllTimeRange:
    """Time-range filtering via start_time / end_time."""

    def test_no_filter_returns_all(self, db):
        conn = db._get_connection("2026-06-27")
        _insert_row(conn, "A", "2026-06-27 08:00:00")
        _insert_row(conn, "B", "2026-06-27 09:00:00")
        _insert_row(conn, "C", "2026-06-27 10:00:00")

        rows = db.get_all("2026-06-27")
        assert len(rows) == 3

    def test_start_time_filters_after(self, db):
        conn = db._get_connection("2026-06-27")
        _insert_row(conn, "A", "2026-06-27 08:00:00")
        _insert_row(conn, "B", "2026-06-27 09:00:00")
        _insert_row(conn, "C", "2026-06-27 10:00:00")

        rows = db.get_all("2026-06-27", start_time="2026-06-27 08:30:00")
        titles = [r["title"] for r in rows]
        assert titles == ["B", "C"]

    def test_end_time_filters_before(self, db):
        conn = db._get_connection("2026-06-27")
        _insert_row(conn, "A", "2026-06-27 08:00:00")
        _insert_row(conn, "B", "2026-06-27 09:00:00")
        _insert_row(conn, "C", "2026-06-27 10:00:00")

        rows = db.get_all("2026-06-27", end_time="2026-06-27 09:30:00")
        titles = [r["title"] for r in rows]
        assert titles == ["A", "B"]

    def test_both_bounds(self, db):
        conn = db._get_connection("2026-06-27")
        _insert_row(conn, "A", "2026-06-27 08:00:00")
        _insert_row(conn, "B", "2026-06-27 09:00:00")
        _insert_row(conn, "C", "2026-06-27 10:00:00")
        _insert_row(conn, "D", "2026-06-27 11:00:00")

        rows = db.get_all("2026-06-27", start_time="2026-06-27 08:30:00", end_time="2026-06-27 10:30:00")
        titles = [r["title"] for r in rows]
        assert titles == ["B", "C"]

    def test_ordering_is_preserved(self, db):
        conn = db._get_connection("2026-06-27")
        _insert_row(conn, "Low", "2026-06-27 09:00:00", tier=4, priority=0)
        _insert_row(conn, "High", "2026-06-27 09:00:01", tier=1, priority=10)
        _insert_row(conn, "Mid", "2026-06-27 09:00:02", tier=2, priority=5)

        rows = db.get_all("2026-06-27", start_time="2026-06-27 08:00:00")
        tiers = [r["tier"] for r in rows]
        assert tiers == [1, 2, 4]  # tier ASC, priority DESC
