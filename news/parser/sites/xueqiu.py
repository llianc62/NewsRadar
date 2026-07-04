"""XueqiuParser — 雪球 (xueqiu.com) HTML → Markdown 解析."""

from __future__ import annotations

import re

from news.parser.parser import HtmlParser


class XueqiuParser(HtmlParser):
    """雪球解析器。

    ``<meta name="keywords">`` 内容被污染为文章摘要文本片段，
    非结构化关键词。删除该 meta 让 Jieba 从正文提取。
    """

    def _preprocess(self, html: str, url: str) -> str:
        return re.sub(
            r'<meta[^>]+name=["\']keywords["\'][^>]*/?>',
            '',
            html,
            flags=re.I,
        )
