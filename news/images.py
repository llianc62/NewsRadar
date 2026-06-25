# coding=utf-8
"""Image download and storage processor.

Downloads article images concurrently via thread pool, saves them
through a :class:`FileStorage` backend, and returns relative paths
suitable for embedding in Markdown content.

Usage::

    from storage.files import LocalStorage

    ip = ImageProcessor(max_workers=8)
    storage = LocalStorage("output")

    tasks = {
        "https://x.com/a.jpg": {
            "target_dir": "news/2026-06-15/images",
            "article_url": "https://x.com/post/123",
        },
        "https://x.com/b.jpg": {
            "target_dir": "news/2026-06-15/images",
            "article_url": "https://x.com/post/123",
        },
    }
    result = ip.download(tasks, storage=storage)
    # → {"https://x.com/a.jpg": "images/a.jpg", ...}
"""

import re
import time

import requests

from typing import Dict, Optional
from urllib.parse import unquote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils import http_get_with_retry

from storage import FileStorage


class ImageProcessor:
    """Download article images, store via :class:`FileStorage`,
    return mapping from original URL → relative path.

    Callers pre-compute the full S3 key for each URL and pass a
    ``{url: {"target_dir": str, "article_url": str}}`` dict.  The
    processor downloads each image and saves it to the given key.
    *article_url* is sent as the ``Referer`` header to avoid CDN
    hotlinking 403 errors.

    Does NOT modify Markdown — callers handle string replacement
    using the returned ``{url: "images/xxx.jpg"}`` mapping.

    Usage::

        from storage.files import LocalStorage

        ip = ImageProcessor(max_workers=8)
        storage = LocalStorage("output")

        tasks = {
            "https://x.com/a.jpg": {
                "target_dir": "news/2026-06-15/images",
                "article_url": "https://x.com/post/123",
            },
        }
        result = ip.download(tasks, storage=storage)
        # → {"https://x.com/a.jpg": "images/a.jpg"}
    """

    # Content-Type → file extension mapping
    EXT_MAP: Dict[str, str] = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
    }

    def __init__(
        self,
        max_workers: int = 8,
    ) -> None:
        self._max_workers = max_workers
        self._session: Optional[requests.Session] = None
        self._executor: Optional[ThreadPoolExecutor] = None

    # ── Session ────────────────────────────────────────────────────

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/91.0.4472.124 Safari/537.36"
                ),
            })

        return self._session

    def _get_executor(self) -> ThreadPoolExecutor:
        """Lazy-init the thread pool for parallel image downloads."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self._max_workers)
        return self._executor

    # ── Public API ─────────────────────────────────────────────────

    def download(
        self,
        tasks: Dict[str, dict],
        storage: FileStorage,
    ) -> Dict[str, str]:
        """Download images and save to pre-computed paths.

        Args:
            tasks: ``{url: {"target_dir": str, "article_url": str}, ...}``.
                *target_dir* is the full S3 key (e.g.
                ``"news/YYYY-MM-DD/images"``).
                *article_url* is the article page URL, sent as the
                ``Referer`` header to avoid CDN hotlinking 403 errors.
            storage: :class:`FileStorage` backend for saving images.

        Returns:
            ``{url: "images/xxx.jpg", ...}`` — each URL maps to the
            relative path (for Markdown content replacement) on success,
            or ``""`` on failure.
        """
        if not tasks:
            return {}

        result: Dict[str, str] = {url: "" for url in tasks}
        print(f"[ImageProcessor] Downloading {len(result)} unique images "
              f"(workers={self._max_workers})")
        executor = self._get_executor()
        futures = {
            executor.submit(
                self._download_and_save, url, t["target_dir"], storage,
                t.get("article_url") or None,
            ): url
            for url, t in tasks.items()
        }

        for future in as_completed(futures):
            url = futures[future]
            try:
                saved_path = future.result()
                if saved_path:
                    result[url] = saved_path
            except Exception as e:
                print(f"[ImageProcessor] Download failed [{url}]: {e}")

        success = sum(1 for v in result.values() if v)
        print(f"[ImageProcessor] Downloaded {success}/{len(result)} images")
        return result

    # ── Helpers ────────────────────────────────────────────────────

    def _download_and_save(
        self,
        url: str,
        target_path: str,
        storage: FileStorage,
        referer: Optional[str] = None,
    ) -> Optional[str]:
        """Download *url* and save directly to *target_path* (full S3 key).

        HTTP GET uses exponential backoff (3 attempts).  Save also retries
        up to 3 times for transient storage errors.

        Args:
            referer: Optional Referer header value (the article URL the
                image came from) to avoid CDN hotlinking 403 errors.

        Returns ``"images/xxx.jpg"`` (relative path for content
        replacement) on success, or ``None`` on failure.
        """
        # Phase 1: HTTP download with retry
        extra_headers: Dict[str, str] = {}
        if referer:
            extra_headers["Referer"] = referer

        resp, error = http_get_with_retry(
            self.session, url, timeout=30, label=url,
            headers=extra_headers if extra_headers else None,
        )
        if resp is None:
            print(f"[ImageProcessor] HTTP error for {url}: {error}")
            return None

        content_type = (
            resp.headers.get("Content-Type", "image/jpeg")
            .split(";")[0]
            .strip()
        )
        image_data = resp.content

        # Phase 2: Save with retry (MinIO may be temporarily unavailable)
        ext = self.EXT_MAP.get(content_type, ".jpg")
        filename = self._extract_filename(url, ext)
        file_path = f"{target_path}/{filename}"

        for attempt in range(1, 4):  # 3 attempts
            try:
                storage.save(image_data, file_path, content_type)
                return f"images/{filename}"
            except Exception as e:
                if attempt == 3:
                    print(f"[ImageProcessor] Save failed [{url}]: {e}")
                    return None
                time.sleep(2 ** attempt)

    def _extract_filename(self, url: str, default_ext: str) -> str:
        """Extract original filename from image *url*, falling back to
        a generated name if the URL path doesn't contain a usable filename.
        """
        import hashlib

        parsed = urlparse(url)
        path = unquote(parsed.path)
        name = path.rsplit("/", 1)[-1] if "/" in path else path

        # Keep only safe characters
        name = re.sub(r"[^\w.\-]", "_", name)

        if not name:
            url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
            return f"{url_hash}{default_ext}"

        if "." not in name:
            # Path has a unique identifier but no file extension
            # (e.g. 36kr's v2_<uuid>_img_000 URLs). Keep the
            # identifier so multiple such images don't collide.
            return f"{name}{default_ext}"

        # Ensure extension matches content type if possible
        root, ext = name.rsplit(".", 1)
        ext = f".{ext.lower()}"
        if ext not in self.EXT_MAP.values():
            ext = default_ext

        return f"{root}{ext}"

    def close(self) -> None:
        """Shut down the thread pool and close the HTTP session."""
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
        if self._session is not None:
            self._session.close()
            self._session = None
