# coding=utf-8
"""HtmlParser 基类 — 通用 HTML → Markdown 解析能力。

子类只需覆写 _preprocess() 或 _extract() 实现站点特定逻辑。
"""

from __future__ import annotations

import re
import html as _html

from typing import Any, Dict, List, Optional

import trafilatura  # kept for metadata extraction
from readability import Document
from markdownify import markdownify


def split_keyword_tags(tags: List[str]) -> List[str]:
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

        # 3-4. 通用降级链
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
        markdown = markdownify(
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
        tags = split_keyword_tags(tags)

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
