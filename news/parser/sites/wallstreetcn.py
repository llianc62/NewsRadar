"""WallstreetcnParser — 华尔街见闻 (wallstreetcn.com) HTML → Markdown 解析.

华尔街见闻在 ``<script>`` 中通过 JS 变量 ``__SSR__`` 注入文章数据。
文章正文 HTML 位于 JSON 路径: ``state.default.children.default.data.article.content``。
``display_time`` 为 Unix 时间戳，作者信息在 ``author.display_name``。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from markdownify import markdownify as _md

from news.parser.parser import HtmlParser


class WallstreetcnParser(HtmlParser):
    """华尔街见闻解析器 — 从 __SSR__ JSON 中提取文章正文 HTML。

    页面使用 Vite/SPA 架构，文章数据在 ``__SSR__`` JS 变量中，
    包含干净的正文 HTML，无需 readability 二次清洗。
    """

    def _extract(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        # 1. Extract __SSR__ JSON
        ssr_data = self._find_ssr_json(html)
        if ssr_data is None:
            return None

        # 2. Navigate to article dict
        article = self._find_article(ssr_data)
        if article is None:
            return None

        # 3. Validate article has content
        content_html = article.get("content", "")
        if not isinstance(content_html, str) or len(content_html.strip()) < 100:
            return None

        # 4. Handle blockquote-wrapped images: unwrap so markdownify preserves them
        content_html = self._unwrap_blockquote_images(content_html)

        # 5. Fix lazy images (data-src → src)
        content_html = self._fix_lazy_images(content_html)

        # 6. Wrap bare <img> in <p> so readability/markdownify preserves them
        content_html = self._wrap_bare_images(content_html)

        # 7. Convert to markdown — content is already clean article HTML
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

        # 8. Build metadata
        title = article.get("title", "")
        if not title:
            title = self._extract_title_from_html(html)

        # Author: from SSR author object or source_name fallback
        author_obj = article.get("author", {})
        if isinstance(author_obj, dict):
            author = author_obj.get("display_name", "")
        else:
            author = str(author_obj) if author_obj else ""

        # Published_at: Unix timestamp
        published_at = ""
        display_time = article.get("display_time", 0)
        if display_time:
            try:
                dt = datetime.fromtimestamp(int(display_time), tz=timezone.utc)
                published_at = dt.isoformat()
            except (ValueError, OSError):
                # Fallback to meta extraction from HTML
                published_at = self._extract_meta(
                    html, "property=[\"']article:published_time[\"']"
                )

        # Tags: categories (name fields) + article.tags
        tags: list[str] = []
        categories = article.get("categories", [])
        if isinstance(categories, list):
            for cat in categories:
                if isinstance(cat, dict):
                    name = cat.get("name", "")
                    if name:
                        tags.append(name)
        raw_tags = article.get("tags", [])
        if isinstance(raw_tags, list):
            for t in raw_tags:
                if isinstance(t, str) and t not in tags:
                    tags.append(t)

        # Summary: content_short → og:description → None
        summary = article.get("content_short", "")
        if not summary:
            summary = (
                self._extract_meta(html, "name=[\"']description[\"']")
                or self._extract_meta(
                    html, "property=[\"']og:description[\"']"
                )
            )

        return self._build_result(
            markdown=markdown.strip(),
            title=title,
            author=author,
            published_at=published_at,
            summary=summary,
            tags=tags,
            category=categories[0]["name"] if (
                isinstance(categories, list) and len(categories) > 0
                and isinstance(categories[0], dict)
            ) else "",
        )

    # ── Internal helpers ────────────────────────────────────────────

    @staticmethod
    def _find_ssr_json(html_text: str) -> Optional[Dict[str, Any]]:
        """Extract JSON object from ``__SSR__`` JS variable assignment.

        Pattern: ``<script>__SSR__ = {...};</script>``
        """
        idx = html_text.find("__SSR__")
        if idx < 0:
            return None

        # Find opening brace after __SSR__ =
        start = html_text.find("{", idx)
        if start < 0:
            return None

        # Brace-counting extractor
        depth = 0
        for i in range(start, len(html_text)):
            ch = html_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        else:
            return None  # unmatched braces

        try:
            return json.loads(html_text[start:end])
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _find_article(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Navigate SSR JSON to find the article dict.

        Expected path: ``state.default.children.default.data.article``
        """
        try:
            state = data["state"]
            default = state["default"]
            children = default["children"]
            default_child = children["default"]
            child_data = default_child["data"]
            article = child_data["article"]
            if isinstance(article, dict):
                return article
        except (KeyError, TypeError):
            pass

        # Fallback: recursive search in the SSR data tree
        return WallstreetcnParser._find_article_recursive(data)

    @staticmethod
    def _find_article_recursive(data: Any) -> Optional[Dict[str, Any]]:
        """Recursively search for a dict with ``content`` + ``title`` fields."""
        best = None
        best_len = 0

        def _search(obj: Any) -> None:
            nonlocal best, best_len
            if isinstance(obj, dict):
                title = obj.get("title") or obj.get("name", "")
                content = obj.get("content", "")
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
    def _unwrap_blockquote_images(html_text: str) -> str:
        """Unwrap ``<blockquote>`` wrappers around ``<img>`` tags.

        华尔街见闻有时会将图片用 <blockquote> 包裹，
        导致 markdownify 无法正确转换为图片标记。
        """
        return re.sub(
            r"<blockquote[^>]*>\s*(<img[^>]*>)\s*</blockquote>",
            r"\1",
            html_text,
            flags=re.DOTALL | re.IGNORECASE,
        )

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
