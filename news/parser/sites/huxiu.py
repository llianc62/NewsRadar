"""HuxiuParser — 虎嗅 (huxiu.com) HTML → Markdown 解析."""

from __future__ import annotations

import re

from news.parser.parser import HtmlParser


class HuxiuParser(HtmlParser):
    """虎嗅解析器。

    两处站点噪声在 ``_preprocess`` 清理:

    1. ``<meta name="keywords">`` 包含"虎嗅网"等站点标识，
       非文章专属关键词。删除该 meta 让 Jieba 从正文提取。
    2. ``article__canonical`` 是文末的"文章标题/文章链接/阅读原文"
       跳转卡片（SEO fallback），不属于正文。它常伴随作者在正文
       末尾粘贴的 ``#`` 开头推广文案出现，是干扰 H1 截断判断的
       主要噪声源。
    """

    def _preprocess(self, html: str, url: str) -> str:
        html = re.sub(
            r'<meta[^>]+name=["\']keywords["\'][^>]*/?>',
            '',
            html,
            flags=re.I,
        )
        return re.sub(
            r'<div[^>]*class=["\'][^"\']*article__canonical[^"\']*["\'][^>]*>.*?</div>',
            '',
            html,
            flags=re.DOTALL | re.I,
        )
