# coding=utf-8
"""Tests for cross-source URL deduplication in Crawler._dedup_items_by_url."""

from unittest.mock import MagicMock, patch

import pytest

from news.crawler import Crawler


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_config(sources=None, rss_feeds=None):
    """Build a minimal Crawler config for testing _dedup_items_by_url."""
    return {
        "app": {"timezone": "Asia/Shanghai"},
        "crawler": {
            "max_workers": 2,
            "timeout": 10,
            "max_retry": 1,
            "newsnow": {
                "enabled": True,
                "interval": 3600,
                "sources": sources or [],
            },
            "rss": {
                "enabled": True,
                "feeds": rss_feeds or [],
            },
        },
        "storage": {
            "local": {"data_dir": "/tmp/test_output"},
            "resource": {
                "endpoint_url": "http://localhost:9000",
                "bucket_name": "test-bucket",
                "access_key_id": "test-key",
                "secret_access_key": "test-secret",
            },
        },
    }


def _make_item(title, source_id, url, source_type="hotlist", **kwargs):
    """Build a standard item dict matching the fetcher output schema."""
    return {
        "title": title,
        "source_id": source_id,
        "source_name": kwargs.pop("source_name", source_id),
        "source_type": source_type,
        "url": url,
        "mobile_url": kwargs.pop("mobile_url", ""),
        "rank": kwargs.pop("rank", 0),
        "guid": kwargs.pop("guid", ""),
        "published_at": kwargs.pop("published_at", ""),
        "summary": kwargs.pop("summary", ""),
        "author": kwargs.pop("author", ""),
        "content": kwargs.pop("content", ""),
        "category": kwargs.pop("category", ""),
        "tags": kwargs.pop("tags", []),
        "ranks": kwargs.pop("ranks", []),
        "heat_score": kwargs.pop("heat_score", 0),
        "sentiment_score": kwargs.pop("sentiment_score", 0),
        **kwargs,
    }


def _make_sources(*ids_and_priorities):
    """Build a list of source configs from (id, priority) pairs."""
    return [
        {"id": sid, "name": sid, "priority": prio, "tier": 2}
        for sid, prio in ids_and_priorities
    ]


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def mock_s3():
    """Patch S3Storage so Crawler.__init__ doesn't make real HTTP calls."""
    with patch("news.crawler.S3Storage", autospec=True) as mock:
        yield mock


# ── Tests ────────────────────────────────────────────────────────────────

class TestDedupItemsByUrl:
    """Unit tests for Crawler._dedup_items_by_url."""

    def test_higher_priority_wins(self, mock_s3):
        """Same URL → higher-priority source replaces lower."""
        config = _make_config(
            sources=_make_sources(("src-a", 80), ("src-b", 85))
        )
        crawler = Crawler(config)

        items = [
            _make_item("Title", "src-a", "https://example.com/article"),
            _make_item("Title", "src-b", "https://example.com/article"),
        ]
        result = crawler._dedup_items_by_url(items)

        assert len(result) == 1
        assert result[0]["source_id"] == "src-b"

    def test_equal_priority_keep_first(self, mock_s3):
        """Same priority → keep the first encountered item."""
        config = _make_config(
            sources=_make_sources(("src-a", 80), ("src-b", 80))
        )
        crawler = Crawler(config)

        items = [
            _make_item("First", "src-a", "https://example.com/article"),
            _make_item("Second", "src-b", "https://example.com/article"),
        ]
        result = crawler._dedup_items_by_url(items)

        assert len(result) == 1
        assert result[0]["source_id"] == "src-a"
        assert result[0]["title"] == "First"

    def test_empty_url_pass_through(self, mock_s3):
        """Items with empty URL pass through without dedup."""
        config = _make_config(
            sources=_make_sources(("src-a", 80))
        )
        crawler = Crawler(config)

        items = [
            _make_item("A", "src-a", ""),
            _make_item("B", "src-a", ""),
            _make_item("C", "src-a", ""),
        ]
        result = crawler._dedup_items_by_url(items)

        assert len(result) == 3

    def test_mixed_empty_and_valid_urls(self, mock_s3):
        """Empty-URL items coexist with deduplicated valid-URL items."""
        config = _make_config(
            sources=_make_sources(("src-a", 80), ("src-b", 90))
        )
        crawler = Crawler(config)

        items = [
            _make_item("Keep", "src-a", "https://example.com/1"),
            _make_item("PassThrough", "src-a", ""),
            _make_item("Dup", "src-b", "https://example.com/1"),
        ]
        result = crawler._dedup_items_by_url(items)

        assert len(result) == 2
        # Empty-URL item passes through
        assert result[1]["source_id"] == "src-a"
        assert result[1]["url"] == ""
        # Valid URL dedup — higher priority wins
        valid_items = [r for r in result if r["url"]]
        assert len(valid_items) == 1
        assert valid_items[0]["source_id"] == "src-b"

    def test_no_duplicates(self, mock_s3):
        """All unique URLs → all items kept, order preserved."""
        config = _make_config(
            sources=_make_sources(("src-a", 80))
        )
        crawler = Crawler(config)

        items = [
            _make_item("A", "src-a", "https://example.com/1"),
            _make_item("B", "src-a", "https://example.com/2"),
            _make_item("C", "src-a", "https://example.com/3"),
        ]
        result = crawler._dedup_items_by_url(items)

        assert len(result) == 3
        assert [r["title"] for r in result] == ["A", "B", "C"]

    def test_multiple_duplicates_same_url(self, mock_s3):
        """3 items, same URL, different priorities → keep highest."""
        config = _make_config(
            sources=_make_sources(
                ("src-a", 80), ("src-b", 85), ("src-c", 82)
            )
        )
        crawler = Crawler(config)

        items = [
            _make_item("A", "src-a", "https://example.com/article"),
            _make_item("B", "src-b", "https://example.com/article"),
            _make_item("C", "src-c", "https://example.com/article"),
        ]
        result = crawler._dedup_items_by_url(items)

        assert len(result) == 1
        # src-b has the highest priority (85)
        assert result[0]["source_id"] == "src-b"
        assert result[0]["title"] == "B"

    def test_source_not_in_tiers(self, mock_s3):
        """Source not in tiers config → default priority 0."""
        config = _make_config(
            sources=_make_sources(("known-src", 50))
        )
        crawler = Crawler(config)

        items = [
            _make_item("Unknown", "unknown-src", "https://example.com/article"),
            _make_item("Known", "known-src", "https://example.com/article"),
        ]
        result = crawler._dedup_items_by_url(items)

        assert len(result) == 1
        assert result[0]["source_id"] == "known-src"

    def test_preserves_original_order(self, mock_s3):
        """Dedup keeps items in their first-encounter position."""
        config = _make_config(
            sources=_make_sources(("src-a", 80), ("src-b", 80), ("src-c", 80))
        )
        crawler = Crawler(config)

        items = [
            _make_item("A", "src-a", "https://example.com/1"),
            _make_item("B", "src-b", "https://example.com/2"),
            _make_item("C", "src-c", "https://example.com/3"),
            _make_item("A2", "src-a", "https://example.com/2"),   # dup of /2
            _make_item("B2", "src-b", "https://example.com/1"),   # dup of /1
        ]
        result = crawler._dedup_items_by_url(items)

        # A (first at /1) and B (first at /2) are kept; C stays; dup /1,/2 dropped
        assert len(result) == 3
        assert result[0]["url"] == "https://example.com/1"  # A
        assert result[1]["url"] == "https://example.com/2"  # B
        assert result[2]["url"] == "https://example.com/3"  # C

    def test_cross_type_dedup(self, mock_s3):
        """Hotlist + RSS with same URL → priority comparison works."""
        config = _make_config(
            sources=_make_sources(("hot-src", 85)),
            rss_feeds=[
                {"id": "rss-src", "name": "rss-src", "priority": 50,
                 "tier": 2, "url": "https://example.com/feed.xml", "enabled": True},
            ],
        )
        crawler = Crawler(config)

        items = [
            _make_item("RSS Item", "rss-src",
                       "https://example.com/article", source_type="rss"),
            _make_item("Hot Item", "hot-src",
                       "https://example.com/article", source_type="hotlist"),
        ]
        result = crawler._dedup_items_by_url(items)

        assert len(result) == 1
        assert result[0]["source_id"] == "hot-src"
        assert result[0]["source_type"] == "hotlist"

    def test_log_output(self, mock_s3, capsys):
        """Verifies the dedup summary log message."""
        config = _make_config(
            sources=_make_sources(("src-a", 80), ("src-b", 90))
        )
        crawler = Crawler(config)

        items = [
            _make_item("A", "src-a", "https://example.com/article"),
            _make_item("B", "src-b", "https://example.com/article"),
        ]
        result = crawler._dedup_items_by_url(items)

        assert len(result) == 1

        captured = capsys.readouterr()
        assert "Dedup: removed 1 duplicate URLs" in captured.out
        assert "kept 1 of 2 items" in captured.out

    def test_single_item(self, mock_s3):
        """Single item passes through unchanged."""
        config = _make_config(
            sources=_make_sources(("src-a", 80))
        )
        crawler = Crawler(config)

        items = [_make_item("Only", "src-a", "https://example.com/article")]
        result = crawler._dedup_items_by_url(items)

        assert len(result) == 1
        assert result[0]["title"] == "Only"

    def test_all_duplicates(self, mock_s3):
        """All items are duplicates of each other."""
        config = _make_config(
            sources=_make_sources(("low", 10), ("high", 90))
        )
        crawler = Crawler(config)

        items = [
            _make_item("Low", "low", "https://example.com/article"),
            _make_item("High", "high", "https://example.com/article"),
        ]
        result = crawler._dedup_items_by_url(items)

        assert len(result) == 1
        assert result[0]["source_id"] == "high"
