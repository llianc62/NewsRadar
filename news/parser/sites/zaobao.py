"""ZaobaoParser — 联合早报 (zaobao.com.sg) HTML → Markdown 解析.

联合早报文章页使用 React Router 客户端路由，文章正文位于
``div.articleBody``，元数据（作者、发布时间、标题）嵌入在
``<script type="application/ld+json">`` 的 JSON-LD 中。

当 JSON-LD 或 ``articleBody`` 不可用时，自动降级到基类的
readability 路径。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from lxml import html as lxml_html
from markdownify import markdownify as _md

from news.parser.parser import HtmlParser


class ZaobaoParser(HtmlParser):
    """联合早报解析器 — 从 JSON-LD 和 ``div.articleBody`` 提取文章内容。

    页面使用 React Router，文章正文在静态 HTML 中通过 ``div.articleBody``
    呈现，元数据在 JSON-LD 的 ``NewsArticle`` 条目中。
    """

    def _extract(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        # 1. Extract article body content from div.articleBody
        content_html = self._find_article_body(html)
        if not content_html or len(content_html.strip()) < 100:
            return None

        # 2. Fix lazy images (data-src → src) and wrap bare <img> in <p>
        #    so markdownify can convert them
        content_html = ZaobaoParser._fix_lazy_images(content_html)
        content_html = ZaobaoParser._wrap_bare_images(content_html)

        # 4. Convert to markdown
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

        # 5. Extract metadata from JSON-LD
        ld_meta = self._find_jsonld_meta(html)

        title = ld_meta.get("title", "")
        if not title:
            title = self._extract_title_from_html(html)

        author = ld_meta.get("author", "")
        published_at = ld_meta.get("published_at", "")

        # Summary: og:description → meta description → None
        summary = ld_meta.get("summary", "")
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
        )

    # ── Internal helpers ────────────────────────────────────────────

    @staticmethod
    def _find_article_body(html_text: str) -> str:
        """Find the ``div.articleBody`` element and return its inner HTML.

        Returns empty string if not found.
        """
        try:
            tree = lxml_html.fromstring(html_text)
        except Exception:
            return ""

        els = tree.cssselect("div.articleBody")
        if not els:
            return ""

        # Get inner HTML
        body = els[0]
        return lxml_html.tostring(body, encoding="unicode", method="html")

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
