# coding=utf-8
"""HTML content parser with image processing support.

Provides two classes:

* ``HtmlParser`` — HTML → Markdown conversion (trafilatura + fallback).
* ``ImageProcessor`` — download images, store locally or upload to MinIO,
  return reference paths for backfilling Markdown.

Reference: https://github.com/microsoft/markitdown
"""

import re
import json
import requests

import html as _html
from urllib.parse import unquote, urlparse

from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed



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
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = config or {}
        cfg = self._config
        crawler_cfg = cfg.get("crawler", {})
        self.max_content_length = crawler_cfg.get("max_content_length", 100000)

        self._has_trafilatura = False
        try:
            import trafilatura  # noqa: F401
            self._has_trafilatura = True
        except ImportError:
            pass

    # ── Public API ─────────────────────────────────────────────────

    def parse(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        """Extract Markdown + metadata from HTML.

        Uses trafilatura when available, falling back to HTML-stripping.
        Does **not** download or process images — callers should use
        :class:`ImageProcessor` separately when image handling is needed.

        Args:
            html: Raw HTML text.
            url: Source URL (passed to trafilatura for metadata).

        Returns:
            Dict with keys ``markdown``, ``title``, ``author``,
            ``published_at``, ``summary``, ``category``, ``tags``,
            or None if extraction produced nothing useful.
        """
        result = None

        if self._has_trafilatura:
            result = self._extract_with_trafilatura(html, url)

        if result is None:
            result = self._fallback(html, url)

        if result is None:
            result = self._extract_spa_data(html, url)

        if result is not None:
            md = result.get("markdown", "")
            if md and len(md) > self.max_content_length:
                result["markdown"] = md[:self.max_content_length] + "\n\n... (truncated)"

        return result

    # ── trafilatura path ───────────────────────────────────────────

    def _extract_with_trafilatura(self, html: str, url: str) -> Optional[Dict[str, Any]]:
        """Use trafilatura for content + metadata extraction."""
        import trafilatura

        # 正文提取（Markdown，不嵌入 metadata）
        markdown = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_tables=True,
            include_images=True,
            include_links=True,
            include_formatting=True,
        )
        if not markdown or len(markdown.strip()) <= 50:
            return None

        # 元数据提取（轻量，只解析 head/meta/JSON-LD）
        try:
            doc = trafilatura.extract_metadata(html, default_url=url)
        except Exception:
            doc = None

        return self._build_result(markdown=markdown.strip(), doc=doc)

    # ── Fallback: HTML strip ───────────────────────────────────────

    def _fallback(self, html_text: str, url: str = "") -> Optional[Dict[str, Any]]:
        """Strip HTML tags, collapse whitespace — used when trafilatura
        is unavailable or fails to extract meaningful content.

        Also extracts title from ``<title>`` tag and metadata from
        ``<meta>`` tags (author, description, published_time).
        """
        # Extract title from <title> tag
        title = ""
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.DOTALL | re.IGNORECASE)
        if title_match:
            title = _html.unescape(title_match.group(1).strip())

        # Extract metadata from <meta> tags
        author = self._extract_meta(html_text, r'name=["\']author["\']')
        summary = (
            self._extract_meta(html_text, r'name=["\']description["\']')
            or self._extract_meta(html_text, r'property=["\']og:description["\']')
        )
        published_at = self._extract_meta(
            html_text, r'property=["\']article:published_time["\']'
        )

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
            return self._build_result(
                markdown=text,
                title=title,
                author=author,
                published_at=published_at,
                summary=summary,
            )
        return None

    @staticmethod
    def _extract_meta(html_text: str, attr_pattern: str) -> str:
        """Extract ``content`` attribute from a ``<meta>`` tag matching
        *attr_pattern*."""
        pattern = re.compile(
            r'<meta[^>]*' + attr_pattern + r'[^>]*content=["\']([^"\']+)["\']',
            re.IGNORECASE,
        )
        match = pattern.search(html_text)
        return match.group(1).strip() if match else ""

    # ── Unified result builder ──────────────────────────────────────

    @staticmethod
    def _build_result(
        markdown: str,
        doc: Any = None,
        title: str = "",
        author: str = "",
        published_at: str = "",
        summary: str = "",
        category: str = "",
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Combine markdown with metadata into a unified result dict.

        Args:
            markdown: Extracted Markdown text (required).
            doc: Optional trafilatura ``Document`` — metadata is read
                from its fields when provided.
            title, author, published_at, summary, category, tags:
                Explicit overrides, taking precedence over *doc* fields.

        Returns:
            Dict with keys ``markdown``, ``title``, ``author``,
            ``published_at``, ``summary``, ``category``, ``tags``.
        """
        result: Dict[str, Any] = {
            "markdown": markdown,
            "title": title,
            "author": author,
            "published_at": published_at,
            "summary": summary,
            "category": category,
            "tags": tags or [],
        }
        if doc is not None:
            if doc.title:
                result["title"] = doc.title
            if doc.author:
                result["author"] = doc.author
            if doc.date:
                result["published_at"] = doc.date
            if doc.description:
                result["summary"] = doc.description
            # categories → 取首个作为主分类，其余合并到 tags
            if doc.categories:
                result["category"] = doc.categories[0]
                if len(doc.categories) > 1:
                    result["tags"] = list(set(result["tags"] + doc.categories[1:]))
            if doc.tags:
                result["tags"] = list(set(result["tags"] + doc.tags))
        # 显式传入的字段优先（覆盖 doc）
        if title:
            result["title"] = title
        if author:
            result["author"] = author
        if published_at:
            result["published_at"] = published_at
        if summary:
            result["summary"] = summary
        if category:
            result["category"] = category
        if tags:
            result["tags"] = list(set(result["tags"] + tags))
        return result

    # ── SPA data extraction ────────────────────────────────────────

    def _extract_spa_data(self, html_text: str, url: str = "") -> Optional[Dict[str, Any]]:
        """Extract content from SPA embedded JSON when DOM-based
        extraction fails (e.g. wallstreetcn.com, Next.js sites).

        Tries known SPA data patterns in order.  When JSON is found, the
        data tree is searched recursively for an object with both
        ``title`` and ``content`` keys — the content is then converted to
        Markdown via trafilatura (or HTML-stripped as fallback).

        Returns result dict, or None if no SPA data was found.
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
                extracted = self._extract_with_trafilatura(content, url)
                if extracted is not None:
                    markdown = extracted["markdown"]
            if markdown is None:
                markdown = self._fallback(content, url)
                if markdown is not None:
                    markdown = markdown["markdown"]
            if markdown and len(markdown.strip()) > 50:
                # 从 SPA JSON / JSON-LD 中提取元数据
                title = article.get("title") or article.get("headline", "")
                author_val = ""
                pub_at = article.get("datePublished") or article.get("date", "")
                summary_val = article.get("description") or article.get("abstract", "")
                category_val = ""
                tags_val: List[str] = []
                # JSON-LD keywords
                keywords = article.get("keywords")
                if isinstance(keywords, str):
                    tags_val = [k.strip() for k in keywords.split(",") if k.strip()]
                elif isinstance(keywords, list):
                    tags_val = [str(k) for k in keywords if k]
                # JSON-LD articleSection
                section = article.get("articleSection")
                if isinstance(section, str) and section:
                    category_val = section

                return self._build_result(
                    markdown=markdown.strip(),
                    title=title,
                    author=author_val,
                    published_at=pub_at,
                    summary=summary_val,
                    category=category_val,
                    tags=tags_val,
                )
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

        Returns a dict with keys ``title``, ``content``, ``author``,
        ``datePublished``, ``description``, ``keywords``, ``articleSection``,
        or None.
        """

        def _get_author(obj: dict) -> str:
            """Extract author name from a JSON-LD object."""
            author = obj.get("author", "")
            if isinstance(author, str):
                return author
            if isinstance(author, dict):
                return author.get("name", "")
            if isinstance(author, list):
                names = []
                for a in author:
                    if isinstance(a, str):
                        names.append(a)
                    elif isinstance(a, dict):
                        n = a.get("name", "")
                        if n:
                            names.append(n)
                return ", ".join(names)
            return ""

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
                            best = {
                                "title": headline,
                                "content": body,
                                "author": _get_author(obj),
                                "datePublished": obj.get("datePublished", ""),
                                "description": obj.get("description", ""),
                                "keywords": obj.get("keywords", []),
                                "articleSection": obj.get("articleSection", ""),
                            }

                # Generic SPA embedded article: title + content
                title = obj.get("title")
                content = obj.get("content")
                if (isinstance(title, str) and isinstance(content, str)
                        and len(title) > 0):
                    content_len = len(content)
                    if content_len > best_len:
                        best_len = content_len
                        best = {
                            "title": title,
                            "content": content,
                            "author": obj.get("author", ""),
                            "datePublished": obj.get("datePublished")
                                or obj.get("date", ""),
                            "description": obj.get("description", ""),
                            "keywords": obj.get("keywords", []),
                            "articleSection": obj.get("articleSection", ""),
                        }

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
    """Download article images, store via :class:`FileStorage`,
    return mapping from original URL → stored path.

    Does NOT modify Markdown — callers handle string replacement
    using the returned mapping dicts.

    Usage::

        from storage import create_storage

        ip = ImageProcessor(storage=create_storage(config), max_workers=8)

        # Batch download (parallel, dedup by URL)
        results = ip.download_images([
            {"id": "article_1", "url": "https://x.com/a.jpg"},
            {"id": "article_1", "url": "https://x.com/b.jpg"},
            {"id": "article_2", "url": "https://x.com/a.jpg"},  # dedup
        ])
        # → [{"id": "article_1", "url": "images/a.jpg"},
        #    {"id": "article_1", "url": "images/b.jpg"},
        #    {"id": "article_2", "url": "images/a.jpg"}]
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
        storage: "FileStorage",
        max_workers: int = 8,
    ) -> None:
        from storage import FileStorage  # noqa: F811
        from storage.files import S3Storage  # noqa: F811

        self._storage: FileStorage = storage
        self._is_s3 = isinstance(storage, S3Storage)
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

    def download_images(
        self,
        images: Dict[str, str],
    ) -> Dict[str, str]:
        """Download images and store via the configured storage backend.

        Dict keys deduplicate image URLs naturally — each unique URL is
        downloaded once.

        Args:
            images: ``{image_url: ""}`` — values are ignored on input.

        Returns:
            ``{image_url: stored_path, ...}``
            *stored_path* is a relative path (``images/<filename>``) for
            local, or an S3 object URL for the S3 backend.
        """
        urls = list(images.keys())
        if not urls:
            return {}

        images_dir = self._images_dir()

        print(f"[ImageProcessor] Downloading {len(urls)} unique images "
              f"(workers={self._max_workers})")

        url_map: Dict[str, Optional[str]] = {}
        executor = self._get_executor()
        futures = {
            executor.submit(self._download_and_save, url, images_dir): url
            for url in urls
        }

        for future in as_completed(futures):
            url = futures[future]
            try:
                url_map[url] = future.result()
            except Exception as e:
                print(f"[ImageProcessor] Download failed [{url}]: {e}")

        success = sum(1 for v in url_map.values() if v is not None)
        print(f"[ImageProcessor] Downloaded {success}/{len(urls)} images")

        return {u: p for u, p in url_map.items() if p is not None}

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

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _images_dir() -> str:
        """Derive today's image directory: ``news/YYYY-MM-DD/images``."""
        from utils import format_date_folder
        return f"news/{format_date_folder()}/images"

    def _download_and_save(
        self,
        url: str,
        images_dir: str,
    ) -> Optional[str]:
        """Download single image + save — used as a future in the thread pool.

        Returns a relative path (``images/<filename>``) for local storage,
        or the S3 object URL for the S3 backend.  Returns None on failure.
        """
        try:
            result = self.download(url)
            if result is None:
                return None
            image_data, content_type = result
        except Exception as e:
            print(f"[ImageProcessor] Download failed [{url}]: {e}")
            return None

        ext = self.EXT_MAP.get(content_type, ".jpg")
        filename = self._extract_filename(url, ext)
        path = f"{images_dir}/{filename}"
        saved = self._storage.save_file(image_data, path, content_type)
        if not saved:
            return None
        if self._is_s3:
            return f"/media/{saved}"  # web proxy route → presigned redirect
        return f"images/{filename}"  # relative path for local storage

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
