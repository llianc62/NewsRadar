# coding=utf-8
"""HtmlParser 基类 — 通用 HTML → Markdown 解析能力。

子类只需覆写 _preprocess() 或 _extract() 实现站点特定逻辑。
"""

from __future__ import annotations

import json
import re
import html as _html

from typing import Any, Dict, List, Optional

import trafilatura  # kept for metadata extraction
from readability import Document
from markdownify import markdownify as _md


def _split_keyword_tags(tags: List[str]) -> List[str]:
    """Normalise keyword tags: split comma/space-separated strings into
    individual tags and remove duplicates while preserving order."""
    result: List[str] = []
    for tag in tags:
        # Split on comma or whitespace, drop empties
        parts = [t.strip() for t in re.split(r'[,\s]+', tag) if t.strip()]
        for p in parts:
            if p not in result:
                result.append(p)
    return result


class HtmlParser:
    """HTML → Markdown 解析基类。

    模板方法：:meth:`parse` 定义流水线骨架，
    子类覆写 :meth:`_preprocess` 或 :meth:`_extract` 实现站点特定逻辑。

    Usage::

        parser = HtmlParser(config)
        result = parser.parse(html_text, url="https://example.com")
    """

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        cfg = self._config
        crawler_cfg = cfg.get("crawler", {})
        self.max_content_length = crawler_cfg.get("max_content_length", 100000)

    # ── Public API ─────────────────────────────────────────────────

    def parse(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        """Extract Markdown + metadata from HTML.

        流水线: _preprocess → _extract → _extract_with_readability → _fallback

        Does **not** download or process images — callers should use
        :class:`ImageProcessor` separately.

        Args:
            html: Raw HTML text.
            url: Source URL (passed to readability for metadata).

        Returns:
            Dict with keys ``markdown``, ``title``, ``author``,
            ``published_at``, ``summary``, ``category``, ``tags``,
            or None if extraction produced nothing useful.
        """
        if not html or not html.strip():
            return None

        # 1. 站点可覆写的预处理 Hook
        html = self._preprocess(html, url)

        # 2. 站点可覆写的解析 Hook
        result = self._extract(html, url)

        # 3. 通用 SPA 嵌入式 JSON 提取 (__SSR__ / __NEXT_DATA__ / JSON-LD)
        if result is None:
            result = self._extract_spa_data(html, url)

        # 4-5. 通用降级链
        if result is None:
            result = self._extract_with_readability(html, url)
        if result is None:
            result = self._fallback(html, url)

        if result is not None:
            md = result.get("markdown", "")
            if md and len(md) > self.max_content_length:
                result["markdown"] = md[:self.max_content_length] + "\n\n... (truncated)"

        return result

    # ── Hooks (子类可覆写) ─────────────────────────────────────────

    def _preprocess(self, html: str, url: str) -> str:
        """预处理 HTML — 子类可覆写此方法进行 DOM 清理等操作。

        默认行为：不修改 HTML。
        """
        return html

    def _extract(self, html: str, url: str) -> Optional[Dict[str, Any]]:
        """站点特定的解析逻辑 — 子类必须覆写此方法。

        默认行为：返回 None，走降级链。
        """
        return None

    # ── SPA embedded JSON extraction (generic fallback) ─────────────

    @staticmethod
    def _build_image_markdown(html_text: str) -> str:
        """Build markdown from image-heavy HTML when readability fails."""
        imgs = re.findall(
            r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>',
            html_text, re.IGNORECASE,
        )
        text = re.sub(r'<[^>]+>', '', html_text)
        text = _html.unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()

        parts = [f'![]({url})' for url in imgs]
        if text:
            parts.append(text)
        return '\n\n'.join(parts)

    def _extract_spa_data(
        self, html_text: str, url: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Extract content from SPA embedded JSON

        (__SSR__ / __NEXT_DATA__ / JSON-LD / __NUXT__).

        Tries known SPA data patterns in order.  When JSON is found, the
        data tree is searched recursively for an object with both
        ``title`` and ``content`` keys.
        """
        candidates = self._find_json_candidates(html_text)
        for data in candidates:
            article = self._find_article_in_json(data)
            if article is None:
                continue
            content = article.get("content", "")
            if not content or not isinstance(content, str) or len(content) < 50:
                continue
            # Strip <blockquote> tags so trafilatura doesn't discard <img> inside
            content = re.sub(r'</?blockquote[^>]*>', '', content)
            # Wrap bare <img> in <p> so trafilatura/readability preserves them
            content = re.sub(r'(<img[^>]*>)', r'<p>\1</p>', content)

            # Convert to Markdown
            markdown = None
            extracted = self._extract_with_readability(content, url, skip_trim=True)
            if extracted is not None:
                markdown = extracted["markdown"]
            if markdown is None:
                fallback_result = self._fallback(content, url)
                if fallback_result is not None:
                    markdown = fallback_result["markdown"]

            # Image-heavy content
            if not markdown or len(markdown.strip()) <= 50:
                markdown = self._build_image_markdown(content)

            if markdown and len(markdown.strip()) > 50:
                title = article.get("title") or article.get("headline", "")
                pub_at = article.get("datePublished") or article.get("date", "")
                summary_val = article.get("description") or article.get("abstract", "")
                if not summary_val:
                    summary_val = (
                        self._extract_meta(
                            html_text, r'name=["\']description["\']'
                        )
                        or self._extract_meta(
                            html_text,
                            r'property=["\']og:description["\']',
                        )
                    )
                category_val = ""
                tags_val: List[str] = []
                keywords = article.get("keywords")
                if isinstance(keywords, str):
                    tags_val = [
                        k.strip()
                        for k in keywords.split(",")
                        if k.strip()
                    ]
                elif isinstance(keywords, list):
                    tags_val = [str(k) for k in keywords if k]
                section = article.get("articleSection")
                if isinstance(section, str) and section:
                    category_val = section

                return self._build_result(
                    markdown=markdown.strip(),
                    title=title,
                    published_at=pub_at,
                    summary=summary_val,
                    category=category_val,
                    tags=tags_val,
                )

        # Fallback: JS content variables (xinhuamm.net pattern)
        content_html = self._extract_js_content_vars(html_text)
        if content_html:
            markdown = None
            extracted = self._extract_with_readability(
                content_html, url, skip_trim=True
            )
            if extracted is not None:
                markdown = extracted["markdown"]
            if not markdown or len(markdown.strip()) <= 50:
                markdown = self._build_image_markdown(content_html)
            if markdown and len(markdown.strip()) > 50:
                return self._build_result(
                    markdown=markdown.strip(),
                    title=self._extract_title_from_html(html_text),
                )

        return None

    def _find_json_candidates(self, html_text: str):
        """Yield parsed JSON objects from known SPA embedding patterns.

        Patterns tried:
        1. ``__SSR__ = {...}`` (Vite SSR, e.g. wallstreetcn.com)
        2. ``__NEXT_DATA__ = {...}`` (Next.js JS assignment)
        3. ``<script id="__NEXT_DATA__" ...>`` (Next.js script tag)
        4. ``__NUXT__ = {...}`` (Nuxt)
        5. ``<script type="application/ld+json">`` (JSON-LD / Schema.org)
        """
        for data in self._extract_bracketed_json(
            html_text, r"__SSR__\s*=\s*(\{)"
        ):
            if data:
                yield data

        for data in self._extract_bracketed_json(
            html_text, r"__NEXT_DATA__\s*=\s*(\{)"
        ):
            if data:
                yield data

        for data in self._extract_bracketed_json(
            html_text,
            r'<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>\s*(\{)',
        ):
            if data:
                yield data

        for data in self._extract_bracketed_json(
            html_text, r"__NUXT__\s*=\s*(\{)"
        ):
            if data:
                yield data

        for data in self._extract_json_ld(html_text):
            if data:
                yield data

    @staticmethod
    def _extract_bracketed_json(html_text: str, pattern: str):
        """Find *pattern* in *html_text*, then bracket-match to get the
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
            if ch == "{":
                depth += 1
            elif ch == "}":
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
        """Yield JSON objects from ``<script type="application/ld+json">`` tags."""
        for match in re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>'
            r'(.*?)</script>',
            html_text,
            re.DOTALL,
        ):
            try:
                yield json.loads(match.group(1).strip())
            except (json.JSONDecodeError, ValueError):
                continue

    @staticmethod
    def _find_article_in_json(data: Any) -> Optional[Dict[str, Any]]:
        """Recursively search for dict with ``title`` + ``content`` keys."""
        best = None
        best_len = 0

        def _search(obj: Any) -> None:
            nonlocal best, best_len
            if isinstance(obj, dict):
                title = obj.get("title") or obj.get("name") or obj.get("headline", "")
                content = obj.get("content") or obj.get("articleBody", "")
                if (
                    isinstance(title, str)
                    and isinstance(content, str)
                    and len(title) > 0
                    and len(content) > 100
                ):
                    if len(content) > best_len:
                        best_len = len(content)
                        best = obj
                for v in obj.values():
                    _search(v)
            elif isinstance(obj, list):
                for item in obj:
                    _search(item)

        _search(data)
        return best

    @staticmethod
    def _extract_js_content_vars(html_text: str) -> str:
        """Extract HTML content from JS variable assignments.

        Pattern: ``var contentTxt = "<p>...</p>"`` (xinhuamm.net CMS)
        """
        match = re.search(
            r'var\s+contentTxt\s*=\s*"((?:[^"\\]|\\.)*)"',
            html_text,
            re.DOTALL,
        )
        if match:
            return match.group(1).replace("\\/", "/").replace('\\"', '"')
        return ""

    # ── readability path ───────────────────────────────────────────

    def _extract_with_readability(
        self, html: str, url: str, skip_trim: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Use readability-lxml + markdownify for content extraction.

        Set *skip_trim* to True when *html* is already clean article
        body content (e.g. from SPA JSON) that doesn't need noise trimming.
        """
        # readability-lxml: extract article content HTML
        try:
            doc = Document(html, url=url)
            content_html = doc.summary()
        except Exception:
            return None

        if not content_html or not content_html.strip():
            return None

        # markdownify: HTML → Markdown
        markdown = _md(
            content_html,
            heading_style="ATX",
            strip=["script", "style"],
            escape_asterisks=False,
            escape_underscores=False,
        )

        if not markdown or len(markdown.strip()) <= 50:
            return None

        title = self._extract_markdown_heading(markdown)
        if not title:
            title = self._extract_title_from_html(html)

        markdown = self._beautify_markdown_formatting(markdown)

        # Trim lines before H1 (page header noise)
        lines = markdown.split("\n")
        in_fence = False
        h1_line_idx: int | None = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
            elif not in_fence and re.match(r"^#\s+.+$", line):
                h1_line_idx = i
                break
        if h1_line_idx is not None:
            markdown = "\n".join(lines[h1_line_idx:])

        # Metadata extraction
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
        if metadata.categories and len(metadata.categories) > 1:
            tags = list(metadata.categories[1:])
        if metadata.tags:
            tags = list(set(tags + metadata.tags))
        tags = _split_keyword_tags(tags)

        author = (metadata.author or "").strip()
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
        """Strip HTML tags, collapse whitespace — used when readability fails."""
        title = self._extract_title_from_html(html_text)

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

    # ── Metadata extraction utilities ──────────────────────────────

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
    def _extract_markdown_heading(markdown: str) -> str:
        """Extract article title from the first H1 heading in markdown.

        Skips ``#`` lines inside fenced code blocks.
        """
        in_fence = False
        for line in markdown.strip().split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
            elif not in_fence:
                m = re.match(r"^#\s+(.+?)$", line)
                if m:
                    return m.group(1).strip()
        return ""

    # ── Markdown formatting ────────────────────────────────────────

    @staticmethod
    def _handle_markdown_bold(markdown: str) -> str:
        """Normalize ``**`` bold markers: strip internal spaces, add
        external spaces where markers abut text."""
        parts = markdown.split("**")
        if len(parts) < 2:
            return markdown

        for i in range(1, len(parts), 2):
            parts[i] = parts[i].strip()

        result = [parts[0]]
        for i in range(1, len(parts)):
            prev, cur = parts[i - 1], parts[i]
            if i % 2 == 1:          # entering bold
                if prev and not prev[-1].isspace():
                    result.append(" ")
                result.append(f"**{cur}")
            else:                   # leaving bold
                result.append("**")
                need_space = cur and not cur[0].isspace()
                if need_space or (not cur and i + 1 < len(parts)):
                    result.append(" ")
                result.append(cur)

        return "".join(result)

    @staticmethod
    def _beautify_markdown_formatting(markdown: str) -> str:
        """Post-process trafilatura output: normalize bold formatting and
        remove praise-button noise (``- +1``) from thepaper.cn widgets."""
        markdown = re.sub(r"^- \+1\n+(?=# )", "", markdown, count=1)
        return HtmlParser._handle_markdown_bold(markdown)

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
        """Build a unified result dict from extracted content and metadata."""
        if tags:
            tags = [t.lstrip("#") for t in tags if t]
            tags = [t for t in tags if t]
        return {
            "markdown": markdown,
            "title": title,
            "author": author,
            "published_at": published_at,
            "summary": summary,
            "category": category,
            "tags": tags or [],
        }
