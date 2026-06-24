"""CkxxappParser — 参考新闻 (ckxxapp.ckxx.net) / 新华社客户端 HTML → Markdown 解析."""

from __future__ import annotations

import re
import html as _html

from typing import Any, Dict, Optional
from readability import Document
from markdownify import markdownify as _md

from news.parser.parser import HtmlParser


class CkxxappParser(HtmlParser):
    """参考新闻 / 新华社客户端解析器。

    使用 xinhuamm.net CMS 模板——文章正文 HTML 嵌入在
    ``<script>`` 标签内的 ``var contentTxt = "...";`` 变量中。
    双引号被 JS-escape 为 ``\\"``，``</`` 被写为 ``\\/`` 以避免
    提前关闭 script 标签。
    """

    def _extract(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        content_html = self._extract_js_content_vars(html)
        if not content_html:
            return None

        # Convert to markdown via readability (skip_trim — already clean)
        try:
            doc = Document(content_html, url=url)
            article_html = doc.summary()
        except Exception:
            return None

        if not article_html or not article_html.strip():
            return None

        markdown = _md(
            article_html,
            heading_style="ATX",
            strip=["script", "style"],
            escape_asterisks=False,
            escape_underscores=False,
        )

        if not markdown or len(markdown.strip()) <= 50:
            return None

        markdown = self._beautify_markdown_formatting(markdown)

        # Metadata from page HTML
        title = self._extract_title_from_html(html)
        summary = (
            self._extract_meta(html, r'name=["\']description["\']')
            or self._extract_meta(html, r'property=["\']og:description["\']')
        )
        published_at = self._extract_meta(
            html, r'property=["\']article:published_time["\']'
        )
        author = self._extract_meta(html, r'name=["\']author["\']')

        return self._build_result(
            markdown=markdown.strip(),
            title=title,
            author=author,
            published_at=published_at,
            summary=summary,
        )

    @staticmethod
    def _extract_js_content_vars(html_text: str) -> Optional[str]:
        """Extract article content HTML from JS string variables in inline scripts."""
        for var_name in ("contentTxt",):
            pattern = re.compile(
                rf'var\s+{var_name}\s*=\s*"((?:[^"\\]|\\.)*)"',
                re.DOTALL,
            )
            match = pattern.search(html_text)
            if not match:
                continue
            content = match.group(1)
            content = content.replace(r'\"', '"')
            content = content.replace(r'\/', '/')
            content = _html.unescape(content)
            if content and len(content) > 50:
                return content
        return None
