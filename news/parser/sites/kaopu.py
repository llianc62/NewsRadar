"""KaopuParser — 靠谱新闻 (kaopu.news) HTML → Markdown 解析."""

from __future__ import annotations

from news.parser.parser import HtmlParser


class KaopuParser(HtmlParser):
    """靠谱新闻解析器 — 标准 HTML 页面，readability 降级链直接覆盖。"""
