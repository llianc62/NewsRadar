# coding=utf-8
"""RSS/Atom/JSON Feed fetcher.

Provides:
- ``RSSFeedConfig`` — feed configuration dataclass
- ``ParsedRSSItem`` — parsed feed entry dataclass
- ``RSSParser`` — RSS 2.0 / Atom / JSON Feed 1.1 parser
- ``RssFetcher`` — :class:`Fetcher` subclass for RSS feeds
"""

import html
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import requests

from news.fetcher.fetcher import Fetcher
from news.parser.parser import HtmlParser
from utils import DEFAULT_TIMEZONE, http_get_with_retry


# ═══════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════


@dataclass
class RSSFeedConfig:
    """RSS feed configuration."""

    id: str  # Unique feed identifier
    name: str  # Human-readable display name
    url: str  # Feed URL
    max_items: int = 0  # Max entries to keep (0 = no limit)
    enabled: bool = True  # Whether this feed is active
    max_age_days: Optional[int] = None  # Max article age in days (None = use default, 0 = disable)


@dataclass
class ParsedRSSItem:
    """Parsed RSS entry from any supported feed format."""

    title: str
    url: str
    published_at: Optional[str] = None
    summary: Optional[str] = None
    author: Optional[str] = None
    guid: Optional[str] = None
    content: str = ""  # Inline HTML content from <content:encoded> / content_html


# ═══════════════════════════════════════════════════════════════════
# RSSParser — RSS 2.0 / Atom / JSON Feed 1.1
# ═══════════════════════════════════════════════════════════════════


class RSSParser:
    """Parse RSS 2.0 (via feedparser), Atom (via feedparser), and JSON Feed 1.1 (native).

    Handles title fallbacks, multi-format date parsing, summary cleaning
    (HTML strip + truncation), and author extraction from multiple fields.
    """

    def __init__(self, max_summary_length: int = 500):
        self.max_summary_length = max_summary_length

    # ── Public API ─────────────────────────────────────────────────

    def parse(self, content: str, feed_url: str = "") -> List[ParsedRSSItem]:
        """Parse feed content (XML or JSON), auto-detecting format.

        Args:
            content: Raw feed body (RSS/Atom XML or JSON Feed JSON).
            feed_url: Feed URL for error context.

        Returns:
            List of ParsedRSSItem objects (empty on parse failure).
        """
        if self._is_json_feed(content):
            return self._parse_json_feed(content, feed_url)

        return self._parse_xml_feed(content, feed_url)

    # ── Format detection ───────────────────────────────────────────

    def _is_json_feed(self, content: str) -> bool:
        """Detect JSON Feed format via version field."""
        content = content.strip()
        if not content.startswith("{"):
            return False
        try:
            data = json.loads(content)
            return "jsonfeed.org" in str(data.get("version", ""))
        except (json.JSONDecodeError, TypeError):
            return False

    # ── XML feed parsing (RSS 2.0 / Atom via feedparser) ───────────

    def _parse_xml_feed(
        self, content: str, feed_url: str
    ) -> List[ParsedRSSItem]:
        feed = feedparser.parse(content)

        if feed.bozo and not feed.entries:
            raise ValueError(
                f"RSS parse failed ({feed_url}): {feed.bozo_exception}"
            )

        items: List[ParsedRSSItem] = []
        for entry in feed.entries:
            item = self._parse_xml_entry(entry)
            if item:
                items.append(item)

        return items

    def _parse_xml_entry(self, entry: Any) -> Optional[ParsedRSSItem]:
        """Parse a single feedparser entry into ParsedRSSItem."""
        title = self._clean_text(entry.get("title", ""))

        # Extract URL from link / links
        url = entry.get("link", "")
        if not url:
            links = entry.get("links", [])
            for link in links:
                rel = link.get("rel", "")
                mime = link.get("type", "")
                if rel == "alternate" or mime.startswith("text/html"):
                    url = link.get("href", "")
                    break
            if not url and links:
                url = links[0].get("href", "")

        # Title fallback: use summary/description/content first snippet
        if not title:
            raw = (
                entry.get("summary")
                or entry.get("description", "")
            )
            if not raw:
                content_list = entry.get("content", [])
                if content_list and isinstance(content_list, list):
                    raw = content_list[0].get("value", "")
            if raw:
                title = self._clean_text(raw)
                if len(title) > 20:
                    title = title[:20] + "..."
            if not title and url:
                title = url

        # Extract inline content (RSS 2.0 <content:encoded>)
        content = ""
        content_list = entry.get("content", [])
        if content_list and isinstance(content_list, list):
            for c in content_list:
                val = (c.get("value") if isinstance(c, dict) else "")
                if val:
                    content = val
                    break

        if not title:
            return None

        return ParsedRSSItem(
            title=title,
            url=url,
            published_at=self._parse_date(entry),
            summary=self._parse_summary(entry),
            author=self._parse_author(entry),
            guid=entry.get("id")
            or (entry.get("guid", {}) if isinstance(entry.get("guid"), dict) else entry.get("guid"))
            or url,
            content=content,
        )

    # ── JSON Feed 1.1 parsing ──────────────────────────────────────

    def _parse_json_feed(
        self, content: str, feed_url: str
    ) -> List[ParsedRSSItem]:
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON Feed parse failed ({feed_url}): {e}")

        items_data = data.get("items", [])
        if not items_data:
            return []

        items: List[ParsedRSSItem] = []
        for item_data in items_data:
            item = self._parse_json_item(item_data)
            if item:
                items.append(item)

        return items

    def _parse_json_item(self, item_data: Dict[str, Any]) -> Optional[ParsedRSSItem]:
        """Parse a single JSON Feed item."""
        url = item_data.get("url", "") or item_data.get("external_url", "")

        title = item_data.get("title", "")
        if not title:
            content_text = item_data.get("content_text", "")
            if content_text:
                title = content_text[:20] + (
                    "..." if len(content_text) > 20 else ""
                )

        title = self._clean_text(title)
        if not title and url:
            title = url
        if not title:
            return None

        # Published date (ISO 8601)
        date_str = item_data.get("date_published") or item_data.get("date_modified")
        published_at = self._parse_iso_date(date_str) if date_str else None

        # Inline content (JSON Feed content_html / content_text)
        content_html = item_data.get("content_html", "")
        content_text = item_data.get("content_text", "")
        content = content_html or content_text

        # Summary: prefer summary, fall back to content_text
        summary = item_data.get("summary", "")
        if not summary:
            summary = content_text or self._clean_text(content_html)

        if summary:
            summary = self._clean_text(summary)
            if len(summary) > self.max_summary_length:
                summary = summary[:self.max_summary_length] + "..."

        # Author
        author = None
        authors = item_data.get("authors", [])
        if authors:
            names = [
                a.get("name", "")
                for a in authors
                if isinstance(a, dict) and a.get("name")
            ]
            if names:
                author = ", ".join(names)

        # GUID
        guid = item_data.get("id") or url

        return ParsedRSSItem(
            title=title,
            url=url,
            published_at=published_at,
            summary=summary or None,
            author=author,
            guid=guid,
            content=content,
        )

    # ── Text cleaning ──────────────────────────────────────────────

    def _clean_text(self, text: str) -> str:
        """Unescape HTML entities, strip tags, and collapse whitespace."""
        if not text:
            return ""

        text = html.unescape(text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    # ── Date parsing ───────────────────────────────────────────────

    def _parse_date(self, entry: Any) -> Optional[str]:
        """Parse published date from a feedparser entry."""
        # feedparser sets published_parsed/updated_parsed as time.struct_time
        date_struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if date_struct:
            try:
                dt = datetime(*date_struct[:6])
                return dt.isoformat()
            except (ValueError, TypeError):
                pass

        # Fall back to string-based parsing
        date_str = entry.get("published") or entry.get("updated")
        if date_str:
            # RFC 2822 (common in RSS)
            try:
                dt = parsedate_to_datetime(date_str)
                return dt.isoformat()
            except (ValueError, TypeError):
                pass
            # ISO 8601
            return self._parse_iso_date(date_str)

        return None

    def _parse_iso_date(self, date_str: str) -> Optional[str]:
        """Parse an ISO 8601 date string."""
        if not date_str:
            return None
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            return dt.isoformat()
        except (ValueError, TypeError):
            return None

    # ── Summary parsing ────────────────────────────────────────────

    def _parse_summary(self, entry: Any) -> Optional[str]:
        """Extract and clean summary from a feedparser entry."""
        summary = entry.get("summary") or entry.get("description", "")

        if not summary:
            content_list = entry.get("content", [])
            if content_list and isinstance(content_list, list):
                summary = content_list[0].get("value", "")

        if not summary:
            return None

        summary = self._clean_text(summary)
        if len(summary) > self.max_summary_length:
            summary = summary[:self.max_summary_length] + "..."

        return summary

    # ── Author parsing ─────────────────────────────────────────────

    def _parse_author(self, entry: Any) -> Optional[str]:
        """Extract author from a feedparser entry across common fields."""
        author = entry.get("author")
        if author:
            return self._clean_text(author)

        # Dublin Core creator
        author = entry.get("dc_creator")
        if author:
            return self._clean_text(author)

        # Atom-style authors list
        authors = entry.get("authors", [])
        if authors:
            names = [a.get("name", "") for a in authors if a.get("name")]
            if names:
                return ", ".join(names)

        return None


# ═══════════════════════════════════════════════════════════════════
# RssFetcher — Fetcher subclass
# ═══════════════════════════════════════════════════════════════════


class RssFetcher(Fetcher):
    """Fetch and parse multiple RSS/Atom/JSON Feed sources.

    Receives the full application config and extracts the
    ``crawler.rss`` section internally.

    Usage::

        fetcher = RssFetcher(config, timezone="Asia/Shanghai")
        results = fetcher.fetch()
    """

    def __init__(self, config: dict, timezone: str = DEFAULT_TIMEZONE):
        cfg = config.get("crawler", {}).get("rss", {})
        self._enabled = cfg.get("enabled", False)
        self._interval = cfg.get("interval", 1000)
        self._timeout = cfg.get("timeout", 30)
        self._timezone = timezone

        # Build feed config list
        self._feeds = []
        for feed_cfg in cfg.get("sources", []):
            feed = RSSFeedConfig(
                id=feed_cfg.get("id", ""),
                name=feed_cfg.get("name", ""),
                url=feed_cfg.get("url", ""),
                max_items=feed_cfg.get("max_items", 0),
                enabled=feed_cfg.get("enabled", True),
                max_age_days=feed_cfg.get("max_age_days"),
            )
            if feed.id and feed.url:
                self._feeds.append(feed)

        self._active_feeds = [f for f in self._feeds if f.enabled]
        self._parser = RSSParser()
        self._session = self._create_session()

    @property
    def enabled(self) -> bool:
        """Whether this fetcher has enabled feeds."""
        return self._enabled and len(self._active_feeds) > 0

    def _create_session(self) -> requests.Session:
        """Create a requests Session with default headers."""
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/91.0.4472.124 Safari/537.36"
                ),
                "Accept": (
                    "application/feed+json, application/json, "
                    "application/rss+xml, application/atom+xml, "
                    "application/xml, text/xml, */*"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        return session

    # ── Single feed ────────────────────────────────────────────────

    def fetch_feed(
        self, feed: RSSFeedConfig
    ) -> Tuple[List[Dict], Optional[str]]:
        """Fetch and parse a single RSS feed.

        Args:
            feed: RSSFeedConfig for the feed to fetch.

        Returns:
            (entries, error) tuple:
            - entries: list of standardised item dicts.
            - error: error description string, or None on success.
        """
        try:
            response, http_error = http_get_with_retry(
                self._session, feed.url, self._timeout, label=feed.name
            )
            if response is None:
                return [], http_error

            parsed_items = self._parser.parse(response.text, feed.url)

            # Apply max_items limit (0 = no limit)
            if feed.max_items > 0:
                parsed_items = parsed_items[: feed.max_items]

            entries = []
            for parsed in parsed_items:
                entries.append(
                    {
                        "title": parsed.title,
                        "source_id": feed.id,
                        "source_name": feed.name,
                        "source_type": "rss",
                        "url": parsed.url,
                        "mobile_url": "",
                        "rank": 0,
                        "guid": parsed.guid or "",
                        "published_at": parsed.published_at or "",
                        "summary": parsed.summary or "",
                        "author": parsed.author or "",
                        "content": parsed.content or "",
                        "category": "",
                        "tags": [],
                        "ranks": [],
                    }
                )

            # ── Inline HTML fallback (no-URL items only) ────────────
            # Items with a URL go through the normal crawl+parse
            # pipeline — the actual page is authoritative.  Only items
            # without a URL use inline HTML from the feed as a last
            # resort.
            inline_count = 0
            for entry in entries:
                if entry.get("url"):
                    entry["content"] = ""
                    continue
                raw_html = entry.get("content", "")
                if not raw_html:
                    continue
                result = HtmlParser().parse(raw_html, "")
                if result and result.get("markdown"):
                    entry["content"] = result["markdown"]
                    if not entry["author"]:
                        entry["author"] = result.get("author", "")
                    if not entry["published_at"]:
                        entry["published_at"] = result.get("published_at", "")
                    if not entry["summary"]:
                        entry["summary"] = result.get("summary", "")
                    entry["category"] = result.get("category", "")
                    entry["tags"] = result.get("tags", [])
                    inline_count += 1
                else:
                    entry["content"] = ""

            # ── Filter title-shaped tags ──────────────────────────────
            # Some sites stuff the full article title into
            # <meta name="keywords">; trafilatura picks it up as a tag.
            # Drop any tag that is a long substring of the title.
            for entry in entries:
                title = entry.get("title", "")
                tags = entry.get("tags", [])
                if title and tags:
                    entry["tags"] = [
                        t for t in tags
                        if not (len(t) > 6 and (t in title or title in t))
                    ]

            if inline_count:
                print(
                    f"[RSS] {feed.name}: parsed {inline_count} items "
                    f"from inline HTML (no-URL fallback)"
                )

            print(f"[RSS] {feed.name}: got {len(entries)} items")
            return entries, None

        except requests.Timeout:
            error = f"Request timeout ({self._timeout}s)"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except requests.RequestException as e:
            error = f"Request failed: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except ValueError as e:
            error = f"Parse failed: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

        except Exception as e:
            error = f"Unexpected error: {e}"
            print(f"[RSS] {feed.name}: {error}")
            return [], error

    # ── All feeds (the Fetcher interface) ──────────────────────────

    def fetch(self) -> List[Dict[str, Any]]:
        """Fetch all enabled feeds with rate limiting and jitter.

        Failed feeds are logged internally and excluded from results.

        Returns:
            Flat list of standardised item dicts.
        """
        if not self.enabled:
            return []

        all_items: List[Dict[str, Any]] = []
        failed_ids: List[str] = []

        print(f"[RSS] Fetching {len(self._active_feeds)} feeds...")

        for i, feed in enumerate(self._active_feeds):
            # Rate limiting with jitter (skip before the first request)
            if i > 0:
                interval_s = self._interval / 1000
                jitter = random.uniform(-0.2, 0.2) * interval_s
                time.sleep(interval_s + jitter)

            entries, error = self.fetch_feed(feed)

            if error:
                failed_ids.append(feed.id)
            else:
                all_items.extend(entries)

        print(
            f"[RSS] Done: {len(all_items)} items, "
            f"{len(failed_ids)} feeds failed"
        )

        return all_items
