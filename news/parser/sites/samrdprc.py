"""SamrdprcParser — 市场监管总局召回公告 (samrdprc.org.cn) HTML → Markdown 解析."""

from __future__ import annotations

import re

from news.parser.parser import HtmlParser


class SamrdprcParser(HtmlParser):
    """SAMR DPRC 召回公告解析器。

    每篇文章的 ``<meta name="keywords">`` 都包含完整标题作为第一个标签
    （如"【广东】广州市江科电子有限公司召回..."），另有一个泛化标签
    "国内消费品召回新闻"。删除该 meta 让 Jieba 从正文提取。
    """

    def _preprocess(self, html: str, url: str) -> str:
        return re.sub(
            r'<meta[^>]+name=["\']keywords["\'][^>]*/?>',
            '',
            html,
            flags=re.I,
        )
