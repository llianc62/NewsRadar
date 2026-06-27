"""CkxxappParser — 参考新闻 (ckxxapp.ckxx.net) / 新华社客户端 HTML → Markdown 解析."""

from __future__ import annotations

import re
import html as _html

from news.parser.parser import HtmlParser


class CkxxappParser(HtmlParser):
    """参考新闻 / 新华社客户端解析器。

    使用 xinhuamm.net CMS 模板——文章正文 HTML 嵌入在
    ``<script>`` 标签内的 ``var contentTxt = "...";`` 变量中。
    双引号被 JS-escape 为 ``\\"``，``</`` 被写为 ``\\/`` 以避免
    提前关闭 script 标签。
    """

    def _extract(self, html: str, url: str = "") -> tuple[str, dict]:
        """从 JS 变量提取正文 HTML + 从页面提取元数据。"""
        content_html = self._extract_js_content_vars(html)
        if not content_html:
            return html, {}

        # 元数据从原始页面 HTML 提取（article body 里没有 meta 标签）
        title = self._extract_title_from_html(html)
        published_at = self._extract_meta(
            html, r'property=["\']article:published_time["\']'
        )
        summary = (
            self._extract_meta(html, r'name=["\']description["\']')
            or self._extract_meta(html, r'property=["\']og:description["\']')
        )
        author = self._extract_meta(html, r'name=["\']author["\']')

        return content_html, {
            "title": title,
            "published_at": published_at,
            "summary": summary,
            "author": author,
        }

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
