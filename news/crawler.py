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

import json
import os
import re
import time
import requests

from enum import Enum
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from news.fetcher import NewsnowFetcher, RssFetcher
from news.parser import HtmlParser
from news.images import ImageProcessor
from news.models import NewsData, NewsItem
from storage.files import LocalStorage, S3Storage
from utils import format_date_folder, format_datetime_now, format_time_display, sanitize_filename

class OutputStyle(Enum):
    MARKDOWN = "markdown"
    HTML = "html"
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class StorageTarget(Enum):
    """Public enum for selecting the image storage backend.

    Used by :meth:`Crawler.fetch` so callers never access private members.
    """

    LOCAL = "local"
    RESOURCE = "resource"

MAX_IMAGE_PROCESSOR_WORKERS = 10  # Limit for concurrent image downloads


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
        self.max_workers = cfg.get("max_workers", 8)
        self.timeout = cfg.get("timeout", 30)

        self.parser = parser or HtmlParser(config)

        # Source tiers — built once from config, rarely changes
        self._source_tiers = self._build_source_tiers()

        # File storage — always local (markdown/html file output)
        storage_conf = config.get("storage", {})
        data_dir = storage_conf.get("local", {}).get("data_dir", "output")
        self._local_storage = LocalStorage(data_dir)

        # Resource storage — local MinIO/S3 for project files/images (required)
        resource_cfg = storage_conf.get("resource", {})
        self._resource_storage = S3Storage(resource_cfg)

        # Thread pool (lazy)
        self._executor: Optional[ThreadPoolExecutor] = None

        # DB connections (lazy or injected)
        self._pg_db = pg_db
        self._sqlite: Any = None

        # HTTP session (lazy)
        self._session: Optional[requests.Session] = None

        # Image processor (lazy) — shared across fetch calls
        self._image_processor: Optional[ImageProcessor] = None

    # ── HTTP session ─────────────────────────────────────────────────

    @staticmethod
    def _hook_response_encoding(response, *args, **kwargs):
        """Response hook: correct encoding when the server omits charset.

        RFC 2616 §3.7.1 defaults to ISO-8859-1 when no charset is
        specified, but many sites serve UTF-8 content.  chardet (via
        ``apparent_encoding``) detects the real encoding and we apply it
        before ``resp.text`` is ever accessed.
        """
        if response.encoding == "ISO-8859-1" and response.apparent_encoding == "utf-8":
            response.encoding = response.apparent_encoding
        return response

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
            self._session.hooks["response"].append(self._hook_response_encoding)
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
            storage_conf = self._config.get("storage", {})
            data_dir = storage_conf.get("local", {}).get("data_dir", "output")
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
        if self._image_processor is None:
            self._image_processor = ImageProcessor(
                max_workers=MAX_IMAGE_PROCESSOR_WORKERS
                )
        return self._image_processor

    def _build_source_tiers(self) -> dict:
        """Build ``{source_id: {tier, priority}}`` mapping from config."""
        tiers = {}
        for s in self._config.get("crawler", {}).get("newsnow", {}).get("sources", []):
            tiers[s["id"]] = {"tier": s.get("tier", 4), "priority": s.get("priority", 0)}
        for rss in self._config.get("crawler", {}).get("rss", {}).get("feeds", []):
            if rss.get("enabled", True):
                tiers[rss["id"]] = {"tier": rss.get("tier", 3), "priority": rss.get("priority", 0)}
        tiers["manual"] = {"tier": 4, "priority": 0}
        return tiers

    # ═══════════════════════════════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════════════════════════════

    def fetch(
        self,
        url: str,
        output_style: OutputStyle,
        with_content: bool = True,
        with_image: bool = False,
        target_storage: StorageTarget | None = None,
    ) -> None:
        """Grab a single URL — download, parse, optionally download images, save.

        Args:
            url: Web page URL to fetch.
            output_style: Persistence target.
            with_content: If False, save metadata only (no HTTP request).
            with_image: If True, download article images after parsing.
            target_storage: Which storage backend to use for images.
                :attr:`StorageTarget.LOCAL` — local filesystem.
                :attr:`StorageTarget.RESOURCE` — S3/MinIO.
                ``None`` (default) — resource if available, else local.
        """
        item: Dict[str, Any] = {
            "title": "",
            "source_id": "manual",
            "source_type": "manual",
            "source_name": "人工添加",
            "url": url,
            "rank": 0,
            "content": "",
        }

        # ── Download HTML ──────────────────────────────────────────
        try:
            resp = self.session().get(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise requests.RequestException(
                f"HTTP 请求失败: {e}"
            ) from e

        # ── Parse to Markdown ──────────────────────────────────────
        parsed = self.parser.parse(resp.text, url)
        if not parsed:
            raise Exception(f"无法提取页面正文内容: {url}")

        item["title"] = parsed.get("title")
        item["author"] = parsed.get("author", "")
        item["published_at"] = parsed.get("published_at", "")
        item["summary"] = parsed.get("summary", "")
        item["category"] = parsed.get("category", "")
        item["tags"] = parsed.get("tags", [])

        if not with_content:
            self.persist(item, output_style=output_style)
            return

        # ── Persistence ────────────────────────────────────────────
        if with_content:
            # Phase 1: download HTML + parse Markdown
            self._run_batch_parse([item])

            # Phase 2: batch image download (if requested)
            if with_image:
                storage = self._resource_storage
                if target_storage:
                    storage = target_storage
                self._run_batch_image_download([item], storage)

        self.persist(item, output_style=output_style)

    def fetch_all(
        self,
        output_style: OutputStyle,
        with_content: bool = False,
        with_image: bool = False,
    ) -> dict:
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
            self.enrich_content(*all_items, with_image=with_image)

        # ── Persistence ────────────────────────────────────────────
        self.persist(*all_items, output_style=output_style)

        print(f"=== Fetch complete: {len(all_items)} items ===")
        return {
            "total": len(all_items),
            "hotlist": len([i for i in all_items if i.get("source_type") == "hotlist"]),
            "rss": len([i for i in all_items if i.get("source_type") == "rss"]),
        }

    # ═══════════════════════════════════════════════════════════════════
    # Internal — content enrichment (shared by fetch_all + cloud sync)
    # ═══════════════════════════════════════════════════════════════════

    def enrich_content(
        self,
        *items: Dict[str, Any],
        with_image: bool = False,
    ) -> None:
        """Enrich items with parsed Markdown content and optionally images.

        Phase 1: download HTML + parse to Markdown via thread pool.
        Phase 2 (optional): download article images and replace URLs in-place.

        Each item dict is mutated in-place — ``content``, ``title``,
        ``author``, ``published_at``, ``summary``, ``category``, ``tags``
        are set or updated.  Items without a URL are silently skipped.
        """
        batch_list = list(items)
        self._run_batch_parse(batch_list)
        if with_image:
            self._run_batch_image_download(
                batch_list,
                image_storage=self._resource_storage
            )

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
        except requests.RequestException as e:
            print(f"[Crawler] HTTP error for {url}: {e}")
            return False

        # Pure text parsing — no image processing
        parsed = self.parser.parse(resp.text, url)
        if parsed is None:
            print(f"[Crawler] No content extracted: {url}")
            return False

        item["content"] = parsed["markdown"]
        item["author"] = parsed.get("author", "")
        item["published_at"] = parsed.get("published_at", "")
        item["summary"] = parsed.get("summary", "")
        item["category"] = parsed.get("category", "")
        item["tags"] = parsed.get("tags", [])
        if not item["tags"] and item.get("content"):
            item["tags"] = _extract_keywords_textrank(item["content"])
        return True

    def _run_batch_image_download(
        self,
        items: List[Dict[str, Any]],
        image_storage=None,
    ) -> None:
        """Phase 2: download article images in batch and replace URLs in-place.

        Collects all unique image URLs across items, downloads them via
        :class:`ImageProcessor`, then replaces each URL with its access URL.
        Avoids re-extracting URLs per item during replacement — instead
        iterates over the download map directly.

        Args:
            image_storage: :class:`FileStorage` for images.  When None,
                defaults to ``self._image_storage``.  When both are None,
                image download is skipped.
        """
        if image_storage is None:
            print("[Crawler] Phase 2 — S3 not configured, skipping image download")
            return

        # Collect unique image URLs across all items
        urls = set()
        for it in items:
            if it.get("content"):
                for img_url in Crawler._extract_image_urls(it["content"]):
                    urls.add(img_url)

        if not urls:
            print("[Crawler] Phase 2 — no images found, skipping")
            return

        print(f"[Crawler] Phase 2 — downloading {len(urls)} unique images")
        processor = self._get_image_processor()
        url_map = processor.download(*urls, storage=image_storage)
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

    def persist(
        self,
        *items: Dict[str, Any],
        output_style: OutputStyle,
    ) -> None:
        """Persist item dicts via the backend matching *output_style*."""

        data = self._to_newsdata(list(items))

        match output_style:
            case OutputStyle.MARKDOWN:
                self._persist_md(data)
            case OutputStyle.HTML:
                self._persist_html(data)
            case OutputStyle.SQLITE:
                self._persist_sqlite(data)
            case OutputStyle.POSTGRESQL:
                self._persist_postgresql(data)

    def _to_newsdata(
        self,
        items: List[Dict[str, Any]],
    ) -> NewsData:
        """Build a :class:`NewsData` from a list of item dicts."""
        tz = self._config.get("app", {}).get("timezone", "Asia/Shanghai")
        date = format_date_folder(tz)

        by_source: Dict[str, List[NewsItem]] = {}
        for d in items:
            sid = d.get("source_id", "manual")
            ti = self._source_tiers.get(sid, {})
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
                ranks=d.get("ranks", []),
                crawled_at=format_datetime_now(tz),
            ))

        return NewsData(date=date, items=by_source)

    # ── Backend-specific persist ─────────────────────────────────────

    @staticmethod
    def _yaml_str(value: str) -> str:
        """Escape a string for safe YAML value output.

        Unquoted when safe; double-quoted with internal escapes otherwise.
        """
        if not value:
            return '""'
        # If the value looks safe (no colons at start, no quotes, no
        # leading/trailing whitespace), return it bare.
        if (
            not value.startswith((" ", "-", ":", "#", "!", ">", "|", "&", "*"))
            and '"' not in value
            and "'" not in value
            and "\n" not in value
        ):
            return value
        # Otherwise double-quote with escapes
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _build_frontmatter(item: NewsItem) -> str:
        """Build YAML frontmatter block from a :class:`NewsItem`."""
        hostname = ""
        if item.url:
            try:
                hostname = urlparse(item.url).hostname or ""
            except Exception:
                pass

        lines = ["---"]
        if item.title:
            lines.append(f"title: {Crawler._yaml_str(item.title)}")
        if item.url:
            lines.append(f"url: {Crawler._yaml_str(item.url)}")
        if hostname:
            lines.append(f"hostname: {Crawler._yaml_str(hostname)}")
        if item.summary:
            lines.append(f"description: {Crawler._yaml_str(item.summary)}")
        if item.published_at:
            lines.append(f"date: {item.published_at[:10]}")
        lines.append("---\n")
        return "\n".join(lines)

    def _persist_md(self, data: NewsData) -> None:
        """Write each article to a Markdown file via the storage layer.

        A YAML frontmatter block is prepended to the content, built from
        item metadata (title, url, hostname, description, date).
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

                full_content = self._build_frontmatter(item) + (item.content or "")

                path = f"news/{data.date}/{safe_title}.md"
                saved = self._local_storage.save(
                    full_content.encode("utf-8"),
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
                saved = self._local_storage.save(
                    (item.content or "").encode("utf-8"),
                    path,
                    content_type="text/html",
                )
                print(f"[Crawler] Saved: {saved}")

    def _persist_sqlite(self, data: NewsData) -> None:
        """Save to SQLite database."""
        db = self._get_sqlite_db()
        db.save_news_data(data, self._source_tiers)
        db.cleanup()

    def _persist_postgresql(self, data: NewsData) -> None:
        """Save to PostgreSQL.

        Transforms relative image paths (``images/xxx.png``) to
        ``/media/news/YYYY-MM-DD/images/xxx.png`` so the web
        ``/media/`` proxy can resolve them to S3 objects.
        """
        # Transform image paths for web/S3 resolution
        date_str = format_date_folder()
        media_prefix = f"/media/news/{date_str}/images/"
        for items in data.items.values():
            for item in items:
                if item.content:
                    item.content = item.content.replace("images/", media_prefix)

        db = self._get_pg_db()
        result = db.save_news_data(data, self._source_tiers, crawled_from="local")
        print(f"[Crawler] PG save result: {result}")

    # ═══════════════════════════════════════════════════════════════════
    # Cloud sync — download S3 SQLite DBs → merge into PostgreSQL
    # ═══════════════════════════════════════════════════════════════════

    def sync_from_cloud(self) -> dict:
        """Download recent SQLite DBs from S3, enrich incremental content,
        and merge into PostgreSQL via UPSERT.

        Queries PostgreSQL for the latest cloud-synced ``crawled_at``
        timestamp, then downloads all S3 DB files from that date onwards.
        Within each DB, only rows with ``created_at`` strictly after the
        threshold are enriched and synced — avoiding redundant HTTP
        requests for content that was already synced.

        Uses UPSERT (NOT DO NOTHING) so that previously synced rows get
        their metadata refreshed on re-crawl.
        """
        from datetime import datetime
        from storage.s3 import S3Client

        cloud_config = self._config["storage"]["cloud"]
        cloud_storage = S3Client.init_by_config(cloud_config)
        if not cloud_storage:
            print("[Sync] S3 not configured, skipping")
            return

        # ── Determine sync threshold ──────────────────────────────
        pg_db = self._get_pg_db()
        latest_crawled = pg_db.get_latest_cloud_sync_date()  # datetime or None

        if latest_crawled is not None:
            print(f"\n[Sync] Latest cloud crawled_at in PG: {latest_crawled}")
            since_dt = latest_crawled.date()  # for S3 key comparison
            threshold_str = latest_crawled.strftime("%Y-%m-%d %H:%M:%S")  # for row filtering
        else:
            print("[Sync] No cloud records in PG — syncing all available S3 DBs")
            since_dt = None
            threshold_str = None

        # ── Discover DBs on S3 ────────────────────────────────────
        all_keys = cloud_storage.list_objects(prefix="db/", max_keys=5000)
        db_keys: List[str] = []

        for key in all_keys:
            if not key.endswith(".db"):
                continue
            basename = key.rsplit("/", 1)[-1]  # "2026-06-10.db"
            date_str = basename.replace(".db", "")
            try:
                key_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
                if since_dt is None or key_dt >= since_dt:
                    db_keys.append(key)
            except ValueError:
                continue  # skip keys with unexpected names

        if not db_keys:
            print("[Sync] No DBs found to sync")
            return

        print(f"[Sync] Found {len(db_keys)} DB(s) to sync: {sorted(db_keys)}")

        # ── Process each day ──────────────────────────────────────
        total_new = 0
        total_skipped = 0

        for key in sorted(db_keys):
            date_str = key.rsplit("/", 1)[-1].replace(".db", "")
            print(f"\n[Sync] Processing {date_str}...")

            tmp = cloud_storage.download_to_temp(key)
            if tmp is None:
                print(f"[Sync] Failed to download {key}, skipping")
                continue

            day_new = 0
            day_skipped = 0

            try:
                rows = Crawler._read_sqlite_db(tmp)
                print(f"[Sync] Read {len(rows)} rows from {date_str}.db")

                # ── Incremental filtering ─────────────────────────
                if threshold_str is not None:
                    before = len(rows)
                    rows = [
                        r for r in rows
                        if r.get("created_at", "") > threshold_str
                    ]
                    filtered = before - len(rows)
                    if filtered > 0:
                        print(
                            f"[Sync] Filtered {filtered} rows "
                            f"(created_at <= {threshold_str})"
                        )

                if rows:
                    self.enrich_content(*rows, with_image=True)

                    news_data = Crawler._rows_to_newsdata(rows, date_str)
                    result = pg_db.save_news_data(
                        news_data, self._source_tiers, crawled_from="cloud", skip_existing=False,
                    )
                    day_new = result.get("processed", 0)
                    day_skipped = result.get("skipped", 0)
                    total_new += day_new
                    total_skipped += day_skipped

            except Exception as e:
                print(f"[Sync] Failed to sync {date_str}: {e}")
            finally:
                try:
                    os.unlink(str(tmp))
                except OSError:
                    pass

            print(
                f"[Sync] {date_str} done: {day_new} upserted, {day_skipped} skipped"
            )

        print(
            f"\n[Sync] Complete: {total_new} upserted, {total_skipped} skipped "
            f"({len(db_keys)} day(s) processed)"
        )
        return {
            "upserted": total_new,
            "skipped": total_skipped,
            "days": len(db_keys),
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
        """Convert SQLite rows (optionally enriched) to a NewsData object."""
        items: Dict[str, List[NewsItem]] = {}

        for row in rows:
            source_id = row.get("source_id", "unknown")
            if source_id not in items:
                items[source_id] = []

            # tags may be a JSON string (raw SQLite) or a list (after enrichment)
            tags_val = row.get("tags", [])
            if isinstance(tags_val, str):
                try:
                    tags = json.loads(tags_val)
                except (json.JSONDecodeError, TypeError):
                    tags = []
            elif isinstance(tags_val, list):
                tags = tags_val
            else:
                tags = []

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
                content=row.get("content", ""),
                author=row.get("author", ""),
                category=row.get("category", ""),
                tags=tags,
                crawled_at=row.get("created_at", ""),
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


# ── Keyword extraction (jieba TextRank fallback) ─────────────────

def _extract_keywords_textrank(content: str, topk: int = 5) -> list[str]:
    """从 Markdown 正文提取关键词，用作 tags fallback。

    仅当页面元数据（meta keywords / JSON-LD）无 tags 时调用。
    使用 jieba TextRank 算法 + 词性过滤，适合中文新闻正文。

    Args:
        content: Markdown 格式的 article body。
        topk: 最多返回的关键词数量。

    Returns:
        关键词列表（可能少于 *topk* 当正文信息量不足时），
        或空列表当正文过短或无法提取。
    """
    # ── 清洗 Markdown 语法 ──────────────────────────────────────
    text = re.sub(r'!\[.*?\]\(.*?\)', '', content)          # 图片
    text = re.sub(r'\[([^\]]*)\]\(.*?\)', r'\1', text)      # 链接保留文字
    text = re.sub(r'[#*>`|~\-_]', ' ', text)                # 格式标记
    text = re.sub(r'\s+', ' ', text).strip()

    if len(text) < 50:
        return []

    # ── jieba TextRank ─────────────────────────────────────────
    try:
        import jieba.analyse
    except ImportError:
        return []

    keywords = jieba.analyse.textrank(
        text,
        topK=topk,
        withWeight=False,
        allowPOS=('ns', 'n', 'vn', 'nr', 'nt', 'nz'),
    )
    return keywords
