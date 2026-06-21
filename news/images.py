# coding=utf-8
"""Image download and storage processor.

Downloads article images concurrently via thread pool, saves them
through a :class:`FileStorage` backend, and returns relative paths
suitable for embedding in Markdown content.

Usage::

    from storage.files import LocalStorage

    ip = ImageProcessor(max_workers=8)
    storage = LocalStorage("output")

    url_map = {
        "https://x.com/a.jpg": "news/2026-06-15/images/a.jpg",
        "https://x.com/b.jpg": "news/2026-06-15/images/b.jpg",
    }
    result = ip.download(url_map, storage=storage)
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

    Callers pre-compute the full S3 key for each URL and pass it as
    ``{url: "news/YYYY-MM-DD/images/xxx.jpg"}``.  The processor downloads
    each image and saves it directly to the given key.

    Does NOT modify Markdown — callers handle string replacement
    using the returned ``{url: "images/xxx.jpg"}`` mapping.

    Usage::

        from storage.files import LocalStorage

        ip = ImageProcessor(max_workers=8)
        storage = LocalStorage("output")

        url_map = {
            "https://x.com/a.jpg": "news/2026-06-15/images/a.jpg",
        }
        result = ip.download(url_map, storage=storage)
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
        self, url_map: Dict[str, str], storage: FileStorage,
    ) -> Dict[str, str]:
        """Download images and save to pre-computed paths.

        Args:
            url_map: ``{url: "news/YYYY-MM-DD/images/xxx.jpg", ...}``.
                Each value is the full S3 key where the image will be stored.
            storage: :class:`FileStorage` backend for saving images.

        Returns:
            ``{url: "images/xxx.jpg", ...}`` — each URL maps to the
            relative path (for Markdown content replacement) on success,
            or ``""`` on failure.
        """
        if not url_map:
            return {}

        result: Dict[str, str] = {url: "" for url in url_map}
        print(f"[ImageProcessor] Downloading {len(result)} unique images "
              f"(workers={self._max_workers})")
        executor = self._get_executor()
        futures = {
            executor.submit(
                self._download_and_save, url, target_path, storage,
            ): url
            for url, target_path in url_map.items()
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
    ) -> Optional[str]:
        """Download *url* and save directly to *target_path* (full S3 key).

        HTTP GET uses exponential backoff (3 attempts).  Save also retries
        up to 3 times for transient storage errors.

        Returns ``"images/xxx.jpg"`` (relative path for content
        replacement) on success, or ``None`` on failure.
        """
        # Phase 1: HTTP download with retry
        resp, error = http_get_with_retry(
            self.session, url, timeout=30, label=url
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
        parsed = urlparse(url)
        path = unquote(parsed.path)
        name = path.rsplit("/", 1)[-1] if "/" in path else path

        # Keep only safe characters
        name = re.sub(r"[^\w.\-]", "_", name)

        if not name or "." not in name:
            return f"image{default_ext}"

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
