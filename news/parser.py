# coding=utf-8
"""HTML content parser with image processing support.

Provides two classes:

* ``HtmlParser`` — HTML → Markdown conversion (trafilatura + fallback).
* ``ImageProcessor`` — download images, store locally or upload to MinIO,
  return reference paths for backfilling Markdown.

Reference: https://github.com/microsoft/markitdown
"""

import html as _html
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from urllib.parse import urljoin, urlparse

import requests


# ═══════════════════════════════════════════════════════════════════
# HtmlParser
# ═══════════════════════════════════════════════════════════════════


class HtmlParser:
    """Convert HTML to Markdown via trafilatura with a regex fallback.

    Pure parser — no database dependency, no network I/O (image
    processing is delegated to :class:`ImageProcessor`).

    Usage::

        parser = HtmlParser(config)
        markdown = parser.parse(html_text, url="https://example.com")
        markdown = parser.parse_with_images(html, url, image_processor)
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        crawler_cfg = cfg.get("crawler", {})
        self.max_content_length = crawler_cfg.get("max_content_length", 100000)

        self._has_trafilatura = False
        try:
            import trafilatura  # noqa: F401
            self._has_trafilatura = True
        except ImportError:
            pass

    # ── Public API ─────────────────────────────────────────────────

    def parse(self, html: str, url: str = "") -> Optional[str]:
        """Extract Markdown content from HTML.

        Uses trafilatura when available, falling back to HTML-stripping.

        Args:
            html: Raw HTML text.
            url: Source URL (passed to trafilatura for metadata).

        Returns:
            Markdown string, or None if extraction produced nothing useful.
        """
        markdown = None

        if self._has_trafilatura:
            markdown = self._extract_with_trafilatura(html, url)

        if markdown is None:
            markdown = self._fallback(html)

        if markdown is None:
            markdown = self._extract_spa_data(html, url)

        if markdown and len(markdown) > self.max_content_length:
            markdown = markdown[:self.max_content_length] + "\n\n... (truncated)"

        return markdown

    def parse_with_images(
        self,
        html: str,
        url: str,
        image_processor: "ImageProcessor",
        article_id: str = "",
    ) -> Optional[str]:
        """Parse HTML and process images through *image_processor*.

        Extracts ``<img>`` tags from *html*, downloads and stores images
        via *image_processor*, then backfills the Markdown output with
        the resolved image references.

        Args:
            html: Raw HTML text.
            url: Source URL (for resolving relative image URLs).
            image_processor: Configured :class:`ImageProcessor` instance.
            article_id: Optional article ID for file naming.

        Returns:
            Markdown with image URLs replaced by local paths / MinIO URLs.
        """
        markdown = self.parse(html, url)
        if markdown is None:
            return None

        return image_processor.process(html, markdown, url, article_id)

    # ── trafilatura path ───────────────────────────────────────────

    def _extract_with_trafilatura(self, html: str, url: str) -> Optional[str]:
        """Use trafilatura for content extraction."""
        import trafilatura

        result = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            with_metadata=True,
            include_tables=True,
            include_images=True,
            include_links=True,
            include_formatting=True,
        )
        if result and len(result.strip()) > 50:
            return result.strip()
        return None

    # ── Fallback: HTML strip ───────────────────────────────────────

    def _fallback(self, html_text: str) -> Optional[str]:
        """Strip HTML tags, collapse whitespace — used when trafilatura
        is unavailable or fails to extract meaningful content."""
        text = re.sub(
            r'<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>',
            '',
            html_text,
            flags=re.DOTALL | re.IGNORECASE,
        )

        text = re.sub(r'<[^>]+>', ' ', text)
        text = _html.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()

        paragraphs = [p.strip() for p in text.split('\n') if len(p.strip()) > 80]
        if paragraphs:
            text = '\n\n'.join(paragraphs)

        if len(text) > 100:
            return text
        return None

    # ── SPA data extraction ────────────────────────────────────────

    def _extract_spa_data(self, html_text: str, url: str = "") -> Optional[str]:
        """Extract content from SPA embedded JSON when DOM-based
        extraction fails (e.g. wallstreetcn.com, Next.js sites).

        Tries known SPA data patterns in order.  When JSON is found, the
        data tree is searched recursively for an object with both
        ``title`` and ``content`` keys — the content is then converted to
        Markdown via trafilatura (or HTML-stripped as fallback).

        Returns Markdown string, or None if no SPA data was found.
        """
        candidates = self._find_json_candidates(html_text)
        for data in candidates:
            article = self._find_article_in_json(data)
            if article is None:
                continue
            content = article.get("content", "")
            if not content or not isinstance(content, str) or len(content) < 50:
                continue
            # content is HTML — convert to Markdown
            markdown = None
            if self._has_trafilatura:
                markdown = self._extract_with_trafilatura(content, url)
            if markdown is None:
                markdown = self._fallback(content)
            if markdown and len(markdown.strip()) > 50:
                return markdown
        return None

    def _find_json_candidates(self, html_text: str):
        """Yield parsed JSON objects from known SPA embedding patterns.

        Patterns tried:
        1. ``__SSR__ = {...}`` (Vite SSR, e.g. wallstreetcn.com)
        2. ``__NEXT_DATA__ = {...}`` (Next.js)
        3. ``__NUXT__ = {...}`` (Nuxt)
        4. ``<script type="application/ld+json">`` (JSON-LD / Schema.org)
        """
        # 1. Vite SSR: __SSR__ = {...}
        for data in self._extract_bracketed_json(html_text, r'__SSR__\s*=\s*(\{)'):
            if data:
                yield data

        # 2. Next.js: __NEXT_DATA__ = {...}
        for data in self._extract_bracketed_json(html_text, r'__NEXT_DATA__\s*=\s*(\{)'):
            if data:
                yield data

        # 3. Nuxt: __NUXT__ = {...}
        for data in self._extract_bracketed_json(html_text, r'__NUXT__\s*=\s*(\{)'):
            if data:
                yield data

        # 4. JSON-LD
        for data in self._extract_json_ld(html_text):
            if data:
                yield data

    @staticmethod
    def _extract_bracketed_json(html_text: str, pattern: str):
        """Find ``pattern`` in *html_text*, then bracket-match to get the
        full JSON string, parse, and return as a list (may be empty).

        The *pattern* must capture the position of the opening ``{``.
        """
        match = re.search(pattern, html_text)
        if not match:
            return []

        start = match.start(1)
        depth = 0
        end = start
        for i in range(start, len(html_text)):
            ch = html_text[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        if end <= start:
            return []

        try:
            return [json.loads(html_text[start:end])]
        except (json.JSONDecodeError, ValueError):
            return []

    @staticmethod
    def _extract_json_ld(html_text: str):
        """Extract articles from ``<script type="application/ld+json">`` tags.

        Returns a list of parsed JSON-LD objects that *might* be articles.
        """
        pattern = re.compile(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            re.DOTALL | re.IGNORECASE,
        )
        results = []
        for match in pattern.finditer(html_text):
            try:
                data = json.loads(match.group(1).strip())
                results.append(data)
            except (json.JSONDecodeError, ValueError):
                continue
        return results

    @staticmethod
    def _find_article_in_json(data):
        """Recursively search *data* for an object that has both a
        ``title`` (str) and ``content`` (str) key.

        If multiple candidates exist, the one with the longest ``content``
        is returned.  For JSON-LD objects, also accepts
        ``@type: Article/NewsArticle`` with ``headline``+``articleBody``.

        Returns the article dict, or None.
        """
        best = None
        best_len = 0

        def _search(obj):
            nonlocal best, best_len
            if isinstance(obj, dict):
                # JSON-LD style: @type Article + headline + articleBody
                obj_type = obj.get("@type", "")
                if obj_type in ("Article", "NewsArticle"):
                    headline = obj.get("headline", "")
                    body = obj.get("articleBody", "")
                    if isinstance(headline, str) and isinstance(body, str):
                        content_len = len(body)
                        if content_len > best_len:
                            best_len = content_len
                            best = {"title": headline, "content": body}

                # Generic SPA embedded article: title + content
                title = obj.get("title")
                content = obj.get("content")
                if (isinstance(title, str) and isinstance(content, str)
                        and len(title) > 0):
                    content_len = len(content)
                    if content_len > best_len:
                        best_len = content_len
                        best = {"title": title, "content": content}

                # Recurse into nested objects
                for v in obj.values():
                    _search(v)

            elif isinstance(obj, list):
                for item in obj:
                    _search(item)

        _search(data)
        return best


# ═══════════════════════════════════════════════════════════════════
# ImageProcessor
# ═══════════════════════════════════════════════════════════════════


class ImageProcessor:
    """Download article images, store locally or upload to MinIO,
    return resolved references for backfilling Markdown.

    Supports two storage backends:

    * ``"local"`` — save to ``{output_dir}/images/{date}/{article_id}/``
    * ``"minio"`` — upload to MinIO and return object URLs

    Usage::

        ip = ImageProcessor(storage_backend="local", config=config)
        updated_md = ip.process(html, markdown, base_url="...", article_id="42")
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
        storage_backend: Literal["local", "minio"] = "local",
        config: Optional[Dict[str, Any]] = None,
    ):
        self.storage_backend = storage_backend
        cfg = config or {}

        storage_cfg = cfg.get("storage", {})
        self.data_dir = storage_cfg.get("local", {}).get("data_dir", "output")

        self._minio_config = cfg.get("minio", {})
        self._minio_storage = None  # Lazy init

        self._session: Optional[requests.Session] = None

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

    # ── Public API ─────────────────────────────────────────────────

    def process(
        self,
        html: str,
        markdown: str,
        base_url: str = "",
        article_id: str = "",
    ) -> str:
        """Extract ``<img>`` tags from *html*, download images, store,
        and replace image URLs in *markdown* with resolved references.

        Args:
            html: Original HTML (used to discover ``<img>`` tags).
            markdown: Parsed Markdown (image URLs are replaced in-place).
            base_url: Source URL for resolving relative image URLs.
            article_id: Article identifier for directory / object naming.

        Returns:
            Markdown with image URLs replaced.
        """
        img_pattern = re.compile(
            r'<img[^>]+src=["\']([^"\']+)["\']'
            r'(?:[^>]+alt=["\']([^"\']*)["\'])?',
            re.IGNORECASE,
        )

        img_index = 0
        for match in img_pattern.finditer(html):
            src = match.group(1)

            # Resolve relative URLs
            img_url = self._resolve_url(src, base_url)
            if img_url is None:
                continue

            try:
                image_data, content_type = self.download(img_url)
                if image_data is None:
                    continue
            except Exception as e:
                print(f"[ImageProcessor] Download failed [{img_url}]: {e}")
                continue

            ext = self.EXT_MAP.get(content_type, ".jpg")
            filename = f"img_{img_index:02d}{ext}"

            try:
                resolved = self.save(image_data, filename, content_type, article_id)
                if resolved:
                    # Replace original src in Markdown
                    markdown = markdown.replace(src, resolved)
                    markdown = markdown.replace(img_url, resolved)
            except Exception as e:
                print(f"[ImageProcessor] Save failed [{img_url}]: {e}")
                continue

            img_index += 1

        return markdown

    def download(self, url: str) -> Optional[tuple]:
        """Download an image from *url*.

        Returns:
            (image_data: bytes, content_type: str) tuple, or None on failure.
        """
        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            content_type = (
                resp.headers.get("Content-Type", "image/jpeg")
                .split(";")[0]
                .strip()
            )
            return resp.content, content_type
        except requests.RequestException as e:
            print(f"[ImageProcessor] HTTP error for {url}: {e}")
            return None

    def save(
        self,
        data: bytes,
        filename: str,
        content_type: str,
        article_id: str = "",
    ) -> Optional[str]:
        """Store image and return its access path / URL.

        Args:
            data: Image binary data.
            filename: Desired filename (e.g. ``img_00.jpg``).
            content_type: MIME type.
            article_id: Article identifier for directory nesting.

        Returns:
            Local file path (local backend) or MinIO URL (minio backend).
        """
        if self.storage_backend == "minio":
            return self._save_to_minio(data, filename, content_type, article_id)
        else:
            return self._save_to_local(data, filename, article_id)

    # ── Local storage ──────────────────────────────────────────────

    def _save_to_local(
        self,
        data: bytes,
        filename: str,
        article_id: str = "",
    ) -> str:
        """Save image to ``{data_dir}/images/{YYYY-MM}/{article_id}/``."""
        date_prefix = datetime.now().strftime("%Y-%m")
        out_dir = Path(self.data_dir) / "images" / date_prefix
        if article_id:
            out_dir = out_dir / str(article_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        out_path = out_dir / filename
        out_path.write_bytes(data)
        return str(out_path)

    # ── MinIO storage ──────────────────────────────────────────────

    def _save_to_minio(
        self,
        data: bytes,
        filename: str,
        content_type: str,
        article_id: str = "",
    ) -> Optional[str]:
        """Upload image to MinIO and return the object URL."""
        if self._minio_storage is None:
            from storage.minio import ImageStorage

            if not self._minio_config:
                print("[ImageProcessor] MinIO config missing — falling back to local")
                return self._save_to_local(data, filename, article_id)

            self._minio_storage = ImageStorage(
                endpoint_url=self._minio_config.get("endpoint_url", ""),
                access_key=self._minio_config.get("access_key_id", ""),
                secret_key=self._minio_config.get("secret_access_key", ""),
                bucket_name=self._minio_config.get("bucket_name", ""),
                region=self._minio_config.get("region") or None,
            )

        date_prefix = datetime.now().strftime("%Y-%m")
        if article_id:
            object_key = f"{date_prefix}/{article_id}/{filename}"
        else:
            object_key = f"{date_prefix}/{filename}"

        with tempfile.NamedTemporaryFile(
            suffix=Path(filename).suffix, delete=False
        ) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)

        try:
            url = self._minio_storage.upload_image(tmp_path, object_key, content_type)
            return url
        except Exception as e:
            print(f"[ImageProcessor] MinIO upload failed: {e}")
            return None
        finally:
            tmp_path.unlink(missing_ok=True)

    # ── Helpers ────────────────────────────────────────────────────

    def _resolve_url(self, src: str, base_url: str) -> Optional[str]:
        """Resolve a potentially-relative image URL against *base_url*.

        Returns None for data URIs or URLs without a network location.
        """
        if not src or src.startswith("data:"):
            return None

        parsed = urlparse(src)
        if not parsed.netloc:
            if not base_url:
                return None
            return urljoin(base_url, src)

        return src
