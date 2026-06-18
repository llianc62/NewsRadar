# coding=utf-8
"""Image download and storage processor.

Downloads article images concurrently via thread pool, saves them
through a :class:`FileStorage` backend, and returns clean relative
paths suitable for embedding in Markdown content.

Usage::

    from storage.files import LocalStorage

    ip = ImageProcessor(max_workers=8)
    storage = LocalStorage("output")

    # Single download
    result = ip.download("https://x.com/a.jpg", target_storage=storage)
    # → {"https://x.com/a.jpg": "images/a.jpg"}

    # Batch download (parallel)
    url_map = {"https://x.com/a.jpg": "", "https://x.com/b.jpg": ""}
    url_map = ip.download(*url_map.keys(), target_storage=storage)
    # → {"https://x.com/a.jpg": "images/a.jpg", "https://x.com/b.jpg": "images/b.jpg"}
"""

import re
import requests

from typing import Dict, Optional
from urllib.parse import unquote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from storage import FileStorage


class ImageProcessor:
    """Download article images, store via :class:`FileStorage`,
    return mapping from original URL → stored path.

    Does NOT modify Markdown — callers handle string replacement
    using the returned mapping dicts.

    Usage::

        from storage.files import LocalStorage

        ip = ImageProcessor(max_workers=8)
        storage = LocalStorage("output")

        # Single download
        result = ip.download("https://x.com/a.jpg", target_storage=storage)
        # → {"https://x.com/a.jpg": "images/a.jpg"}

        # Batch download (parallel)
        url_map = {"https://x.com/a.jpg": "", "https://x.com/b.jpg": ""}
        url_map = ip.download(*url_map.keys(), target_storage=storage)
        # → {"https://x.com/a.jpg": "images/a.jpg", "https://x.com/b.jpg": "images/b.jpg"}
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

    def download(self, *urls: str, storage: FileStorage) -> Dict[str, str]:
        """Download images from *urls*, save via *target_storage*.

        Downloads are parallelised across ``max_workers`` threads.
        Duplicate URLs are downloaded only once.

        Args:
            *urls: One or more image URLs to download.
            target_storage: :class:`FileStorage` backend for saving images.

        Returns:
            ``{url: saved_path, ...}`` — each URL maps to the stored path
            on success, or ``""`` on failure.
        """
        if not urls:
            return {}

        result: Dict[str, str] = {url: "" for url in urls}
        target_dir = self._images_dir()
        print(f"[ImageProcessor] Downloading {len(result)} unique images "
              f"(workers={self._max_workers})")
        executor = self._get_executor()
        futures = {
            executor.submit(self._download_and_save, url, target_dir, storage): url
            for url in result
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

    @staticmethod
    def _images_dir() -> str:
        """Derive today's image directory: ``news/YYYY-MM-DD/images``."""
        from utils import format_date_folder
        return f"news/{format_date_folder()}/images"

    def _download_and_save(
        self,
        url: str,
        target_dir: str,
        storage: FileStorage,
    ) -> Optional[str]:
        """Download a single image and save via *target_storage*.

        Used as a future in the thread pool inside :meth:`download`.

        Returns the saved path on success, or None on failure.
        """
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            content_type = (
                resp.headers.get("Content-Type", "image/jpeg")
                .split(";")[0]
                .strip()
            )
            image_data = resp.content
        except requests.RequestException as e:
            print(f"[ImageProcessor] HTTP error for {url}: {e}")
            return None

        ext = self.EXT_MAP.get(content_type, ".jpg")
        filename = self._extract_filename(url, ext)
        target_path = f"{target_dir}/{filename}"
        try:
            storage.save(image_data, target_path, content_type)
            return f"images/{filename}"
        except Exception as e:
            print(f"[ImageProcessor] Save failed [{url}]: {e}")
            return None

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
