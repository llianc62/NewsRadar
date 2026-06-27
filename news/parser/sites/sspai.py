"""SspaiParser — 少数派 (sspai.com) HTML → Markdown 解析."""

from __future__ import annotations

import re

from news.parser.parser import HtmlParser


class SspaiParser(HtmlParser):
    """少数派解析器 — _preprocess 中清洗 meta keywords 脏数据。

    少数派的 ``<meta name="keywords">`` 存在 SSR 渲染 bug，
    JSON 对象被 HTML 编码后直接塞进 content 属性，
    trafilatura 解析时会产出 ``"keyword":`` 等 JSON 碎片作为 tag。
    _preprocess 中删除该 meta 标签，让下游 jieba 关键词提取接管。
    """

    def _preprocess(self, html: str, url: str) -> str:
        return re.sub(
            r'<meta[^>]+name=["\']keywords["\'][^>]*/?>',
            '',
            html,
            flags=re.I,
        )
