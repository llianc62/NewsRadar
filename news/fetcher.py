# coding=utf-8
"""
Combined news fetcher module.

Provides:
- NewsFetcher: fetches hot-list data from the NewsNow API
- RSSFeedConfig: RSS feed configuration dataclass
- ParsedRSSItem: parsed RSS entry dataclass
- RSSParser: parses RSS 2.0, Atom, and JSON Feed 1.1
- RSSFetcher: orchestrates fetching and parsing multiple RSS feeds
"""

import re
import json
import time
import html
import random

from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import feedparser
import requests

from utils import DEFAULT_TIMEZONE, get_configured_time


# ═══════════════════════════════════════════════════════════════════
# NewsFetcher — NewsNow hot-list API
# ═══════════════════════════════════════════════════════════════════

# Hardcoded NewsNow API endpoint
NEWSNOW_API_URL = "https://newsnow.busiyi.world/api/s"

# Default request headers (Chrome UA, Accept JSON, zh-CN)
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
}


class NewsFetcher:
    """Fetch hot-list data from the NewsNow API.

    No proxy support — requests go directly to the API endpoint.
    """

    def __init__(self):
        pass

    def fetch_data(
        self,
        id_info: Union[str, Tuple[str, str]],
        max_retries: int = 2,
    ) -> Tuple[Optional[str], str, str]:
        """Fetch data for a single platform with exponential backoff.

        Args:
            id_info: Platform ID string, or (platform_id, alias) tuple.
            max_retries: Maximum retry attempts (default 2 = up to 3 total).

        Returns:
            (response_text, platform_id, alias) tuple.
            response_text is None when all attempts fail.
        """
        if isinstance(id_info, tuple):
            id_value, alias = id_info
        else:
            id_value = id_info
            alias = id_value

        url = f"{NEWSNOW_API_URL}?id={id_value}&latest"

        for attempt in range(max_retries + 1):
            try:
                response = requests.get(
                    url,
                    headers=_DEFAULT_HEADERS,
                    timeout=10,
                )
                response.raise_for_status()

                data_json = json.loads(response.text)
                status = data_json.get("status", "unknown")

                if status not in ("success", "cache"):
                    raise ValueError(f"Unexpected response status: {status}")

                status_label = "fresh" if status == "success" else "cached"
                print(f"Fetched {id_value} successfully ({status_label})")
                return response.text, id_value, alias

            except Exception as e:
                if attempt < max_retries:
                    # Exponential backoff: base 3-5s, double each retry
                    base = random.uniform(3, 5)
                    wait = base * (2 ** attempt)
                    print(
                        f"Request for {id_value} failed: {e}. "
                        f"Retrying in {wait:.2f}s (attempt {attempt + 1}/{max_retries})..."
                    )
                    time.sleep(wait)
                else:
                    print(f"Request for {id_value} failed after {max_retries} retries: {e}")
                    return None, id_value, alias

        return None, id_value, alias

    def crawl_websites(
        self,
        ids_list: List[Union[str, Tuple[str, str]]],
        request_interval: int = 2000,
    ) -> Tuple[Dict, Dict, List]:
        """Batch-crawl multiple platforms with rate limiting and jitter.

        Args:
            ids_list: List of platform IDs (str) or (id, alias) tuples.
            request_interval: Minimum interval between requests in milliseconds.

        Returns:
            (results, id_to_name, failed_ids) tuple:
            - results: {source_id: {title: {"ranks": [1,2], "url": "...", "mobileUrl": "..."}}}
            - id_to_name: {source_id: display_name}
            - failed_ids: list of source_ids that failed
        """
        results: Dict[str, Dict] = {}
        id_to_name: Dict[str, str] = {}
        failed_ids: List[str] = []

        for i, id_info in enumerate(ids_list):
            if isinstance(id_info, tuple):
                id_value, name = id_info
            else:
                id_value = id_info
                name = id_value

            id_to_name[id_value] = name
            response, _, _ = self.fetch_data(id_info)

            if response:
                try:
                    data = json.loads(response)
                    results[id_value] = {}

                    for index, item in enumerate(data.get("items", []), 1):
                        title = item.get("title")
                        # Skip invalid titles (None, float, empty)
                        if (
                            title is None
                            or isinstance(title, float)
                            or not str(title).strip()
                        ):
                            continue
                        title = str(title).strip()
                        url = item.get("url", "")
                        mobile_url = item.get("mobileUrl", "")

                        if title in results[id_value]:
                            results[id_value][title]["ranks"].append(index)
                        else:
                            results[id_value][title] = {
                                "ranks": [index],
                                "url": url,
                                "mobileUrl": mobile_url,
                            }
                except json.JSONDecodeError:
                    print(f"Failed to parse response for {id_value}")
                    failed_ids.append(id_value)
                except Exception as e:
                    print(f"Error processing data for {id_value}: {e}")
                    failed_ids.append(id_value)
            else:
                failed_ids.append(id_value)

            # Rate limiting with jitter (skip after the last request)
            if i < len(ids_list) - 1:
                jitter = random.uniform(-0.15, 0.15) * request_interval
                actual_interval = max(50, request_interval + jitter)
                time.sleep(actual_interval / 1000)

        print(f"Success: {list(results.keys())}, Failed: {failed_ids}")
        return results, id_to_name, failed_ids


# ═══════════════════════════════════════════════════════════════════
# RSS data types
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

        # Summary: prefer summary, fall back to content_text
        summary = item_data.get("summary", "")
        if not summary:
            content_text = item_data.get("content_text", "")
            content_html = item_data.get("content_html", "")
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
# RSSFetcher — orchestrate fetching multiple RSS feeds
# ═══════════════════════════════════════════════════════════════════


class RSSFetcher:
    """Fetch and parse multiple RSS feeds with rate limiting.

    No proxy support. Returns plain dicts (not domain objects) for
    downstream conversion via convert_rss_items_to_news_data().
    """

    def __init__(
        self,
        feeds: List[RSSFeedConfig],
        request_interval: int = 2000,
        timeout: int = 15,
        timezone: str = DEFAULT_TIMEZONE,
    ):
        """Initialise the fetcher.

        Args:
            feeds: List of RSSFeedConfig objects (disabled feeds are skipped).
            request_interval: Minimum interval between requests in milliseconds.
            timeout: HTTP request timeout in seconds.
            timezone: Timezone string for crawl timestamps.
        """
        self.feeds = [f for f in feeds if f.enabled]
        self.request_interval = request_interval
        self.timeout = timeout
        self.timezone = timezone

        self.parser = RSSParser()
        self.session = self._create_session()

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
            - entries: list of dicts with keys title, url, guid, published_at,
              summary, author, feed_id, feed_name, crawl_time, crawl_date.
            - error: error description string, or None on success.
        """
        now = get_configured_time(self.timezone)
        crawl_time = now.strftime("%H:%M")
        crawl_date = now.strftime("%Y-%m-%d")

        try:
            response = self.session.get(feed.url, timeout=self.timeout)
            response.raise_for_status()

            parsed_items = self.parser.parse(response.text, feed.url)

            # Apply max_items limit (0 = no limit)
            if feed.max_items > 0:
                parsed_items = parsed_items[: feed.max_items]

            entries = []
            for parsed in parsed_items:
                entries.append(
                    {
                        "title": parsed.title,
                        "url": parsed.url,
                        "guid": parsed.guid or "",
                        "published_at": parsed.published_at or "",
                        "summary": parsed.summary or "",
                        "author": parsed.author or "",
                        "feed_id": feed.id,
                        "feed_name": feed.name,
                        "crawl_time": crawl_time,
                        "crawl_date": crawl_date,
                    }
                )

            print(f"[RSS] {feed.name}: got {len(entries)} items")
            return entries, None

        except requests.Timeout:
            error = f"Request timeout ({self.timeout}s)"
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

    # ── All feeds ──────────────────────────────────────────────────

    def fetch_all(
        self,
    ) -> Tuple[Dict[str, List], Dict[str, str], List[str]]:
        """Fetch all enabled feeds with rate limiting and jitter.

        Returns:
            (all_items, id_to_name, failed_ids) tuple:
            - all_items: {feed_id: [entry_dict, ...]}
            - id_to_name: {feed_id: display_name}
            - failed_ids: list of feed_ids that failed
        """
        all_items: Dict[str, List] = {}
        id_to_name: Dict[str, str] = {}
        failed_ids: List[str] = []

        print(f"[RSS] Fetching {len(self.feeds)} feeds...")

        for i, feed in enumerate(self.feeds):
            # Rate limiting with jitter (skip before the first request)
            if i > 0:
                interval_s = self.request_interval / 1000
                jitter = random.uniform(-0.2, 0.2) * interval_s
                time.sleep(interval_s + jitter)

            entries, error = self.fetch_feed(feed)

            id_to_name[feed.id] = feed.name

            if error:
                failed_ids.append(feed.id)
            else:
                all_items[feed.id] = entries

        total = sum(len(v) for v in all_items.values())
        print(
            f"[RSS] Done: {len(all_items)} feeds ok, "
            f"{len(failed_ids)} failed, {total} items total"
        )

        return all_items, id_to_name, failed_ids

    # ── Factory ────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: Dict) -> "RSSFetcher":
        """Create an RSSFetcher from a configuration dictionary.

        Args:
            config: Dict with keys:
                - feeds (list): List of feed config dicts, each with
                  id, name, url, and optional max_items, enabled, max_age_days.
                - request_interval (int, optional): default 2000.
                - timeout (int, optional): default 15.
                - timezone (str, optional): default DEFAULT_TIMEZONE.

        Returns:
            Configured RSSFetcher instance.
        """
        feeds = []
        for feed_cfg in config.get("feeds", []):
            feed = RSSFeedConfig(
                id=feed_cfg.get("id", ""),
                name=feed_cfg.get("name", ""),
                url=feed_cfg.get("url", ""),
                max_items=feed_cfg.get("max_items", 0),
                enabled=feed_cfg.get("enabled", True),
                max_age_days=feed_cfg.get("max_age_days"),
            )
            if feed.id and feed.url:
                feeds.append(feed)

        return cls(
            feeds=feeds,
            request_interval=config.get("request_interval", 2000),
            timeout=config.get("timeout", 15),
            timezone=config.get("timezone", DEFAULT_TIMEZONE),
        )


# ═══════════════════════════════════════════════════════════════════
# ArticleParser — article content extraction with MinIO image storage
# ═══════════════════════════════════════════════════════════════════


class ArticleParser:
    """Fetch article content, convert to Markdown, save images to MinIO.

    Queries PostgreSQL for articles where ``content IS NULL``,
    downloads the article HTML, extracts the main body with
    ``trafilatura``, uploads images to MinIO, and updates the
    ``content`` column.

    Reference: https://github.com/microsoft/markitdown
    """

    def __init__(
        self,
        db,  # storage.postgres.Database
        image_storage=None,  # storage.minio.ImageStorage (optional)
        config: Optional[Dict[str, Any]] = None,
    ):
        self.db = db
        self.image_storage = image_storage
        cfg = config or {}
        self.timeout = cfg.get("timeout", 30)
        self.max_content_length = cfg.get("max_content_length", 100000)
        self.request_interval = cfg.get("request_interval", 2000)

        # Try to import trafilatura (optional dependency)
        self._has_trafilatura = False
        try:
            import trafilatura  # noqa: F401
            self._has_trafilatura = True
        except ImportError:
            pass

        self._session = self._create_session()

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        return session

    # ── Batch processing ──────────────────────────────────────────

    def process_pending(self, limit: int = 10) -> int:
        """Fetch content for articles that don't have it yet.

        Returns the number of articles successfully processed.
        """
        articles = self.db.get_articles_without_content(limit=limit)
        if not articles:
            print("[ArticleParser] No articles without content")
            return 0

        print(f"[ArticleParser] Processing {len(articles)} articles...")
        success = 0

        for i, article in enumerate(articles):
            if i > 0:
                time.sleep(self.request_interval / 1000)

            try:
                content = self.parse_article(article["url"], article["id"])
                if content:
                    self.db.update_article_content(article["id"], content)
                    success += 1
                    print(
                        f"[ArticleParser] OK [{article['id']}]: "
                        f"{article['title'][:40]}... ({len(content)} chars)"
                    )
                else:
                    print(
                        f"[ArticleParser] No content extracted: "
                        f"{article['title'][:40]}..."
                    )
            except Exception as e:
                print(f"[ArticleParser] Failed [{article['id']}]: {e}")

        print(f"[ArticleParser] Done: {success}/{len(articles)} succeeded")
        return success

    def process_article(self, article_id: int) -> Optional[str]:
        """Fetch content for a single article by ID.

        Returns the Markdown content string, or None on failure.
        """
        article = self.db.get_news_by_id(article_id)
        if not article:
            print(f"[ArticleParser] Article {article_id} not found")
            return None

        url = article.get("url", "")
        if not url:
            print(f"[ArticleParser] Article {article_id} has no URL")
            return None

        content = self.parse_article(url, article_id)
        if content:
            self.db.update_article_content(article_id, content)
        return content

    # ── Core: download → extract → images → Markdown ──────────────

    def parse_article(self, url: str, article_id: int = 0) -> Optional[str]:
        """Download article HTML, extract content as Markdown, handle images.

        Args:
            url: Article URL.
            article_id: Article DB ID (for image association).

        Returns:
            Markdown content string, or None if extraction failed.
        """
        try:
            response = self._session.get(url, timeout=self.timeout)
            response.raise_for_status()
            html_text = response.text
        except requests.RequestException as e:
            print(f"[ArticleParser] HTTP error for {url}: {e}")
            return None

        if self._has_trafilatura:
            markdown = self._extract_content(html_text, url)
        else:
            markdown = self._extract_fallback(html_text)

        if not markdown:
            return None

        # Handle images: download → MinIO → replace URLs → save to DB
        if self.image_storage is not None:
            markdown = self._handle_images(html_text, markdown, article_id)

        return markdown

    def _extract_content(self, html_text: str, url: str) -> Optional[str]:
        """Use trafilatura for content extraction, fall back to HTML-strip."""
        markdown = None

        if self._has_trafilatura:
            import trafilatura
            result = trafilatura.extract(
                html_text,
                url=url,
                output_format="markdown",
                with_metadata=True,
                include_tables=True,
                include_images=True,
                include_links=True,
                include_formatting=True,
            )
            if result and len(result.strip()) > 50:
                markdown = result.strip()

        # Fallback when trafilatura is unavailable or fails to extract
        if markdown is None:
            markdown = self._extract_fallback(html_text)

        if markdown and len(markdown) > self.max_content_length:
            markdown = markdown[:self.max_content_length] + "\n\n... (truncated)"

        return markdown

    def _handle_images(
        self, html_text: str, markdown: str, article_id: int
    ) -> str:
        """Download images from HTML, upload to MinIO, replace URLs in Markdown.

        Returns updated Markdown with MinIO image URLs.
        """
        from urllib.parse import urljoin, urlparse

        # Parse <img> tags from HTML
        img_pattern = re.compile(
            r'<img[^>]+src=["\']([^"\']+)["\']'
            r'(?:[^>]+alt=["\']([^"\']*)["\'])?'
            r'(?:[^>]+width=["\'](\d+)["\'])?'
            r'(?:[^>]+height=["\'](\d+)["\'])?',
            re.IGNORECASE,
        )

        img_index = 0
        for match in img_pattern.finditer(html_text):
            src = match.group(1)
            alt = match.group(2) or ""
            width = int(match.group(3)) if match.group(3) else None
            height = int(match.group(4)) if match.group(4) else None

            # Resolve relative URLs
            img_url = src
            if not urlparse(src).netloc:
                # We don't have the base URL context here, skip relative
                continue

            try:
                # Download image
                img_response = self._session.get(img_url, timeout=15)
                img_response.raise_for_status()

                # Determine content type and extension
                content_type = img_response.headers.get(
                    "Content-Type", "image/jpeg"
                ).split(";")[0].strip()
                ext_map = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/gif": ".gif",
                    "image/webp": ".webp",
                    "image/svg+xml": ".svg",
                }
                ext = ext_map.get(content_type, ".jpg")

                # Upload to MinIO
                from datetime import datetime
                from pathlib import Path
                import tempfile

                date_prefix = datetime.now().strftime("%Y-%m")
                object_key = f"{date_prefix}/{article_id}/img_{img_index:02d}{ext}"

                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                    tmp.write(img_response.content)
                    tmp_path = Path(tmp.name)

                try:
                    minio_url = self.image_storage.upload_image(
                        tmp_path, object_key, content_type
                    )
                    file_size = len(img_response.content)

                    # Save to news_images table
                    self.db.save_article_image(
                        article_id=article_id,
                        image_url=minio_url,
                        original_url=img_url,
                        width=width,
                        height=height,
                        file_size=file_size,
                        sort_order=img_index,
                    )

                    # Replace URL in Markdown
                    markdown = markdown.replace(img_url, minio_url)

                finally:
                    tmp_path.unlink(missing_ok=True)

                img_index += 1

            except Exception as e:
                print(f"[ArticleParser] Image download failed [{img_url}]: {e}")

        return markdown

    def _extract_fallback(self, html_text: str) -> Optional[str]:
        """Fallback: strip HTML tags, collapse whitespace."""
        text = re.sub(
            r'<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>',
            '',
            html_text,
            flags=re.DOTALL | re.IGNORECASE,
        )

        text = re.sub(r'<[^>]+>', ' ', text)
        text = html.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()

        paragraphs = [p.strip() for p in text.split('\n') if len(p.strip()) > 80]
        if paragraphs:
            text = '\n\n'.join(paragraphs)

        if len(text) > 100:
            if len(text) > self.max_content_length:
                text = text[:self.max_content_length] + "\n\n... (truncated)"
            return text

        return None
