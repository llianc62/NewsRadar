"""CctvParser — 央视网 (cctv.com) HTML → Markdown 解析.

央视网文章页通过 JavaScript 变量 ``contentdate`` 动态加载正文，
静态 HTML 中的 ``#content_area`` 为空容器，readability-lxml 无法
提取到完整内容。

解析策略：从 ``<script>`` 标签中提取 ``var contentdate = '...'``，
将 HTML 片段转 Markdown。
"""

from __future__ import annotations

import html as _html
import re

from news.parser.parser import HtmlParser


class CctvParser(HtmlParser):
    """央视网解析器 — 从 ``var contentdate`` 提取正文后转 Markdown。

    央视网文章页使用 JavaScript 动态渲染正文，静态 HTML 中
    内容为空。实际正文 HTML 存储在 ``<script>`` 标签的
    ``var contentdate = '<p>...</p>'`` 变量中。
    """

    # 匹配 var contentdate = '...' 或 var contentdate = "..."
    _CONTENTDATE_RE = re.compile(
        r"var\s+contentdate\s*=\s*'((?:[^'\\]|\\.)*)'",
        re.DOTALL,
    )

    def _extract(self, html: str, url: str = "") -> tuple[str, dict]:
        """从 JS 变量提取正文 HTML，清洗标题后缀。"""
        content_html = self._extract_contentdate(html)
        if not content_html:
            return html, {}

        # Title — the page <title> is usually clean for CCTV
        title = self._extract_title_from_html(html)
        # Strip site suffix " _新闻频道_央视网(cctv.com)" etc.
        if title:
            title = re.sub(r"[-_]\s*[一-鿿]+频道[-_]\s*央视网.*$", "", title)
            title = re.sub(r"[-_]\s*央视网.*$", "", title)
            title = title.strip()

        return content_html, {"title": title}

    @classmethod
    def _extract_contentdate(cls, html_text: str) -> str:
        """从 script 标签中提取 ``contentdate`` 变量的 HTML 正文。

        返回解码后的 HTML 片段，或空字符串。
        """
        match = cls._CONTENTDATE_RE.search(html_text)
        if not match:
            return ""

        raw = match.group(1)
        # Unescape JS string escapes
        raw = raw.replace("\\'", "'")
        raw = raw.replace('\\"', '"')
        raw = raw.replace("\\n", "\n")
        raw = raw.replace("\\t", "\t")
        raw = raw.replace("\\\\", "\\")

        # HTML unescape (e.g. &lt; → <) — the JS variable may contain
        # already-escaped HTML entities inside the JS string
        raw = _html.unescape(raw)

        return raw
