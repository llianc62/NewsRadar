# coding=utf-8
"""Tests for RssFetcher.fetch_feed — max_items / max_age_days filtering."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from news.fetcher.rss import ParsedRSSItem, RSSFeedConfig, RssFetcher


# ── Helpers ────────────────────────────────────────────────────────

def _make_item(title: str, published_at: str = "") -> ParsedRSSItem:
    return ParsedRSSItem(
        title=title,
        url=f"https://example.com/{title}",
        published_at=published_at,
        guid=f"guid-{title}",
    )


def _recent_iso(hours_ago: float = 0) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    ).isoformat()


def _days_ago_iso(days: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()


def _cfg(**overrides) -> dict:
    """Minimal config to construct RssFetcher."""
    base = {
        "crawler": {
            "rss": {
                "enabled": True,
                "sources": [
                    {
                        "id": "test-feed",
                        "name": "Test Feed",
                        "url": "https://example.com/rss",
                        "enabled": True,
                    }
                ],
            }
        }
    }
    # Merge overrides into the first source
    base["crawler"]["rss"]["sources"][0].update(overrides)
    return base


# ═══════════════════════════════════════════════════════════════════════
# max_age_days filtering
# ═══════════════════════════════════════════════════════════════════════

class TestMaxAgeDaysFiltering:
    """max_age_days filters out items older than N days."""

    def test_drops_items_older_than_max_age(self):
        """Items older than max_age_days should be dropped."""
        fetcher = RssFetcher(_cfg(max_age_days=1))

        now = _recent_iso(0)
        old = _days_ago_iso(3)  # 3 days old → should be dropped

        parsed = [
            _make_item("fresh", now),
            _make_item("stale", old),
        ]

        with patch.object(fetcher._parser, "parse", return_value=parsed), \
             patch("news.fetcher.rss.http_get_with_retry",
                   return_value=(MagicMock(text="<rss/>"), None)):
            entries, err = fetcher.fetch_feed(fetcher._feeds[0])

        assert err is None
        assert len(entries) == 1
        assert entries[0]["title"] == "fresh"

    def test_keeps_items_within_max_age(self):
        """Items younger than max_age_days should be kept."""
        fetcher = RssFetcher(_cfg(max_age_days=2))

        parsed = [
            _make_item("yesterday", _days_ago_iso(1)),
            _make_item("today", _recent_iso(0)),
        ]

        with patch.object(fetcher._parser, "parse", return_value=parsed), \
             patch("news.fetcher.rss.http_get_with_retry",
                   return_value=(MagicMock(text="<rss/>"), None)):
            entries, err = fetcher.fetch_feed(fetcher._feeds[0])

        assert err is None
        assert len(entries) == 2

    def test_keeps_items_without_published_at(self):
        """Items with empty published_at should be kept (can't determine age)."""
        fetcher = RssFetcher(_cfg(max_age_days=1))

        parsed = [
            _make_item("no_date", ""),
            _make_item("with_date", _recent_iso(0)),
        ]

        with patch.object(fetcher._parser, "parse", return_value=parsed), \
             patch("news.fetcher.rss.http_get_with_retry",
                   return_value=(MagicMock(text="<rss/>"), None)):
            entries, err = fetcher.fetch_feed(fetcher._feeds[0])

        assert err is None
        assert len(entries) == 2

    def test_max_age_days_none_disables_filtering(self):
        """max_age_days=None should skip filtering entirely."""
        fetcher = RssFetcher(_cfg())  # no max_age_days set → defaults to None

        parsed = [
            _make_item("very_old", _days_ago_iso(30)),
            _make_item("also_old", _days_ago_iso(20)),
        ]

        with patch.object(fetcher._parser, "parse", return_value=parsed), \
             patch("news.fetcher.rss.http_get_with_retry",
                   return_value=(MagicMock(text="<rss/>"), None)):
            entries, err = fetcher.fetch_feed(fetcher._feeds[0])

        assert err is None
        assert len(entries) == 2  # both kept

    def test_max_age_days_zero_disables_filtering(self):
        """max_age_days=0 should skip filtering."""
        fetcher = RssFetcher(_cfg(max_age_days=0))

        parsed = [
            _make_item("old", _days_ago_iso(30)),
        ]

        with patch.object(fetcher._parser, "parse", return_value=parsed), \
             patch("news.fetcher.rss.http_get_with_retry",
                   return_value=(MagicMock(text="<rss/>"), None)):
            entries, err = fetcher.fetch_feed(fetcher._feeds[0])

        assert err is None
        assert len(entries) == 1

    def test_unparseable_date_is_kept(self):
        """Items with unparseable published_at → keep (fail safe)."""
        fetcher = RssFetcher(_cfg(max_age_days=1))

        parsed = [
            _make_item("bad_date", "not a valid date"),
            _make_item("good_date", _recent_iso(0)),
        ]

        with patch.object(fetcher._parser, "parse", return_value=parsed), \
             patch("news.fetcher.rss.http_get_with_retry",
                   return_value=(MagicMock(text="<rss/>"), None)):
            entries, err = fetcher.fetch_feed(fetcher._feeds[0])

        assert err is None
        assert len(entries) == 2  # bad_date item kept (fail safe)


# ═══════════════════════════════════════════════════════════════════════
# max_items truncation
# ═══════════════════════════════════════════════════════════════════════

class TestMaxItemsTruncation:
    """max_items caps the number of entries."""

    def test_truncates_to_max_items(self):
        fetcher = RssFetcher(_cfg(max_items=3))

        parsed = [_make_item(f"item_{i}", _recent_iso(0)) for i in range(10)]

        with patch.object(fetcher._parser, "parse", return_value=parsed), \
             patch("news.fetcher.rss.http_get_with_retry",
                   return_value=(MagicMock(text="<rss/>"), None)):
            entries, err = fetcher.fetch_feed(fetcher._feeds[0])

        assert err is None
        assert len(entries) == 3

    def test_max_items_zero_no_truncation(self):
        """max_items=0 means no limit."""
        fetcher = RssFetcher(_cfg())  # defaults to 0

        parsed = [_make_item(f"item_{i}", _recent_iso(0)) for i in range(50)]

        with patch.object(fetcher._parser, "parse", return_value=parsed), \
             patch("news.fetcher.rss.http_get_with_retry",
                   return_value=(MagicMock(text="<rss/>"), None)):
            entries, err = fetcher.fetch_feed(fetcher._feeds[0])

        assert err is None
        assert len(entries) == 50


# ═══════════════════════════════════════════════════════════════════════
# Interaction: max_items + max_age_days
# ═══════════════════════════════════════════════════════════════════════

class TestMaxItemsAndMaxAgeDays:
    """max_items applied first, then max_age_days."""

    def test_max_items_then_max_age(self):
        """Order: truncate by max_items → filter by age."""
        fetcher = RssFetcher(_cfg(max_items=5, max_age_days=1))

        # 10 items, first 5 are old, last 5 are fresh
        parsed = [
            _make_item(f"old_{i}", _days_ago_iso(3)) for i in range(5)
        ] + [
            _make_item(f"fresh_{i}", _recent_iso(0)) for i in range(5)
        ]

        with patch.object(fetcher._parser, "parse", return_value=parsed), \
             patch("news.fetcher.rss.http_get_with_retry",
                   return_value=(MagicMock(text="<rss/>"), None)):
            entries, err = fetcher.fetch_feed(fetcher._feeds[0])

        assert err is None
        # After max_items=5 truncation: only first 5 (old) remain
        # After max_age_days=1 filtering: all 5 old items dropped → 0
        assert len(entries) == 0
