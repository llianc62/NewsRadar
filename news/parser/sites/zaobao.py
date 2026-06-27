"""ZaobaoParser — 联合早报 (zaochenbao.com) HTML → Markdown 解析.

文章正文位于 ``div.article-body``，元数据优先从
``<script type="application/ld+json">`` 的 JSON-LD 中提取。

当 JSON-LD 或 article body 不可用时，自动降级到基类的
readability 路径。
"""

from __future__ import annotations

import json
import re
from typing import Dict
from urllib.parse import urljoin

from lxml import html as lxml_html

from news.parser.parser import HtmlParser


class ZaobaoParser(HtmlParser):
    """联合早报解析器 — 从 JSON-LD 和 ``div.articleBody`` 提取文章内容。

    页面使用 React Router，文章正文在静态 HTML 中通过 ``div.articleBody``
    呈现，元数据在 JSON-LD 的 ``NewsArticle`` 条目中。
    """

    def _extract(self, html: str, url: str = "") -> tuple[str, dict]:
        """提取 article body HTML 和 JSON-LD 元数据。"""
        content_html = self._find_article_body(html)
        if not content_html or len(content_html.strip()) < 100:
            return html, {}

        ld_meta = self._find_jsonld_meta(html)
        return content_html, ld_meta

    def _preprocess(self, html: str, url: str) -> str:
        """修复懒加载图片、解析相对 URL、包裹裸 img。"""
        html = ZaobaoParser._fix_lazy_images(html)
        html = ZaobaoParser._resolve_image_urls(html, url)
        html = ZaobaoParser._wrap_bare_images(html)
        return html

    # ── Internal helpers ────────────────────────────────────────────

    # Supported article body selectors (old zaobao.com.sg uses ``div.articleBody``;
    # current zaochenbao.com uses ``<article class="article-body">``)
    _BODY_SELECTORS = ("article.article-body", "div.articleBody")

    @classmethod
    def _find_article_body(cls, html_text: str) -> str:
        """Find the article body element and return its inner HTML.

        Returns empty string if not found.
        """
        try:
            tree = lxml_html.fromstring(html_text)
        except Exception:
            return ""

        for selector in cls._BODY_SELECTORS:
            els = tree.cssselect(selector)
            if els:
                body = els[0]
                # Strip anti-adblock warning elements
                for warning in body.cssselect(".warning"):
                    warning.drop_tree()
                return lxml_html.tostring(body, encoding="unicode", method="html")

        return ""

    @staticmethod
    def _find_jsonld_meta(html_text: str) -> Dict[str, str]:
        """Extract article metadata from JSON-LD.

        Looks for a ``NewsArticle`` type entry in any
        ``<script type="application/ld+json">`` tag.
        """
        result: Dict[str, str] = {
            "title": "",
            "author": "",
            "published_at": "",
            "summary": "",
        }

        pattern = re.compile(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            re.DOTALL,
        )

        for match in pattern.finditer(html_text):
            try:
                data = json.loads(match.group(1))
            except (json.JSONDecodeError, ValueError):
                continue

            # Handle both {..} and {"@graph": [{..}]} structures
            items = []
            if isinstance(data, dict):
                if "@graph" in data and isinstance(data["@graph"], list):
                    items = data["@graph"]
                else:
                    items = [data]
            elif isinstance(data, list):
                items = data

            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") != "NewsArticle":
                    continue

                # Title
                headline = item.get("headline", "")
                name = item.get("name", "")
                result["title"] = headline or name or ""

                # Published date
                date_pub = item.get("datePublished", "")
                if date_pub:
                    result["published_at"] = date_pub.strip()

                # Author
                author_data = item.get("author", [])
                if isinstance(author_data, list) and author_data:
                    first_author = author_data[0]
                    if isinstance(first_author, dict):
                        result["author"] = first_author.get("name", "")
                elif isinstance(author_data, dict):
                    result["author"] = author_data.get("name", "")

                # Summary
                description = item.get("description", "")
                if description:
                    result["summary"] = description.strip()

                # Stop at the first NewsArticle found
                break

        return result

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
    def _resolve_image_urls(html_text: str, base_url: str) -> str:
        """Resolve relative image ``src`` to absolute URLs.

        ``/uploads/foo.jpg`` → ``https://example.com/uploads/foo.jpg``
        """
        if not base_url:
            return html_text

        def _make_absolute(m: re.Match) -> str:
            src = m.group(2)
            if src.startswith("http://") or src.startswith("https://"):
                return m.group(0)  # already absolute
            absolute = urljoin(base_url, src)
            return m.group(0).replace(src, absolute)

        return re.sub(r'(<img[^>]*\s+src=")([^"]+)(")', _make_absolute, html_text)

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
