"""WallstreetcnParser — 华尔街见闻 (wallstreetcn.com) HTML → Markdown 解析.

华尔街见闻在 ``<script>`` 中通过 JS 变量 ``__SSR__`` 注入文章数据。
文章正文 HTML 位于 JSON 路径: ``state.default.children.default.data.article.content``。
``display_time`` 为 Unix 时间戳，作者信息在 ``author.display_name``。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from news.parser.parser import HtmlParser


class WallstreetcnParser(HtmlParser):
    """华尔街见闻解析器 — 从 __SSR__ JSON 中提取文章正文 HTML。

    页面使用 Vite/SPA 架构，文章数据在 ``__SSR__`` JS 变量中，
    包含干净的正文 HTML，无需 readability 二次清洗。
    """

    def _extract(self, html: str, url: str = "") -> tuple[str, dict]:
        """从 __SSR__ JSON 提取正文 HTML 和元数据。"""
        ssr_data = self._find_ssr_json(html)
        if ssr_data is None:
            return html, {}

        article = self._find_article(ssr_data)
        if article is None:
            return html, {}

        content_html = article.get("content", "")
        if not isinstance(content_html, str) or len(content_html.strip()) < 100:
            return html, {}

        # Build metadata
        title = article.get("title", "")

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
                pass

        # Noise categories from SSR — site navigation labels, not article topics
        _NOISE_CATEGORIES = frozenset({
            "见闻首页", "见闻", "直播", "首页", "快讯", "要闻", "深度",
            "精选", "推荐", "最新", "热门", "全部",
        })

        # Tags: categories (name fields) + article.tags, minus noise
        tags: list[str] = []
        categories = article.get("categories", [])
        if isinstance(categories, list):
            for cat in categories:
                if isinstance(cat, dict):
                    name = cat.get("name", "")
                    if name and name not in _NOISE_CATEGORIES:
                        tags.append(name)
        raw_tags = article.get("tags", [])
        if isinstance(raw_tags, list):
            for t in raw_tags:
                if isinstance(t, str) and t not in tags and t not in _NOISE_CATEGORIES:
                    tags.append(t)

        # Summary
        summary = article.get("content_short", "")

        # Category
        category = next(
            (c["name"] for c in categories
             if isinstance(c, dict) and c.get("name", "") not in _NOISE_CATEGORIES),
            "",
        )

        return content_html, {
            "title": title,
            "author": author,
            "published_at": published_at,
            "summary": summary,
            "tags": tags,
            "category": category,
        }

    def _preprocess(self, html: str, url: str) -> str:
        """清理图片标签：解包 blockquote、修复懒加载、包裹裸 img、展平 li 内 p。"""
        html = self._remove_audio_components(html)
        html = self._unwrap_blockquote_images(html)
        html = self._fix_lazy_images(html)
        html = self._wrap_bare_images(html)
        return html

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

    @staticmethod
    def _remove_audio_components(html_text: str) -> str:
        """Remove audio player components identified by ``data-wscntype="audio"``.

        Wallstreetcn embeds ``<audio>`` in the morning briefing. The block
        consists of an ``<h2>`` (e.g. "华见早安之声"), a ``<p
        class="shield-text">`` (upgrade notice), and a ``<p>`` wrapping an
        ``<img data-wscntype="audio">``. All three are stripped.
        """
        # The audio <img> inside its <p> wrapper
        html_text = re.sub(
            r'<p[^>]*>\s*<img[^>]*\bdata-wscntype="audio"[^>]*>\s*</p>',
            "",
            html_text,
        )
        # The "华见早安之声" heading that labels the audio section
        html_text = re.sub(
            r"<h2[^>]*>华见早安之声</h2>",
            "",
            html_text,
        )
        # The upgrade-notice paragraph
        html_text = re.sub(
            r'<p[^>]*class="shield-text"[^>]*>.*?</p>',
            "",
            html_text,
        )
        return html_text
