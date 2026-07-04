"""StcnParser — 证券时报 (stcn.com) HTML → Markdown 解析."""

from __future__ import annotations

import re

from news.parser.parser import HtmlParser


class StcnParser(HtmlParser):
    """证券时报解析器。

    每篇文章的 ``<meta name="keywords">`` 包含 30-45 个泛化标签
    （"人工智能,半导体,机器人,新能源,数字经济..."），每篇几乎相同。
    删除该 meta 让 Jieba 从正文提取。
    """

    def _preprocess(self, html: str, url: str) -> str:
        return re.sub(
            r'<meta[^>]+name=["\']keywords["\'][^>]*/?>',
            '',
            html,
            flags=re.I,
        )
