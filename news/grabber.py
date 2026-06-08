# coding=utf-8
"""Content grabber — download HTML, parse Markdown, save per OutputStyle.

Provides a content processing pipeline that bridges Fetcher results and
storage backends.  Supports two work modes:

* ``run(url, output_style)`` — single URL (CLI ``grab_one``)
* ``run_batch(items, output_style)`` — batch with thread-pool (crawler)
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from news.parser import HtmlParser


# ═══════════════════════════════════════════════════════════════════
# OutputStyle
# ═══════════════════════════════════════════════════════════════════


class OutputStyle(Enum):
    """Storage target for Grabber output."""

    MARKDOWN = "markdown"
    HTML = "html"
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


# ═══════════════════════════════════════════════════════════════════
# Grabber
# ═══════════════════════════════════════════════════════════════════


class Grabber:
    """Content processing pipeline: download → parse → save.

    Two work modes:

    * ``run(url, output_style)`` — fetch and process a single URL.
    * ``run_batch(items, output_style)`` — process many items concurrently
      via :class:`~concurrent.futures.ThreadPoolExecutor`.

    Usage::

        grabber = Grabber(config)
        grabber.run("https://example.com", OutputStyle.MARKDOWN)

        items = [{"url": "...", "title": "...", "source_id": "..."}, ...]
        grabber.run_batch(items, OutputStyle.SQLITE)
    """

    def __init__(
        self,
        config: dict,
        parser: HtmlParser | None = None,
        image_processor: Any | None = None,
    ):
        cfg = config.get("crawler", {})
        self.max_workers = cfg.get("max_workers", 5)
        self.min_interval = cfg.get("interval", 2000) / 1000  # ms → s
        self.timeout = cfg.get("timeout", 30)

        self.parser = parser or HtmlParser(config)
        self.image_processor = image_processor

        storage_cfg = config.get("storage", {})
        self.data_dir = storage_cfg.get("local", {}).get("data_dir", "output")

        self._session: Optional[requests.Session] = None

    # ── Session (lazy, shared across workers) ──────────────────────

    @property
    def session(self) -> requests.Session:
        """Lazy-create a shared requests Session with default headers."""
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

    # ── Public API ─────────────────────────────────────────────────

    def run(self, url: str, output_style: OutputStyle) -> None:
        """Fetch and process a single URL.

        Args:
            url: Web page URL.
            output_style: Where to save the result.
        """
        item = {"url": url, "title": url, "source_id": "manual", "date": ""}
        self._process_one(item, output_style)

    def run_batch(
        self,
        items: List[Dict[str, Any]],
        output_style: OutputStyle,
    ) -> None:
        """Batch process items with a thread pool.

        Args:
            items: List of dicts, each must contain ``"url"``.
            output_style: Where to save results.
        """
        if not items:
            return

        total = len([it for it in items if it.get("url")])
        print(f"[Grabber] Processing {total} items "
              f"(workers={self.max_workers}, output={output_style.value})")

        success = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {
                pool.submit(self._process_one, item, output_style): item
                for item in items
                if item.get("url")
            }

            for future in as_completed(futures):
                item = futures[future]
                try:
                    ok = future.result()
                    if ok:
                        success += 1
                    else:
                        failed += 1
                except Exception as e:
                    print(f"[Grabber] Worker error for {item.get('url', '?')}: {e}")
                    failed += 1

        print(f"[Grabber] Done: {success} success, {failed} failed")

    async def run_batch_async(
        self,
        items: List[Dict[str, Any]],
        output_style: OutputStyle,
    ) -> None:
        """Async wrapper — runs :meth:`run_batch` in a thread pool.

        Suitable for use with ``main.py`` semaphore-based signal system.
        """
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.run_batch, items, output_style)

    # ── Internal ───────────────────────────────────────────────────

    def _process_one(
        self,
        item: Dict[str, Any],
        output_style: OutputStyle,
    ) -> bool:
        """Download HTML, parse to Markdown, save.  Returns True on success."""
        url = item.get("url", "")
        if not url:
            return False

        html = self._fetch(url)
        if html is None:
            return False

        markdown: Optional[str] = None

        if self.image_processor:
            markdown = self.parser.parse_with_images(
                html, url, self.image_processor, str(item.get("id", ""))
            )
        else:
            markdown = self.parser.parse(html, url)

        if markdown:
            self._save(item, markdown, output_style)
            return True
        else:
            print(f"[Grabber] No content extracted: {url}")
            return False

    def _fetch(self, url: str) -> Optional[str]:
        """HTTP GET with rate limiting.  Returns HTML text or None."""
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            # Fix encoding when server omits charset in Content-Type
            if resp.encoding == "ISO-8859-1" and resp.apparent_encoding == "utf-8":
                resp.encoding = resp.apparent_encoding
            return resp.text
        except requests.RequestException as e:
            print(f"[Grabber] HTTP error for {url}: {e}")
            return None

    def _save(
        self,
        item: Dict[str, Any],
        markdown: str,
        output_style: OutputStyle,
    ) -> None:
        """Dispatch save to the appropriate backend."""
        match output_style:
            case OutputStyle.MARKDOWN:
                self._save_to_file(item, markdown, ".md")
            case OutputStyle.HTML:
                self._save_to_file(item, markdown, ".html")
            case OutputStyle.SQLITE:
                self._save_to_db(item, markdown, "sqlite")
            case OutputStyle.POSTGRESQL:
                self._save_to_db(item, markdown, "postgresql")

    # ── File output ────────────────────────────────────────────────

    def _save_to_file(
        self,
        item: Dict[str, Any],
        content: str,
        ext: str,
    ) -> None:
        """Write content to a local file."""
        title = item.get("title", "untitled")[:50]
        source_id = item.get("source_id", "unknown")
        date = item.get("date", "")

        # Sanitize filename
        safe_title = "".join(
            c for c in title if c.isalnum() or c in (" ", "_", "-")
        ).rstrip()

        parts = [self.data_dir]
        if date:
            parts.append(date)
        parts.append(source_id)
        out_dir = Path(*parts)
        out_dir.mkdir(parents=True, exist_ok=True)

        out_path = out_dir / f"{safe_title}{ext}"
        out_path.write_text(content, encoding="utf-8")
        print(f"[Grabber] Saved: {out_path}")

    # ── DB output ──────────────────────────────────────────────────

    def _save_to_db(
        self,
        item: Dict[str, Any],
        markdown: str,
        backend: str,
    ) -> None:
        """Set content on the item dict for DB-bound output.

        For SQLITE/POSTGRESQL modes the content is written back to the
        item dict rather than directly to the database — the caller's
        storage layer handles the final INSERT/UPDATE with content.
        """
        item["content"] = markdown
