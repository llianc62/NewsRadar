"""CsrcParser — 证监会 HTML → Markdown 解析。

证监会文章页面 sidebar 中包含 ``<h1>政府网站年度报表</h1>``，
trafilatura 会将其误认为文章标题。实际标题在 ``<meta name="ArticleTitle">``
或 ``<title>`` 标签中。

本 Parser 通过 _extract 从 meta 标签提取正确标题，
正文仍走通用 readability 管线。
"""

from __future__ import annotations

import re

from news.parser.parser import HtmlParser


class CsrcParser(HtmlParser):
    """证监会解析器 — 从 ``<meta name="ArticleTitle">`` 提取标题。

    证监会页面结构：
    - ``<meta name="ArticleTitle" content="真实标题">``
    - ``<h1>政府网站年度报表</h1>``（sidebar，会被 trafilatura 误提取）
    - ``<h2>真实标题</h2>``（正文标题）

    _extract 返回正确标题后，父类 pipeline 中的
    ``metadata.update(extracted_meta)`` 会覆盖 trafilatura 的标题。
    """

    _ARTICLE_TITLE_RE = re.compile(
        r'<meta[^>]*name=["\']ArticleTitle["\'][^>]*'
        r'content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )

    def _extract(self, html: str, url: str) -> tuple[str, dict]:
        """从 meta 标签提取正确标题。"""
        meta = {}
        m = self._ARTICLE_TITLE_RE.search(html)
        if m:
            meta["title"] = m.group(1).strip()
        return html, meta
