# coding=utf-8
"""HtmlParser 基类 — 通用 HTML → Markdown 解析能力。

子类只需覆写 _preprocess() 或 _extract() 实现站点特定逻辑。
"""

from __future__ import annotations

import re
import html as _html

from typing import Any, Dict, List, Optional

import trafilatura
from readability import Document
from markdownify import markdownify


def split_keyword_tags(tags: List[str]) -> List[str]:
    """Normalise keyword tags: split on common separators into individual
    tags and remove duplicates while preserving order.

    Handles ASCII and CJK separators: ``,``, ``;``, ``；``, ``，``, ``、``,
    ``|``, and whitespace.
    """
    _SEP_RE = re.compile(r'[,;；，、|\s]+')

    result: List[str] = []
    for tag in tags:
        parts = [t.strip() for t in _SEP_RE.split(tag) if t.strip()]
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

    # ── Public API ─────────────────────────────────────────────────

    def parse(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        """Extract Markdown + metadata from HTML.

        流水线: _extract → _preprocess → metadata → readability → markdownify → result

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
        # 1. _extract: 子类提取正文 HTML + 元数据
        content_html, extracted_meta = self._extract(html, url)

        if not content_html.strip():
            return None

        # 2. _preprocess: 子类清理 HTML
        content_html = self._preprocess(content_html, url)

        # 3. 元数据提取（用 _extract 返回的 HTML，trafilatura）
        metadata = self._extract_metadata(content_html, url)
        # 子类提取的元数据优先级更高
        metadata.update(extracted_meta)

        # 4. readability 提取正文
        content_html = self._readability_extract(content_html, url)
        if not content_html:
            return self._fallback(html, url)

        # 5. HTML → Markdown
        markdown = self._to_markdown(content_html)
        if not markdown or len(markdown.strip()) <= 50:
            return None

        # 6. Markdown 格式化
        markdown = self._format_markdown(markdown)

        # 7. 标题兜底
        if not metadata.get("title"):
            metadata["title"] = self._extract_markdown_heading(markdown)
        if not metadata.get("title"):
            metadata["title"] = self._extract_title_from_html(html)

        # 8. 拼装结果
        return self._build_result(markdown=markdown, **metadata)

    # ── Hooks (子类可覆写) ─────────────────────────────────────────

    def _extract(self, html: str, url: str) -> tuple[str, dict]:
        """站点特定的正文提取 — 子类可覆写此方法。

        默认行为：原样返回原始 HTML，交给 _preprocess + readability 处理。

        Returns:
            (content_html, metadata_dict)
        """
        return html, {}

    def _preprocess(self, html: str, url: str) -> str:
        """预处理 HTML — 子类可覆写此方法进行 DOM 清理等操作。

        默认行为：不修改 HTML。
        """
        return html

    # ── Pipeline steps ─────────────────────────────────────────────

    def _extract_metadata(self, html: str, url: str) -> Dict[str, Any]:
        """用 trafilatura 从原始页面 HTML 提取元数据。"""
        try:
            meta = trafilatura.extract_metadata(html, default_url=url)
        except Exception:
            meta = None

        if meta is None:
            return {}

        tags: List[str] = []
        if meta.categories and len(meta.categories) > 1:
            tags = list(meta.categories[1:])
        if meta.tags:
            tags = list(set(tags + meta.tags))
        tags = split_keyword_tags(tags)

        return {
            "title": (meta.title or "").strip(),
            "author": (meta.author or "").strip(),
            "published_at": (meta.date or "").strip(),
            "summary": (meta.description or "").strip(),
            "category": meta.categories[0] if meta.categories else "",
            "tags": tags,
        }

    def _readability_extract(self, html: str, url: str) -> Optional[str]:
        """用 readability-lxml 提取正文 HTML。"""
        # readability-lxml 兼容：<li><p>text</p></li> 会导致 readability
        # 丢弃 <blockquote> 内的 <ul>，提前展平为 <li>text</li>
        html = self._handle_blockquote_ulli(html)

        try:
            doc = Document(html, url=url)
            content_html = doc.summary()
        except Exception:
            return None

        if not content_html.strip():
            return None

        # readability may return an empty shell (<html><body></body></html>)
        # when no article content is detected — treat as failure so the
        # caller can fall back to alternative extraction.
        text_content = re.sub(r"<[^>]+>", "", content_html).strip()
        if not text_content:
            return None

        return content_html

    def _to_markdown(self, html: str) -> str:
        """HTML → Markdown 转换。"""
        return markdownify(
            html,
            heading_style="ATX",
            strip=["script", "style"],
            escape_asterisks=False,
            escape_underscores=False,
        )

    def _format_markdown(self, markdown: str) -> str:
        """Markdown 后处理：合并浮动图标段落 + 格式化 + 截断 H1 前噪声。"""
        markdown = self._beautify_markdown_formatting(markdown)

        # Trim lines before H1 (page header noise).
        #
        # Guard: only trim when the pre-H1 portion is the minority of the
        # content.  Page-header noise is a few lines before the article
        # title; but the first H1 can also appear at the END of the
        # markdown (e.g. huxiu authors paste a "# 推广文案" line after
        # the body) - trimming there would destroy the whole article.
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
        if h1_line_idx is not None and h1_line_idx > 0:
            prefix_len = sum(len(l) for l in lines[:h1_line_idx])
            suffix_len = sum(len(l) for l in lines[h1_line_idx:])
            if prefix_len < suffix_len:
                markdown = "\n".join(lines[h1_line_idx:])

        return markdown.strip()


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

    # ── readability-lxml workarounds ─────────────────────────────────

    @staticmethod
    def _handle_blockquote_ulli(html_text: str) -> str:
        """Remove ``<p>`` wrappers inside ``<li>`` elements.

        ``<li><p>text</p></li>`` causes readability-lxml to drop ``<ul>``
        sections inside ``<blockquote>`` after the first one.  Flattening
        to ``<li>text</li>`` fixes this without changing semantics.

        Inline elements (``<strong>``, ``<img>``, ``<a>``, etc.) inside
        the ``<p>`` are preserved — only the ``<p>`` / ``</p>`` tags
        are stripped.
        """

        def _unwrap_li(m: re.Match) -> str:
            inner = m.group(1)
            inner = re.sub(r"<p[^>]*>", "", inner)
            inner = re.sub(r"</p>", "", inner)
            li_open = re.match(r"<li(?:\s[^>]*)?>", m.group(0))
            li_tag = li_open.group(0) if li_open else "<li>"
            return f"{li_tag}{inner}</li>"

        # NOTE: <li(?:\s[^>]*)?> requires whitespace (or nothing) after
        # "li" — prevents matching <link>, <literal>, etc. which also
        # start with "li".
        return re.sub(
            r"<li(?:\s[^>]*)?>(.*?)</li>",
            _unwrap_li,
            html_text,
            flags=re.DOTALL,
        )

    # ── Markdown formatting ────────────────────────────────────────

    @staticmethod
    def _handle_emoji_full_line(markdown: str) -> str:
        """Merge standalone emoji/icon lines with the following paragraph.

        Some sites (e.g. ifanr morning briefings) use CSS ``float: left``
        on a single-emoji ``<p>`` to make it appear inline with the next
        ``<p>``. readability-lxml strips the CSS signal. After markdownify
        the emoji ends up isolated on its own line::

            📈

            标题文本

        We collapse the blank-line separator into a space, producing
        ``📈 标题文本``.
        """
        import re

        # Match a line of 1-6 non-text characters (emoji / dingbats /
        # symbols — anything that isn't a letter, digit, whitespace,
        # CJK, or markdown syntax), followed by one or more blank lines,
        # followed by non-blank content. Collapse the separator.
        return re.sub(
            r'^([ \t]*[^\w\s一-鿿#*>|\-`\d]{1,6})[ \t]*\n([ \t]*\n)+(?=\S)',
            r'\1 ',
            markdown,
            flags=re.MULTILINE,
        )

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
        markdown = HtmlParser._handle_emoji_full_line(markdown)
        return HtmlParser._handle_markdown_bold(markdown)

    # ── Unified result builder ─────────────────────────────────────

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
