"""ClsParser — 财联社 (cls.cn) HTML → Markdown 解析.

财联社文章页使用 Next.js 服务端渲染，在 ``<script id="__NEXT_DATA__">``
中注入文章数据。正文 HTML 位于 ``articleDetail.content``，
作者信息在 ``articleDetail.author.name``，发布时间为 Unix 时间戳
``articleDetail.ctime``，主题标签在 ``articleDetail.subject``。

当 ``__NEXT_DATA__`` 不可用时，自动降级到基类的 readability 路径。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from markdownify import markdownify as _md

from news.parser.parser import HtmlParser


class ClsParser(HtmlParser):
    """财联社解析器 — 从 ``__NEXT_DATA__`` JSON 中提取文章正文 HTML。

    cls.cn 使用 Next.js，文章数据嵌入在 ``<script id="__NEXT_DATA__">``
    标签中，包含已渲染好的正文 HTML 和完整的元数据。
    """

    def _extract(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        # 1. Extract __NEXT_DATA__ JSON
        next_data = self._find_next_data(html)
        if next_data is None:
            return None

        # 2. Navigate to article detail
        article = self._find_article_detail(next_data)
        if article is None:
            return None

        # 3. Validate article has content
        content_html = article.get("content", "")
        if not isinstance(content_html, str) or len(content_html.strip()) < 100:
            return None

        # 4. Fix lazy images (data-src → src) and wrap bare <img> in <p>
        #    so markdownify can convert them
        content_html = ClsParser._fix_lazy_images(content_html)
        content_html = ClsParser._wrap_bare_images(content_html)

        # 5. Convert to markdown — content is already clean article HTML
        try:
            markdown = _md(
                content_html,
                heading_style="ATX",
                strip=["script", "style"],
                escape_asterisks=False,
                escape_underscores=False,
            )
        except Exception:
            return None

        if not markdown or len(markdown.strip()) <= 50:
            return None

        markdown = self._beautify_markdown_formatting(markdown)

        # 7. Build metadata
        title = article.get("title", "")
        if not title:
            title = self._extract_title_from_html(html)

        # Author: from articleDetail.author.name or notes.reviewer
        author = ""
        author_obj = article.get("author", {})
        if isinstance(author_obj, dict):
            author = author_obj.get("name", "")
        if not author:
            notes_raw = article.get("notes", "")
            if isinstance(notes_raw, str) and notes_raw.strip():
                try:
                    notes = json.loads(notes_raw)
                    if isinstance(notes, dict):
                        author = notes.get("reviewer", "")
                except (json.JSONDecodeError, TypeError):
                    pass
            elif isinstance(notes_raw, dict):
                author = notes_raw.get("reviewer", "")

        # Published_at: Unix timestamp (ctime)
        published_at = ""
        ctime = article.get("ctime", 0)
        if ctime:
            try:
                dt = datetime.fromtimestamp(int(ctime), tz=timezone.utc)
                published_at = dt.isoformat()
            except (ValueError, OSError):
                published_at = self._extract_meta(
                    html, "property=[\"']article:published_time[\"']"
                )

        # Tags: subject array (name fields)
        tags: list[str] = []
        subjects = article.get("subject", [])
        if isinstance(subjects, list):
            for subj in subjects:
                if isinstance(subj, dict):
                    name = subj.get("name", "")
                    if name and name not in tags:
                        tags.append(name)

        # Summary: brief → meta description → None
        summary = article.get("brief", "")
        if not summary:
            summary = (
                self._extract_meta(html, "name=[\"']description[\"']")
                or self._extract_meta(
                    html, "property=[\"']og:description[\"']"
                )
            )

        # Category: from column info or first subject
        category = ""
        column = article.get("column", {})
        if isinstance(column, dict):
            category = column.get("columnName", "")
        if not category and tags:
            category = tags[0]

        return self._build_result(
            markdown=markdown.strip(),
            title=title,
            author=author,
            published_at=published_at,
            summary=summary,
            category=category,
            tags=tags,
        )

    # ── Internal helpers ────────────────────────────────────────────

    @staticmethod
    def _find_next_data(html_text: str) -> Optional[Dict[str, Any]]:
        """Extract JSON from ``<script id="__NEXT_DATA__">`` tag.

        Pattern: ``<script id="__NEXT_DATA__" ...>{...}</script>``
        """
        match = re.search(
            r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html_text,
            re.DOTALL,
        )
        if not match:
            return None

        try:
            return json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _find_article_detail(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Navigate Next.js data to find the article detail dict.

        Path: ``props.pageProps.articleDetail``
        """
        try:
            article = data["props"]["pageProps"]["articleDetail"]
            if isinstance(article, dict):
                return article
        except (KeyError, TypeError):
            pass
        return None

    # ── Image helpers (shared across site parsers) ────────────────

    @staticmethod
    def _fix_lazy_images(html_text: str) -> str:
        """Convert lazy-loaded ``data-src`` to ``src``."""
        for data_attr in ("data-src", "data-original", "data-srcset"):
            html_text = re.sub(
                rf"<img([^>]*)\s+{data_attr}=[\"']([^\"']+)[\"']([^>]*)\s+src=[\"'][^\"']*[\"']",
                rf"<img\1 src=\"\2\"\3",
                html_text,
            )
            html_text = re.sub(
                rf"<img([^>]*)\s+src=[\"'][^\"']*[\"']([^>]*)\s+{data_attr}=[\"']([^\"']+)[\"']",
                rf"<img\1 src=\"\3\"\2",
                html_text,
            )
        return html_text

    @staticmethod
    def _wrap_bare_images(html_text: str) -> str:
        """Wrap bare ``<img>`` in ``<p>`` so markdownify preserves them."""
        html_text = re.sub(
            r"(?<!>)\s*<img\s",
            "<p><img ",
            html_text,
        )
        html_text = re.sub(
            r"<img([^>]*?)>\s*(?!<)",
            r"<img\1></p>",
            html_text,
        )
        return html_text
