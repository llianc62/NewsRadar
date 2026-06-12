# coding=utf-8
"""News crawler — fetch, content enrichment, and storage.

Provides the :class:`Crawler` class with two public entry points:

* ``fetch(url, output_style)`` — grab a single URL, parse content, save.
* ``fetch_all(with_content, output_style)`` — full pipeline from all configured
  sources (NewsNow API + RSS), optionally enriching with article body.

Both persist to the target backend automatically when *output_style*
is :attr:`OutputStyle.SQLITE` or :attr:`OutputStyle.POSTGRESQL`.
"""

from __future__ import annotations

import os
import re
import time

from enum import Enum
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from news.fetcher import NewsnowFetcher, RssFetcher
from news.parser import HtmlParser, ImageProcessor
from utils import format_date_folder, format_datetime_now, format_time_display, sanitize_filename

from news.models import NewsData, NewsItem

class OutputStyle(Enum):
    MARKDOWN = "markdown"
    HTML = "html"
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class Crawler:
    """News crawler — fetch, enrich, persist.

    Usage::

        crawler = Crawler(config)
        crawler.fetch("https://example.com", OutputStyle.MARKDOWN)
        news_data, tiers = crawler.fetch_all(
            with_content=True, output_style=OutputStyle.POSTGRESQL,
        )
    """

    def __init__(
        self,
        config: dict,
        parser: HtmlParser | None = None,
        pg_db: Any = None,
    ):
        self._config = config

        cfg = config.get("crawler", {})
        self.max_workers = cfg.get("max_workers", 5)
        self.timeout = cfg.get("timeout", 30)

        self.parser = parser or HtmlParser(config)

        # Unified file storage — local or S3, chosen by storage.backend.
        from storage import create_storage
        self.storage = create_storage(config)

        # Thread pool (lazy)
        self._executor: Optional[ThreadPoolExecutor] = None

        # Image processor (lazy — created on first use when with_image=True)
        self._image_processor: Optional[ImageProcessor] = None

        # DB connections (lazy or injected)
        self._pg_db = pg_db
        self._sqlite: Any = None

        # HTTP session (lazy)
        self._session: Optional[requests.Session] = None

    # ── HTTP session ─────────────────────────────────────────────────

    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko)"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
        return self._session

    # ── Lazy resources ───────────────────────────────────────────────

    def _get_pg_db(self):
        if self._pg_db is None:
            from storage.postgres import PostgreSQL
            self._pg_db = PostgreSQL(self._config["postgresql"])
            self._pg_db.connect()
            self._pg_db.init_schema()
        return self._pg_db

    def _get_sqlite_db(self):
        if self._sqlite is None:
            from storage.sqlite import Sqlite
            sc = self._config.get("storage", {})
            data_dir = sc.get("local", {}).get("data_dir", "output")
            self._sqlite = Sqlite(
                data_dir=data_dir,
                timezone=self._config.get("app", {}).get("timezone", "Asia/Shanghai"),
            )
        return self._sqlite

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self.max_workers)
        return self._executor

    def _get_image_processor(self) -> ImageProcessor:
        """Lazy-init the :class:`ImageProcessor` with the configured storage."""
        if self._image_processor is None:
            self._image_processor = ImageProcessor(
                storage=self.storage, max_workers=10,
            )
        return self._image_processor

    # ═══════════════════════════════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════════════════════════════

    def fetch(
        self,
        url: str,
        output_style: OutputStyle,
        with_content: bool = True,
        with_image: bool = False,
    ) -> None:
        """Grab a single URL — download, parse, optionally download images, save.

        Args:
            url: Web page URL to fetch.
            output_style: Persistence target.
            with_content: If False, save metadata only (no HTTP request).
            with_image: If True, download article images after parsing.
        """
        tiers = {"manual": {"tier": 4, "priority": 0}}
        item: Dict[str, Any] = {
            "title": "",
            "source_id": "manual",
            "source_name": "Manual Grab",
            "source_type": "manual",
            "url": url,
            "mobile_url": "",
            "rank": 0,
            "guid": "",
            "published_at": "",
            "summary": "",
            "author": "",
            "content": "",
            "category": "",
            "tags": [],
            "ranks": [],
        }

        # ── Download HTML ──────────────────────────────────────────
        try:
            resp = self.session().get(url, timeout=self.timeout)
            resp.raise_for_status()
            if resp.encoding == "ISO-8859-1" and resp.apparent_encoding == "utf-8":
                resp.encoding = resp.apparent_encoding
        except requests.RequestException as e:
            print(f"[Crawler] HTTP error for {url}: {e}")
            return

        # ── Parse to Markdown ──────────────────────────────────────
        parsed = self.parser.parse(resp.text, url)
        if not parsed:
            print(f"[Crawler] No content extracted: {url}")
            return

        item["title"] = parsed.get("title")
        item["author"] = parsed.get("author", "")
        item["published_at"] = parsed.get("published_at", "")
        item["summary"] = parsed.get("summary", "")
        item["category"] = parsed.get("category", "")
        item["tags"] = parsed.get("tags", [])

        if not with_content:
            self._persist(output_style, item, source_tiers=tiers)
            return

        # ── Persistence ────────────────────────────────────────────
        if with_content:
            # Phase 1: download HTML + parse Markdown
            self._run_batch_parse([item])

            # Phase 2: batch image download (if requested)
            if with_image:
                self._download_images_batch([item])

        self._persist(output_style, item, source_tiers=tiers)

    def fetch_all(
        self,
        output_style: OutputStyle,
        with_content: bool = False,
        with_image: bool = False,
    ) -> None:
        """Fetch from all configured sources (hot-list + RSS).

        When *output_style* is a DB backend the data is persisted
        automatically — callers do **not** need a separate save step.

        When *with_image* is True (only meaningful when *with_content*
        is also True), article images are downloaded in parallel after
        content parsing.
        """
        timezone = self._config["app"]["timezone"]
        date = format_date_folder(timezone)
        time_str = format_time_display(timezone)

        print(f"=== Crawler === {date} {time_str}")

        source_tiers = self.build_source_tiers()
        all_items: List[Dict[str, Any]] = []

        # ── Hot-list ───────────────────────────────────────────────
        newsnow_cfg = self._config["crawler"]["newsnow"]
        if newsnow_cfg["enabled"]:
            print(f"\n[Hot-list] Fetching {len(newsnow_cfg['sources'])} platforms...")
            fetcher = NewsnowFetcher(self._config)
            results = fetcher.fetch()
            if results:
                all_items.extend(results)

        # ── RSS ────────────────────────────────────────────────────
        rss_cfg = self._config["crawler"]["rss"]
        if rss_cfg["enabled"]:
            print("\n[RSS] Fetching feeds...")
            rss_fetcher = RssFetcher(self._config, timezone)
            rss_results = rss_fetcher.fetch()
            if rss_results:
                all_items.extend(rss_results)

        # ── Enrichment ─────────────────────────────────────────────
        if with_content:
            # Phase 1: download HTML + parse Markdown
            self._run_batch_parse(all_items)

            # Phase 2: batch image download (if requested)
            if with_image:
                self._download_images_batch(all_items)

        # ── Persistence ────────────────────────────────────────────
        self._persist(output_style, *all_items, source_tiers=source_tiers)

        print(f"=== Fetch complete: {len(all_items)} items ===")

    def build_source_tiers(self) -> dict:
        """Build ``{source_id: {tier, priority}}`` mapping from config."""
        tiers = {}
        for s in self._config.get("crawler", {}).get("newsnow", {}).get("sources", []):
            tiers[s["id"]] = {"tier": s.get("tier", 4), "priority": s.get("priority", 0)}
        for f in self._config.get("crawler", {}).get("rss", {}).get("feeds", []):
            if f.get("enabled", True):
                tiers[f["id"]] = {"tier": f.get("tier", 3), "priority": f.get("priority", 0)}
        return tiers

    # ═══════════════════════════════════════════════════════════════════
    # Internal — batch content fetch
    # ═══════════════════════════════════════════════════════════════════

    def _run_batch_parse(
        self,
        items: List[Dict[str, Any]],
    ) -> None:
        """Phase 1: download HTML + parse Markdown for all items via thread pool.

        Sets ``item["content"]`` (Markdown) and metadata fields
        (title, author, published_at, summary, category, tags).
        """
        valid = [it for it in items if it.get("url")]
        if not valid:
            return

        print(f"[Crawler] Phase 1 — downloading & parsing {len(valid)} items "
              f"(workers={self.max_workers})")

        executor = self._get_executor()
        futures = {
            executor.submit(self._download_and_parse, it): it
            for it in valid
        }

        success = 0
        for future in as_completed(futures):
            try:
                if future.result():
                    success += 1
            except Exception as e:
                item = futures[future]
                print(f"[Crawler] Worker error for {item.get('url', '?')}: {e}")

        print(f"[Crawler] Phase 1 done: {success}/{len(valid)} success")

    def _download_and_parse(self, item: Dict[str, Any]) -> bool:
        """Download HTML for a single item, parse to Markdown (no images).

        Sets ``item["content"]`` (Markdown), and metadata fields
        (title, author, published_at, summary, category, tags)
        extracted from the page.
        """
        url = item.get("url", "")
        if not url:
            return False

        try:
            resp = self.session().get(url, timeout=self.timeout)
            resp.raise_for_status()
            if resp.encoding == "ISO-8859-1" and resp.apparent_encoding == "utf-8":
                resp.encoding = resp.apparent_encoding
        except requests.RequestException as e:
            print(f"[Crawler] HTTP error for {url}: {e}")
            return False

        # Pure text parsing — no image processing
        parsed = self.parser.parse(resp.text, url)
        if parsed is None:
            print(f"[Crawler] No content extracted: {url}")
            return False

        item["content"] = parsed["markdown"]
        # Populate metadata from parsed result (don't overwrite existing values)
        item["title"] = parsed.get("title") or item.get("title", "")
        item["author"] = parsed.get("author", "")
        item["published_at"] = parsed.get("published_at", "")
        item["summary"] = parsed.get("summary", "")
        item["category"] = parsed.get("category", "")
        item["tags"] = parsed.get("tags", [])
        return True

    def _download_images_batch(
        self,
        items: List[Dict[str, Any]],
    ) -> None:
        """Phase 2: download article images in batch and replace URLs in-place.

        Collects all unique image URLs across items, downloads them via
        :class:`ImageProcessor`, then replaces each URL with its local path.
        Avoids re-extracting URLs per item during replacement — instead
        iterates over the download map directly.
        """
        # Collect unique image URLs across all items
        all_urls: Dict[str, str] = {}
        for it in items:
            if it.get("content"):
                for img_url in Crawler._extract_image_urls(it["content"]):
                    all_urls[img_url] = ""

        if not all_urls:
            print("[Crawler] Phase 2 — no images found, skipping")
            return

        print(f"[Crawler] Phase 2 — downloading {len(all_urls)} unique images")
        url_map = self._get_image_processor().download_images(all_urls)
        if not url_map:
            print("[Crawler] Phase 2 done (no images downloaded)")
            return

        # Replace URLs in-place: iterate url_map keys and do substring
        # replacement — avoids a second regex extraction per item
        replaced = 0
        for it in items:
            md = it.get("content", "")
            if not md:
                continue
            for old_url, new_path in url_map.items():
                if old_url in md:
                    md = md.replace(old_url, new_path)
                    replaced += 1
            it["content"] = md

        print(f"[Crawler] Phase 2 done: {replaced} replacements across "
              f"{sum(1 for it in items if it.get('content'))} articles")

    @staticmethod
    def _extract_image_urls(markdown: str) -> List[str]:
        """Extract image URLs from Markdown text.

        Matches both Markdown image syntax (``![alt](url)``) and inline
        HTML ``<img src="url">`` tags.
        """
        urls: List[str] = []
        # Markdown image: ![alt](url)
        urls.extend(re.findall(r'!\[.*?\]\((https?://[^\s)]+)\)', markdown))
        # HTML img: <img src="url">
        urls.extend(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', markdown, re.IGNORECASE))
        return urls

    # ═══════════════════════════════════════════════════════════════════
    # Internal — persistence (single entry point)
    # ═══════════════════════════════════════════════════════════════════

    def _persist(
        self,
        output_style: OutputStyle,
        *items: dict,
        source_tiers: dict | None = None,
    ) -> None:
        """Persist item dicts via the backend matching *output_style*."""
        if source_tiers is None:
            source_tiers = {}

        data = self._to_newsdata(list(items), source_tiers)

        match output_style:
            case OutputStyle.MARKDOWN:
                self._persist_md(data)
            case OutputStyle.HTML:
                self._persist_html(data)
            case OutputStyle.SQLITE:
                self._persist_sqlite(data, source_tiers=source_tiers)
            case OutputStyle.POSTGRESQL:
                self._persist_postgresql(data, source_tiers=source_tiers)

    def _to_newsdata(
        self,
        items: List[Dict[str, Any]],
        source_tiers: dict,
    ) -> NewsData:
        """Build a :class:`NewsData` from a list of item dicts."""
        tz = self._config.get("app", {}).get("timezone", "Asia/Shanghai")
        date = format_date_folder(tz)
        time_str = format_time_display(tz)

        by_source: Dict[str, List[NewsItem]] = {}
        for d in items:
            sid = d.get("source_id", "manual")
            ti = source_tiers.get(sid, {})
            by_source.setdefault(sid, []).append(NewsItem(
                title=d.get("title", ""),
                source_id=sid,
                source_name=d.get("source_name", "Manual Grab"),
                source_type=d.get("source_type", "hotlist"),
                tier=ti.get("tier", 4),
                priority=ti.get("priority", 0),
                url=d.get("url", ""),
                mobile_url=d.get("mobile_url", ""),
                rank=d.get("rank", 0),
                guid=d.get("guid", ""),
                published_at=d.get("published_at") or format_datetime_now(tz),
                summary=d.get("summary", ""),
                content=d.get("content", ""),
                author=d.get("author", ""),
                category=d.get("category", ""),
                tags=d.get("tags", []),
                first_crawl_time=d.get("first_crawl_time") or time_str,
                last_crawl_time=d.get("last_crawl_time") or time_str,
                crawl_count=d.get("crawl_count") or 1,
                ranks=d.get("ranks", []),
            ))

        return NewsData(date=date, items=by_source)

    # ── Backend-specific persist ─────────────────────────────────────

    def _persist_md(self, data: NewsData) -> None:
        """Write each article to a Markdown file via the storage layer.

        Filenames are derived from the article title via
        :func:`sanitize_filename` for readability.
        """
        seen: Dict[str, int] = {}
        for source_id, items in data.items.items():
            for item in items:
                safe_title = sanitize_filename(item.title) if item.title else "untitled"
                # Handle duplicate filenames by appending -2, -3, ...
                base = safe_title
                if safe_title in seen:
                    seen[safe_title] += 1
                    safe_title = f"{base}-{seen[safe_title]}"
                else:
                    seen[safe_title] = 1

                path = f"news/{data.date}/{safe_title}.md"
                saved = self.storage.save_file(
                    (item.content or "").encode("utf-8"),
                    path,
                    content_type="text/markdown",
                )
                print(f"[Crawler] Saved: {saved}")

    def _persist_html(self, data: NewsData) -> None:
        """Write each article to an HTML file via the storage layer."""
        seen: Dict[str, int] = {}
        for source_id, items in data.items.items():
            for item in items:
                safe_title = sanitize_filename(item.title) if item.title else "untitled"
                base = safe_title
                if safe_title in seen:
                    seen[safe_title] += 1
                    safe_title = f"{base}-{seen[safe_title]}"
                else:
                    seen[safe_title] = 1

                path = f"news/{data.date}/{safe_title}.html"
                saved = self.storage.save_file(
                    (item.content or "").encode("utf-8"),
                    path,
                    content_type="text/html",
                )
                print(f"[Crawler] Saved: {saved}")

    def _persist_sqlite(
        self, data: NewsData, source_tiers: dict
    ) -> None:
        """Save to SQLite database."""
        db = self._get_sqlite_db()
        db.save_news_data(data, source_tiers)
        db.cleanup()

    def _persist_postgresql(
        self, data: NewsData, source_tiers: dict
    ) -> None:
        """Save to PostgreSQL."""
        db = self._get_pg_db()
        result = db.save_news_data(data, source_tiers, sync_status="local")
        print(f"[Crawler] PG save result: {result}")

    # ═══════════════════════════════════════════════════════════════════
    # Cloud sync — download S3 SQLite DBs → merge into PostgreSQL
    # ═══════════════════════════════════════════════════════════════════

    def sync_from_cloud(
        self,
        dates: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Download daily SQLite DBs from S3 and merge into PostgreSQL.

        Args:
            dates: List of YYYY-MM-DD date strings. Defaults to past 7 days.

        Returns:
            {"dates_processed": int, "total_new": int, "total_skipped": int,
             "errors": [str]}
        """
        from storage.s3 import S3Client

        s3_config = self._config["storage"]["remote"]
        s3 = S3Client.from_config(s3_config)
        if s3 is None:
            return {
                "dates_processed": 0, "total_new": 0, "total_skipped": 0,
                "errors": ["S3 not configured"],
            }

        if dates is None:
            from datetime import datetime, timedelta
            import pytz
            tz = pytz.timezone("Asia/Shanghai")
            today = datetime.now(tz).date()
            dates = [
                (today - timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(1, 8)
            ]

        total_new = 0
        total_skipped = 0
        errors: List[str] = []

        for date_str in dates:
            print(f"\n[Sync] Processing {date_str}...")

            key = f"db/{date_str}.db"
            tmp = s3.download_to_temp(key)

            if tmp is None:
                print(f"[Sync] No S3 object for {date_str}, skipping")
                continue

            try:
                rows = Crawler._read_sqlite_db(tmp)
                print(f"[Sync] Read {len(rows)} rows from {date_str}.db")

                if not rows:
                    continue

                news_data = Crawler._rows_to_newsdata(rows, date_str)
                db = self._get_pg_db()
                result = db.save_news_data(
                    news_data, sync_status="cloud", skip_existing=True,
                )
                total_new += result.get("new", 0)
                total_skipped += result.get("skipped", 0)

            except Exception as e:
                msg = f"Failed to sync {date_str}: {e}"
                print(f"[Sync] {msg}")
                errors.append(msg)
            finally:
                try:
                    os.unlink(str(tmp))
                except OSError:
                    pass

        print(
            f"\n[Sync] Complete: {total_new} new, {total_skipped} skipped, "
            f"{len(errors)} errors"
        )
        return {
            "dates_processed": len(dates),
            "total_new": total_new,
            "total_skipped": total_skipped,
            "errors": errors,
        }

    @staticmethod
    def _read_sqlite_db(db_path) -> List[Dict[str, Any]]:
        """Read all rows from a SQLite news_items table."""
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute("SELECT * FROM news_items ORDER BY id")
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    @staticmethod
    def _rows_to_newsdata(
        rows: List[Dict[str, Any]], date_str: str
    ) -> NewsData:
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

        return NewsData(date=date_str, items=items)

    def close(self) -> None:
        """Close connections, thread pools, and resources."""
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
        if self._image_processor is not None:
            self._image_processor.close()
            self._image_processor = None
        if self._session is not None:
            self._session.close()
            self._session = None
        if self._pg_db is not None:
            self._pg_db.close()
            self._pg_db = None
        if self._sqlite is not None:
            self._sqlite.cleanup()
            self._sqlite = None
