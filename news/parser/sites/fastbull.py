"""FastbullParser — 法布财经 (fastbull.com) HTML → Markdown 解析."""

from __future__ import annotations

import re

from news.parser.parser import HtmlParser


class FastbullParser(HtmlParser):
    """法布财经解析器 — _preprocess 中清洗 meta keywords 脏数据。

    法布财经的 ``<meta name="keywords">`` 包含完整句子而非关键词，
    trafilatura 会将其整句提取为 tag。_preprocess 中删除该 meta 标签，
    让下游 jieba 关键词提取接管。
    """

    def _preprocess(self, html: str, url: str) -> str:
        return re.sub(
            r'<meta[^>]+name=["\']keywords["\'][^>]*/?>',
            '',
            html,
            flags=re.I,
        )
