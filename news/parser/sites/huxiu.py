"""HuxiuParser — 虎嗅 (huxiu.com) HTML → Markdown 解析."""

from __future__ import annotations

import re

from news.parser.parser import HtmlParser


class HuxiuParser(HtmlParser):
    """虎嗅解析器。

    ``<meta name="keywords">`` 包含"虎嗅网"等站点标识，
    非文章专属关键词。删除该 meta 让 Jieba 从正文提取。
    """

    def _preprocess(self, html: str, url: str) -> str:
        return re.sub(
            r'<meta[^>]+name=["\']keywords["\'][^>]*/?>',
            '',
            html,
            flags=re.I,
        )
