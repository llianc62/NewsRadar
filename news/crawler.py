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
import threading
import time
import requests

from enum import Enum
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from news.fetcher import NewsnowFetcher, RssFetcher
from news.parser import registry as parser
from news.images import ImageProcessor
from news.models import NewsData, NewsItem
from storage.files import FileStorage, LocalStorage, S3Storage
from utils import (
    format_date_today, format_datetime_now, format_time_now, sanitize_filename,
    http_get_with_retry,
)

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

MAX_IMAGE_PROCESSOR_WORKERS = 20  # Limit for concurrent image downloads

_WAF_DOMAINS: frozenset[str] = frozenset({"xueqiu.com", "huxiu.com", "juejin.cn"})


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
        pg_db: Any = None,
    ):
        self._config = config

        cfg = config.get("crawler", {})
        self.max_workers = cfg.get("max_workers", 8)
        self.timeout = cfg.get("timeout", 30)
        self.max_retry = cfg.get("max_retry", 3)

        self.parser = parser

        # Source tiers — built once from config, rarely changes
        self._source_tiers = self._build_source_tiers()

        # File storage — always local (markdown/html file output)
        storage_conf = config.get("storage", {})
        data_dir = storage_conf.get("local", {}).get("data_path", "output")
        self._local_storage = LocalStorage(data_dir)

        # Resource storage — S3 for images when configured, else local filesystem
        resource_cfg = storage_conf.get("resource", {})
        if any(resource_cfg.get(k) for k in (
            "endpoint_url", "bucket_name", "access_key_id", "secret_access_key"
        )):
            self._resource_storage: FileStorage = S3Storage(resource_cfg)
        else:
            self._resource_storage = self._local_storage

        # Thread pool (lazy)
        self._executor: Optional[ThreadPoolExecutor] = None

        # DB connections (lazy or injected)
        self._pg_db = pg_db
        self._sqlite: Any = None

        # HTTP session (lazy)
        self._session: Optional[requests.Session] = None

        # Image processor (lazy) — shared across fetch calls
        self._image_processor: Optional[ImageProcessor] = None

        # Analyzer (lazy) — shared analyzer instance
        self._analyzer: Any = None

        # Playwright — per-domain browser for WAF-protected sites.
        # Each WAF domain gets a dedicated single-thread executor; the
        # worker thread owns the browser, satisfying sync_playwright's
        # greenlet thread-affinity.
        self._playwright_executors: dict[str, ThreadPoolExecutor] = {}
        self._playwright_browsers: dict[str, tuple[Any, Any]] = {}

    # ── HTTP session ─────────────────────────────────────────────────

    @staticmethod
    def _hook_response_encoding(response, *args, **kwargs):
        """Response hook: correct encoding when the server omits charset.

        RFC 2616 §3.7.1 defaults to ISO-8859-1 when no charset is
        specified, but real-world sites serve UTF-8, GBK, and other
        encodings.  chardet (via ``apparent_encoding``) detects the real
        encoding and we apply it before ``resp.text`` is ever accessed.
        """
        if response.encoding == "ISO-8859-1":
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
            data_dir = storage_conf.get("local", {}).get("data_path", "output")
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

    def _get_analyzer(self):
        if self._analyzer is None:
            from news.analyzer import create_analyzer
            try:
                db = self._get_pg_db()
            except Exception:
                db = None
            self._analyzer = create_analyzer(self._config, db=db)
        return self._analyzer

    def _build_source_tiers(self) -> dict:
        """Build ``{source_id: {tier, priority}}`` mapping from config."""
        tiers = {}
        for s in self._config.get("crawler", {}).get("newsnow", {}).get("sources", []):
            tiers[s["id"]] = {"tier": s.get("tier", 4), "priority": s.get("priority", 0)}
        for rss in self._config.get("crawler", {}).get("rss", {}).get("sources", []):
            if rss.get("enabled", True):
                tiers[rss["id"]] = {"tier": rss.get("tier", 3), "priority": rss.get("priority", 0)}
        tiers["manual"] = {"tier": 4, "priority": 0}
        return tiers

    def _dedup_items_by_url(
        self,
        items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Deduplicate items by URL, keeping the highest-priority source.

        When multiple items share the same URL, the item from the source
        with the highest ``priority`` (from :meth:`_build_source_tiers`)
        is kept.  Items with equal priority retain the first encountered.
        Items with empty URLs are passed through without dedup.
        """
        seen: Dict[str, int] = {}       # url -> index in result
        result: List[Dict[str, Any]] = []
        duplicates_removed = 0

        for item in items:
            url = item.get("url", "")
            if not url:
                result.append(item)
                continue

            if url not in seen:
                seen[url] = len(result)
                result.append(item)
                continue

            # URL collision — compare priorities
            existing_idx = seen[url]
            existing_item = result[existing_idx]

            existing_sid = existing_item.get("source_id", "")
            current_sid = item.get("source_id", "")

            existing_prio = self._source_tiers.get(
                existing_sid, {}
            ).get("priority", 0)
            current_prio = self._source_tiers.get(
                current_sid, {}
            ).get("priority", 0)

            if current_prio > existing_prio:
                result[existing_idx] = item

            duplicates_removed += 1

        if duplicates_removed > 0:
            print(
                f"[Crawler] Dedup: removed {duplicates_removed} duplicate URLs "
                f"(kept {len(result)} of {len(items)} items)"
            )

        return result

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
        if self._needs_playwright(url):
            html, error = self._download_with_playwright(url, self.timeout)
            if html is None:
                raise Exception(f"Playwright 页面下载失败: {error}")
        else:
            try:
                resp = self.session().get(url, timeout=self.timeout)
                resp.raise_for_status()
            except requests.RequestException as e:
                raise requests.RequestException(
                    f"HTTP 请求失败: {e}"
                ) from e
            html = resp.text

        # ── Parse to Markdown ──────────────────────────────────────
        result = self.parser.parse(item["source_id"], html, url)
        if not result:
            raise Exception(f"无法提取页面正文内容: {url}")

        item["title"] = result.get("title")
        item["author"] = result.get("author", "")
        item["published_at"] = result.get("published_at", "")
        item["summary"] = result.get("summary", "")
        item["category"] = result.get("category", "")
        item["tags"] = result.get("tags", [])

        if not with_content:
            self.persist(item, output_style=output_style)
            return

        # ── Persistence ────────────────────────────────────────────
        if with_content:
            # Phase 1: download HTML + parse Markdown
            item["content"] = result["markdown"]
            if not item["tags"] and item.get("content"):
                item["tags"] = self._extract_keywords(item["content"])

            # Phase 2: batch image download (if requested)
            if with_image:
                storage = self._resource_storage
                if target_storage == StorageTarget.LOCAL:
                    storage = self._local_storage
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
        date = format_date_today(timezone)
        time_str = format_time_now(timezone)

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

        # ── Cross-source URL dedup ─────────────────────────────────
        all_items = self._dedup_items_by_url(all_items)

        all_items = self._filter_existing_content_urls(all_items)

        # ── Enrichment ─────────────────────────────────────────────
        if with_content:
            self.enrich_content(*all_items, with_image=with_image)

        # ── Analysis ───────────────────────────────────────────────
        analyzer = self._get_analyzer()
        if analyzer is not None:
            # Sentiment: analyze items that have content body
            contentful_items = [it for it in all_items if it.get("content")]
            if contentful_items:
                analyzer.analyze_sentiment(contentful_items)

            # Heat: compute heat score for ALL items (hotlist + RSS)
            # using tier-base × time-decay formula — no DB roundtrip
            if all_items:
                # Inject tier from source config (fetcher item dicts
                # don't carry tier — it's added later during persistence)
                for it in all_items:
                    sid = it.get("source_id", "")
                    ti = self._source_tiers.get(sid, {})
                    it.setdefault("tier", ti.get("tier", 4))
                analyzer.analyze_heat(all_items)

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

    def _filter_existing_content_urls(
        self,
        items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Remove items whose content already exists in the DB.

        For items with a URL, checks by URL.  For items without a URL
        (e.g. RSS feeds that only ship inline HTML), checks by
        ``(source_id, guid)``.

        If the DB is unavailable, all items pass through unchanged.
        """
        try:
            pg = self._get_pg_db()
        except Exception:
            return items

        # ── URL-based check ───────────────────────────────────────
        url_items = [it for it in items if it.get("url")]
        existing_urls: set = set()
        if url_items:
            try:
                existing_urls = pg.get_urls_with_content(
                    [it["url"] for it in url_items]
                )
            except Exception:
                pass

        # ── GUID-based check (no-URL items only) ───────────────────
        existing_guids: set = set()
        no_url_pairs = [
            (it["source_id"], it["guid"])
            for it in items
            if not it.get("url") and it.get("guid")
        ]
        if no_url_pairs:
            # Group by source_id for batch query
            by_source: Dict[str, List[str]] = {}
            for sid, guid in no_url_pairs:
                by_source.setdefault(sid, []).append(guid)

            try:
                with pg.get_conn() as conn:
                    with conn.cursor() as cur:
                        for sid, guids in by_source.items():
                            cur.execute(
                                """SELECT guid FROM news_articles
                                   WHERE source_id = %s
                                     AND guid = ANY(%s)
                                     AND content IS NOT NULL
                                     AND content != ''""",
                                (sid, guids),
                            )
                            for row in cur:
                                existing_guids.add((sid, row[0]))
            except Exception:
                pass

        if not existing_urls and not existing_guids:
            return items

        skipped = 0
        filtered = []
        for it in items:
            if it.get("url") and it["url"] in existing_urls:
                skipped += 1
            elif (not it.get("url")
                  and (it.get("source_id"), it.get("guid")) in existing_guids):
                skipped += 1
            else:
                filtered.append(it)

        if skipped:
            print(
                f"[Crawler] Skipping {skipped} items that already have content in DB"
            )

        return filtered

    def _run_batch_parse(
        self,
        items: List[Dict[str, Any]],
    ) -> None:
        """Phase 1: download HTML + parse Markdown for all items via thread pool.

        Sets ``item["content"]`` (Markdown) and metadata fields
        (title, author, published_at, summary, category, tags).

        ``_download_and_parse`` internally routes WAF URLs through
        the per-domain Playwright executor.
        """
        valid = [it for it in items if it.get("url") and not it.get("content")]
        if not valid:
            return

        print(f"[Crawler] Phase 1 — downloading & parsing {len(valid)} items "
              f"(workers={self.max_workers})")

        begin = time.time()
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

        elapsed = time.time() - begin
        print(f"[Crawler] Phase 1 done: {success}/{len(valid)} success "
              f"({elapsed:.1f}s)")

    @staticmethod
    def _get_url_domain(url: str) -> str:
        """Extract the hostname portion of *url*, stripping ``www.`` prefix."""
        host = urlparse(url).hostname or ""
        if host.startswith("www."):
            host = host[4:]
        return host

    @staticmethod
    def _needs_playwright(url: str) -> bool:
        """Check whether *url* requires Playwright to bypass WAF."""
        return Crawler._get_url_domain(url) in _WAF_DOMAINS

    def _get_playwright_browser(self, domain: str):
        """Return the headless Chromium browser for *domain*, creating it on
        first call via the domain executor so the browser lives on the
        same thread that uses it.

        Returns ``None`` if *domain* is not WAF-protected.
        """
        if domain not in _WAF_DOMAINS:
            return None
        if domain not in self._playwright_browsers:
            # Trigger the executor's initializer (creates browser in
            # the executor's worker thread), then wait for it to finish.
            self._get_playwright_executor(domain).submit(lambda: None).result()
        return self._playwright_browsers[domain][1]

    def _get_playwright_executor(self, domain: str) -> ThreadPoolExecutor:
        """Return the single-thread executor for *domain*, creating it on
        first call.

        Stores the executor in ``_playwright_executors`` *before* calling
        ``_get_playwright_browser`` so the recursive call inside
        ``_get_playwright_browser`` can short-circuit.
        """
        if domain in self._playwright_executors:
            return self._playwright_executors[domain]

        def _init_worker() -> None:
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            self._playwright_browsers[domain] = (pw, pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            ))

        self._playwright_executors[domain] = ThreadPoolExecutor(
            max_workers=1, initializer=_init_worker,
        )
        # Ensure the browser is ready before returning
        self._get_playwright_browser(domain)
        return self._playwright_executors[domain]

    def _download_with_playwright(
        self,
        url: str,
        timeout: int,
    ) -> tuple[str | None, str | None]:
        """Download HTML via headless Playwright for WAF-protected sites.

        Dispatches to the domain executor internally — safe to call from
        any thread.

        Returns:
            ``(html, None)`` on success, ``(None, error_message)`` on failure.
        """
        domain = self._get_url_domain(url)
        # Both calls are idempotent — browser & executor are created
        # once per domain and reused.
        browser = self._get_playwright_browser(domain)

        def _do():
            try:
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/149.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                    locale="zh-CN",
                )
                page = context.new_page()
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined,
                    });
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [1, 2, 3, 4, 5],
                    });
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['zh-CN', 'zh', 'en'],
                    });
                """)
                try:
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=timeout * 1_000)
                    # Wait for post-WAF-challenge reloads to settle
                    # (e.g. juejin.cn / xueqiu.com load content
                    # dynamically after JS challenge).
                    # Some sites (huxiu.com, etc.) have persistent
                    # analytics/ads connections that prevent networkidle
                    # from ever firing — don't fail the whole request,
                    # page.content() is already available after
                    # domcontentloaded for those sites.
                    try:
                        page.wait_for_load_state(
                            "networkidle",
                            timeout=timeout * 1_000,
                        )
                    except Exception:
                        pass
                    html = page.content()
                    return html, None
                finally:
                    context.close()
            except Exception as e:
                return None, str(e)

        executor = self._get_playwright_executor(domain)
        for attempt in range(2):
            html, error = executor.submit(_do).result()
            if html is not None:
                return html, None
            # Retry on timeout — WAF challenges can take longer under load
            if "Timeout" in (error or "") and attempt == 0:
                print(f"[Crawler] Playwright timeout for {url}, retrying...")
                continue
            return None, error
        return None, "Playwright: max retries exceeded"

    def _download_and_parse(self, item: Dict[str, Any]) -> bool:
        """Download HTML for a single item, parse to Markdown (no images).

        Sets ``item["content"]`` (Markdown), and metadata fields
        (title, author, published_at, summary, category, tags)
        extracted from the page.

        WAF-protected domains (e.g. xueqiu.com) are routed through
        headless Playwright; all others use the standard ``requests``
        session.
        """
        url = item.get("url", "")
        if not url:
            return False

        if self._needs_playwright(url):
            html, error = self._download_with_playwright(url, self.timeout)
            if html is None:
                print(f"[Crawler] Playwright error for {url}: {error}")
                self._record_content_fetch_failure(item, error)
                return False
        else:
            resp, error = http_get_with_retry(
                self.session(), url, self.timeout, label=url
            )
            if resp is None:
                print(f"[Crawler] HTTP error for {url}: {error}")
                self._record_content_fetch_failure(item, error)
                return False
            html = resp.text

        # Pure text parsing — no image processing
        result = self.parser.parse(item["source_id"], html, url)
        if result is None:
            print(f"[Crawler] No content extracted: {url}")
            return False

        item["content"] = result["markdown"]
        item["author"] = result.get("author", "")
        item["published_at"] = result.get("published_at", "")
        item["summary"] = result.get("summary", "")
        item["category"] = result.get("category", "")
        item["tags"] = result.get("tags", [])
        if not item["tags"] and item.get("content"):
            item["tags"] = self._extract_keywords(item["content"])
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

        tasks: Dict[str, dict] = {}
        for item in items:
            if not item.get("content"):
                continue
            article_url = item.get("url", "")
            date = format_date_today()
            for url in self._extract_image_urls(item["content"]):
                if url in tasks:
                    continue
                target_dir = f"news/{date}/images"
                tasks[url] = {
                    "target_dir": target_dir,
                    "article_url": article_url,
                }

        if not tasks:
            print("[Crawler] Phase 2 — no images found, skipping")
            return

        print(f"[Crawler] Phase 2 — downloading {len(tasks)} unique images")
        processor = self._get_image_processor()
        url_map = processor.download(tasks, storage=image_storage)

        # Record failures for lazy retry
        self._record_image_download_failures(tasks, url_map)

        if not url_map:
            print("[Crawler] Phase 2 done (no images downloaded)")
            return

        # Build a single regex that matches any downloaded image URL.
        # Sort by length descending so longer URLs are tried first —
        # prevents a shorter URL from matching inside a longer one.
        _escaped = [
            re.escape(url) for url in
            sorted(url_map.keys(), key=len, reverse=True)
        ]
        _pattern = re.compile("|".join(_escaped))

        def _replacer(m: re.Match) -> str:
            return url_map[m.group(0)]

        t0 = time.time()
        replaced = 0
        for item in items:
            md = item.get("content", "")
            if not md:
                continue
            new_md, n = _pattern.subn(_replacer, md)
            if n:
                item["content"] = new_md
                replaced += n

        elapsed = time.time() - t0
        article_count = sum(1 for item in items if item.get("content"))
        print(f"[Crawler] Phase 2 done: {replaced} replacements across "
              f"{article_count} articles ({elapsed:.1f}s)")

    def _extract_image_urls(self, markdown: str) -> List[str]:
        """Extract image URLs from Markdown text.

        Matches both Markdown image syntax (``![alt](url)``) and inline
        HTML ``<img src="url">`` tags.
        """
        urls: List[str] = []
        # Markdown image: ![alt](url) or ![alt](url "title")
        urls.extend(re.findall(r'!\[.*?\]\((https?://[^\s)]+)(?:\s+"[^"]*")?\)', markdown))
        # HTML img: <img src="url">
        urls.extend(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', markdown, re.IGNORECASE))
        return urls

    def _record_content_fetch_failure(
        self, item: Dict[str, Any], error: str,
    ) -> None:
        """Record a failed content fetch to the ``failed_tasks`` table."""
        context = {
            "url": item.get("url", ""),
            "source_id": item.get("source_id", ""),
            "source_type": item.get("source_type", ""),
            "source_name": item.get("source_name", ""),
            "title": item.get("title", ""),
            "rank": item.get("rank", 0),
            "guid": item.get("guid", ""),
            "mobile_url": item.get("mobile_url", ""),
            "published_at": item.get("published_at", ""),
        }
        try:
            pg = self._get_pg_db()
            task_id = pg.record_failure("content_fetch", context, self.max_retry)
            if task_id:
                print(f"[Crawler] Recorded content_fetch failure: {item.get('url')}")
        except Exception as e:
            print(f"[Crawler] Failed to record content_fetch failure: {e}")

    def _record_image_download_failures(
        self,
        tasks: Dict[str, dict],
        results: Dict[str, str],
    ) -> None:
        """For each image URL where result is ``""``, record a failure.

        Args:
            tasks: ``{url: {"target_dir": str, "article_url": str}, ...}``
                (pre-download, for context).
            results: ``{url: saved_path_or_""}`` (post-download, for checking).
        """
        for url, saved_path in results.items():
            if saved_path:
                continue
            t = tasks.get(url, {})
            context = {
                "url": url,
                "target_dir": t.get("target_dir", ""),
                "article_url": t.get("article_url", ""),
            }
            try:
                pg = self._get_pg_db()
                pg.record_failure("image_download", context, self.max_retry)
                print(f"[Crawler] Recorded image_download failure: {url}")
            except Exception as e:
                print(f"[Crawler] Failed to record image_download failure: {e}")

    def _retry_content_fetch_failures(self) -> List[Dict[str, Any]]:
        """Retry previously failed content_fetch tasks.

        Queries ``failed_tasks`` for pending content_fetch tasks, calls
        ``_download_and_parse`` for each, and returns successfully
        retried items as dicts.
        """
        pg = self._get_pg_db()
        tasks = pg.get_pending_failures(task_type="content_fetch")
        if not tasks:
            return []

        print(f"[Crawler] Retrying {len(tasks)} content_fetch failures...")
        retried: List[Dict[str, Any]] = []

        for task in tasks:
            ctx = task["context"]
            url = ctx.get("url", "")
            if not url:
                pg.mark_failure_completed(task["id"])
                continue

            # Prevent duplicate download: check if article already has content
            if pg.article_has_content(url):
                pg.mark_failure_completed(task["id"])
                print(f"[Crawler] Article already has content, skip retry: {url}")
                continue

            # Reconstruct item dict from context
            item: Dict[str, Any] = {
                "url": url,
                "source_id": ctx.get("source_id", ""),
                "source_type": ctx.get("source_type", ""),
                "source_name": ctx.get("source_name", ""),
                "title": ctx.get("title", ""),
                "rank": ctx.get("rank", 0),
                "guid": ctx.get("guid", ""),
                "mobile_url": ctx.get("mobile_url", ""),
                "published_at": ctx.get("published_at", ""),
                "summary": "",
                "author": "",
                "content": "",
                "category": "",
                "tags": [],
                "ranks": [],
            }

            success = self._download_and_parse(item)
            if success:
                pg.mark_failure_completed(task["id"])
                retried.append(item)
                print(f"[Crawler] Retry success (content_fetch): {url}")
            else:
                pg.mark_failure_retried(task["id"], error="HTTP failed after retries")
                print(f"[Crawler] Retry failed (content_fetch): {url}")

        return retried

    def _retry_image_download_failures(self) -> Dict[str, int]:
        """Retry previously failed image_download tasks.

        Downloads each failed image, updates article content with the
        new image path, and marks the task completed.

        Must be called AFTER articles are persisted.
        """
        pg = self._get_pg_db()
        tasks = pg.get_pending_failures(task_type="image_download")
        if not tasks:
            return {"total": 0, "success": 0}

        print(f"[Crawler] Retrying {len(tasks)} image_download failures...")
        processor = self._get_image_processor()
        storage = self._resource_storage

        total = len(tasks)
        success = 0

        for task in tasks:
            ctx = task["context"]
            url = ctx.get("url", "")
            target_dir = ctx.get("target_dir", "")

            if not url:
                pg.mark_failure_completed(task["id"])
                continue

            result = processor.download(
                {url: {"target_dir": target_dir,
                       "article_url": ctx.get("article_url", "")}},
                storage,
            )
            saved_path = result.get(url, "")

            if saved_path:
                pg.mark_failure_completed(task["id"])
                success += 1

                # Update article content — replace old URL with new path
                article_ids = pg.find_articles_by_image_url(url)
                for article_id in article_ids:
                    pg.update_article_image_url(article_id, url, saved_path)

                print(f"[Crawler] Retry success (image_download): {url}")
            else:
                pg.mark_failure_retried(task["id"], error="Image download failed")
                print(f"[Crawler] Retry failed (image_download): {url}")

        return {"total": total, "success": success}

    def retry_failed_tasks(self) -> dict:
        """Retry previously failed content_fetch and image_download tasks.

        Called by the daemon AFTER ``fetch_all`` in each crawl cycle.
        Does NOT modify ``fetch_all`` — lazy retry is a separate step.

        Returns a summary dict with counts.
        """
        print("\n[Crawler] === Lazy retry: checking failed tasks ===")
        result = {
            "content_retried": 0,
            "content_success": 0,
            "image_retried": 0,
            "image_success": 0,
        }

        # 1. Retry content_fetch failures
        try:
            retried_items = self._retry_content_fetch_failures()
            result["content_retried"] = len(retried_items)

            if retried_items:
                # Enrich + persist retried items
                self.enrich_content(*retried_items, with_image=True)
                self.persist(
                    *retried_items, output_style=OutputStyle.POSTGRESQL
                )
                result["content_success"] = len(retried_items)
        except Exception as e:
            print(f"[Crawler] Content retry error (non-fatal): {e}")

        # 2. Retry image_download failures (must be AFTER persist)
        try:
            img_result = self._retry_image_download_failures()
            result["image_retried"] = img_result["total"]
            result["image_success"] = img_result["success"]
        except Exception as e:
            print(f"[Crawler] Image retry error (non-fatal): {e}")

        print(f"[Crawler] Lazy retry done: {result}")
        return result

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
        date = format_date_today(tz)

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
                heat_score=d.get("heat_score", 0),
                sentiment_score=d.get("sentiment_score", 0),
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

        Content image paths are stored as bare ``images/xxx.jpg``.
        The web rendering layer resolves them to ``/media/`` URLs
        using each article's ``updated_at`` date.
        """
        db = self._get_pg_db()
        result = db.save_news_data(data, self._source_tiers, crawled_from="local")
        print(f"[Crawler] PG save result: {result}")

    # ═══════════════════════════════════════════════════════════════════
    # Cloud sync — download S3 SQLite DBs → merge into PostgreSQL
    # ═══════════════════════════════════════════════════════════════════

    def sync_from_cloud(self) -> dict:
        """Download recent SQLite DBs from S3, enrich incremental content,
        and merge into PostgreSQL via UPSERT.

        Queries PostgreSQL for the latest cloud-synced ``updated_at``
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
            print(f"\n[Sync] Latest cloud updated_at in PG: {latest_crawled}")
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
                heat_score=row.get("heat_score") or 0,
                guid=row.get("guid", ""),
                published_at=row.get("published_at") or format_datetime_now(),
                summary=row.get("summary", ""),
                content=row.get("content", ""),
                author=row.get("author", ""),
                category=row.get("category", ""),
                tags=tags,
            )
            items[source_id].append(item)

        return NewsData(date=date_str, items=items)

    # ── Keyword extraction (delegates to analyzer) ─────────────────

    def _extract_keywords(self, content: str, topk: int = 5) -> list[str]:
        """从 Markdown 正文提取关键词，委托给 Analyzer。

        当 analyzer 启用时使用 TF-IDF（优先）+ TextRank（兜底），
        禁用时退回到模块级 TextRank。
        """
        analyzer = self._get_analyzer()
        if analyzer is not None:
            return analyzer.extract_keywords(content, topk=topk)
        from news.analyzer.jieba import extract_keywords_textrank
        return extract_keywords_textrank(content, topk=topk)

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



