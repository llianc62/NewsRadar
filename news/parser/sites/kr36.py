"""Kr36Parser — 36氪 (36kr.com) HTML → Markdown 解析."""

from __future__ import annotations

import re

from news.parser.parser import HtmlParser


class Kr36Parser(HtmlParser):
    """36氪解析器 — _preprocess 中移除 meta keywords 脏数据。

    36氪每篇文章的 ``<meta name="keywords">`` 都包含相同的 24 个站点全局
    SEO 标签（"资讯,股票,创业,投资,资本市场,..."），而非文章专属关键词。
    若保留则 trafilatura 会将其提取为 article tags，导致所有 36氪文章
    的 tags 完全相同，污染词云和关键词筛选。

    _preprocess 中删除该 meta 标签，让下游 jieba 关键词提取接管。
    """

    def _preprocess(self, html: str, url: str) -> str:
        return re.sub(
            r'<meta[^>]+name=["\']keywords["\'][^>]*/?>',
            '',
            html,
            flags=re.I,
        )
