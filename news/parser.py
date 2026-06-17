# coding=utf-8
"""HTML content parser — HTML → Markdown conversion (trafilatura + fallback).

Reference: https://github.com/microsoft/markitdown
"""

import re
import json

import trafilatura
import html as _html

from typing import Any, Dict, List, Optional
from dataclasses import dataclass

from lxml import html as lxml_html
from lxml.etree import ParseError

# ═══════════════════════════════════════════════════════════════════
# Block — extracted block-level content node
# ═══════════════════════════════════════════════════════════════════

BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "pre"}


@dataclass
class Block:
    """A block-level content node extracted from DOM for boundary detection."""

    tag: str
    text: str
    text_len: int
    link_density: float
    html: str  # original inner HTML, preserved for trafilatura


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

        title = self._extract_title_from_html(html)

        markdown = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_tables=True,
            include_images=True,
            include_links=True,
            include_formatting=True,
            with_metadata=False,
        )

        # 如果正文小于50个字符，就默认是无效文档。
        if not markdown or len(markdown.strip()) <= 50:
            return None

        # 标题来源：正文 H1（干净无后缀） > HTML <title>/og:title
        title =  self._extract_markdown_heading(markdown) or title

        # 优化 markdown 文本
        markdown = self._beautify_markdown_formatting(markdown)

        # 元数据提取（轻量，只解析 head/meta/JSON-LD）
        try:
            metadata = trafilatura.extract_metadata(html, default_url=url)
        except Exception:
            metadata = None

        if metadata is None:
            return self._build_result(
                markdown=markdown.strip(),
                title=title,
            )

        tags: List[str] = []
        if metadata and metadata.categories and len(metadata.categories) > 1:
            tags = list(metadata.categories[1:])
        if metadata and metadata.tags:
            tags = list(set(tags + metadata.tags))

        author = (metadata.author or "").strip() if metadata else ""
        published_at = (metadata.date or "").strip()
        summary = (metadata.description or "").strip()
        category = metadata.categories[0] if metadata.categories else ""

        return self._build_result(
            markdown=markdown.strip(),
            title=title,
            author=author,
            published_at=published_at,
            summary=summary,
            category=category,
            tags=tags,
        )

    # ── Fallback: HTML strip ───────────────────────────────────────

    def _fallback(self, html_text: str, url: str = "") -> Optional[Dict[str, Any]]:
        """Strip HTML tags, collapse whitespace — used when trafilatura
        is unavailable or fails to extract meaningful content.

        Also extracts title from ``<title>`` tag and metadata from
        ``<meta>`` tags (author, description, published_time).
        """
        title = self._extract_title_from_html(html_text)

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

    @staticmethod
    def _extract_title_from_html(html_text: str) -> str:
        """Extract title from ``og:title`` meta or ``<title>`` tag.

        Prefers ``og:title`` (usually cleaner, without site-name suffix).
        """
        match = re.search(
            r'<meta[^>]*property=["\']og:title["\'][^>]*'
            r'content=["\']([^"\']+)["\']',
            html_text,
            re.IGNORECASE,
        )
        if match:
            return _html.unescape(match.group(1).strip())

        match = re.search(
            r"<title[^>]*>(.*?)</title>",
            html_text,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            return _html.unescape(match.group(1).strip())
        return ""

    @staticmethod
    def _beautify_markdown_formatting(markdown: str) -> str:
        """Remove UI noise that trafilatura picks up from interactive
        widgets (e.g. praise button ``- +1`` on thepaper.cn). Normalize
        ``**`` bold markers for consistency.

        1. Strip stray spaces *inside* the markers so all bold is
           ``**text**`` (fixes asymmetric ``**text **`` etc.).
        2. Insert a space *outside* when the marker abuts external
           text (e.g. ``是**text**普`` → ``是 **text** 普``).
        3. praise button ``- +1`` on thepaper.cn.
        """

        # Remove UI noise
        markdown = re.sub(r"^- \+1\n+(?=# )", "", markdown, count=1)
        # Normalize internal spacing: strip spaces between ** and content
        markdown = re.sub(r"\*\* +", "**", markdown)
        markdown = re.sub(r" +\*\*", "**", markdown)
        # Add external spacing: space between **...** and adjacent text
        markdown = re.sub(r"([^\s*])(\*\*.*?\*\*)", r"\1 \2", markdown)
        markdown = re.sub(r"(\*\*.*?\*\*)([^\s*])", r"\1 \2", markdown)
        return markdown

    @staticmethod
    def _extract_markdown_heading(markdown: str) -> str:
        """Extract article title from the first H1 heading in markdown.

        trafilatura converts body ``<h1>`` to ``# heading`` — this is the
        clean article title without site-name suffixes that pollute
        ``<title>`` and ``og:title``.
        """
        match = re.search(
            r"^#\s+(.+?)$",
            markdown.strip(),
            re.MULTILINE,
        )
        if match:
            return match.group(1).strip()
        return ""

    # ── Unified result builder ──────────────────────────────────────

    @staticmethod
    def _build_result(
        markdown: str,
        title: str = "",
        author: str = "",
        published_at: str = "",
        summary: str = "",
        category: str = "",
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Build a unified result dict from extracted content and metadata.

        All callers are responsible for extracting their own metadata
        and passing it as explicit parameters — there is no implicit
        override logic inside this method.
        """
        if tags:
            tags = [t.lstrip("#") for t in tags if t]
            tags = [t for t in tags if t]  # remove empty strings after stripping
        return {
            "markdown": markdown,
            "title": title,
            "author": author,
            "published_at": published_at,
            "summary": summary,
            "category": category,
            "tags": tags or [],
        }

    # ── Block extraction & noise trimming ──────────────────────────

    @staticmethod
    def _extract_blocks(tree) -> List["Block"]:
        """Extract block-level content nodes from lxml tree in document order.

        Only outermost block nodes are included — nested blocks (e.g.
        ``<blockquote><p>...</p></blockquote>``) yield only the parent,
        avoiding duplicate content.

        Each block's ``html`` is the full serialized element including its
        tag, so the reassembled fragment is valid HTML for trafilatura.
        """
        blocks: List[Block] = []
        for el in tree.iter():
            tag = el.tag if isinstance(el.tag, str) else ""
            if tag not in BLOCK_TAGS:
                continue

            # skip nested blocks — only keep the outermost block ancestor
            parent = el.getparent()
            if parent is not None:
                parent_tag = parent.tag if isinstance(parent.tag, str) else ""
                if parent_tag in BLOCK_TAGS:
                    continue

            text_content = el.text_content()
            text = " ".join(text_content.split())
            text_len = len(text)
            if text_len == 0:
                continue

            # calculate link density: ratio of link text to total text
            link_text = " ".join(
                a.text_content() for a in el.iter("a")
                if a.text_content()
            )
            link_text = " ".join(link_text.split())
            link_len = len(link_text)
            link_density = link_len / text_len if text_len > 0 else 0.0

            # serialize the full element (including its tag) for reassembly
            element_html = lxml_html.tostring(el, encoding="unicode")

            blocks.append(Block(
                tag=tag,
                text=text,
                text_len=text_len,
                link_density=link_density,
                html=element_html,
            ))
        return blocks

    @staticmethod
    def _trim_noise(html: str) -> Optional[str]:
        """Trim head/tail noise from HTML before feeding to trafilatura.

        Extracts block-level content nodes, finds the first "real content"
        block (the head boundary) and the last (the tail boundary), and
        returns only the HTML between them.

        Returns None when boundaries cannot be reliably detected — callers
        should fall back to the original HTML.
        """
        try:
            tree = lxml_html.fromstring(html)
        except ParseError:
            return None

        blocks = HtmlParser._extract_blocks(tree)

        # no blocks — nothing to work with
        if not blocks:
            return None

        # ── Find start (trim head) ──────────────────────────────────
        start = 0
        start_found = False
        for i, b in enumerate(blocks):
            if b.text_len >= 80 and b.link_density < 0.3:
                start = i
                start_found = True
                break
            if b.tag in ("h1", "h2", "h3") and b.text_len >= 2:
                start = i
                start_found = True
                break

        # ── Find end (trim tail) ────────────────────────────────────
        end = len(blocks) - 1
        end_found = False
        for i in range(len(blocks) - 1, -1, -1):
            b = blocks[i]
            if b.text_len >= 50 and b.link_density < 0.3:
                end = i
                end_found = True
                break
            if b.tag in ("h1", "h2", "h3", "h4") and b.text_len >= 2:
                end = i
                end_found = True
                break

        # No boundaries detected — degrade
        if not start_found and not end_found:
            return None

        # Overlap — degrade
        if start > end:
            return None

        # ── Reassemble ──────────────────────────────────────────────
        return "".join(b.html for b in blocks[start:end + 1])

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
